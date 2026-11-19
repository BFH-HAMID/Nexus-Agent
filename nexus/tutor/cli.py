"""``nexus tutor`` - flashcards with spaced repetition: add cards, then quiz or review what is due."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ACTIONS = ("status", "decks", "add", "import", "generate", "list", "edit", "forget", "export", "quiz", "review", "stats")
_GRADE_KEYS = {"1": "again", "2": "hard", "3": "good", "4": "easy", "again": "again", "hard": "hard", "good": "good",
               "easy": "easy", "": "good"}
_END = ("/stop", "/quit", "/q")
SHOWN = 20     # cards shown before an import or a model's proposal is confirmed


def add_parser(sub) -> None:
    parser = sub.add_parser("tutor", help="flashcards with spaced repetition: quiz, review, statistics (opt-in)")
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("values", nargs="*", help="add: DECK QUESTION ANSWER | import/generate: FILE | edit/forget: ID | "
                                                  "list/export/quiz/review/stats: [DECK]")
    parser.add_argument("-C", "--workspace", default=".")
    parser.add_argument("--deck", default="", help="import/generate: the deck to add to (default: the file name); "
                                                   "forget: remove this whole deck")
    parser.add_argument("-n", "--count", default="", metavar="N", help="quiz/review: at most N cards; generate: N cards")
    parser.add_argument("--search", default="", metavar="WORDS", help="list: cards with all of these words")
    parser.add_argument("--limit", type=int, default=50, help="list: at most this many cards")
    parser.add_argument("--front", default=None, metavar="TEXT", help="edit: the new question")
    parser.add_argument("--back", default=None, metavar="TEXT", help="edit: the new answer")
    parser.add_argument("--yes", action="store_true", help="import/generate/forget: do not ask for confirmation")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=command)


def _confirm(args, question: str) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise ValueError("Look at what is shown above, then confirm with --yes (no terminal to ask on)")
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


def run(tutor, kind: str, deck: str = "", count: int | None = None, *, ask=None, say=None):
    """One quiz or review at the terminal. Every answer is saved at once; returns the summary.

    The summary of a session that was left before its last card also holds ``stopped: True``.
    """
    from .core import outcome_line, printable, question_line

    ask, say = ask or input, say or print
    started = tutor.begin(kind, deck, count, origin="cli")
    if started["note"]:
        say(started["note"])
    say("Type your answer. Enter alone: I do not know. Ctrl-D or /stop ends." if kind == "quiz" else
        "Enter shows the answer; then grade yourself: 1 again, 2 hard, 3 good (Enter), 4 easy. Ctrl-D or /stop ends.")
    view, summary = started["question"], None
    try:
        while view is not None:
            say(question_line(view))
            typed = ask("> ")
            if typed.strip().lower() in _END:
                break
            if kind == "quiz":
                result = tutor.answer(typed)
                if result["result"] == "unsure":
                    say(f"The card says: {printable(result['expected'])}")
                    verdict = ask("Were you right? [y/N] ").strip().lower()
                    if verdict in _END:
                        break
                    result = tutor.settle(verdict in {"y", "yes"})
            else:
                say(printable(tutor.reveal()["back"]))
                grade = None
                for _attempt in range(5):
                    typed = ask("1 again  2 hard  3 good  4 easy > ").strip().lower()
                    grade = _GRADE_KEYS.get(typed)
                    if grade or typed in _END:
                        break
                if not grade:
                    break
                result = tutor.grade(grade)
            say(outcome_line(result))
            view, summary = result["next"], result["summary"]
    except (EOFError, KeyboardInterrupt):
        say("")
    if summary is not None:
        return summary
    summary = tutor.stop()      # left before the last card: what was answered is already saved
    return {**summary, "stopped": True} if summary else None


def _preview(plan: dict, marked: bool = False) -> list[str]:
    """What an import or a proposal would add, for the operator to read before saying yes."""
    from .cards import one_line
    from .core import printable

    lines = []
    for card in plan["cards"][:SHOWN]:
        doubt = "? " if marked and not card.get("in_notes", True) else "  "
        lines.append(printable(f"{doubt}{one_line(card['front'], 70)}  ->  {one_line(card['back'], 70)}"))
    if plan["new"] > SHOWN:
        lines.append(f"  ... and {plan['new'] - SHOWN} more")
    if marked and any(not card.get("in_notes", True) for card in plan["cards"]):
        lines.append("? = this answer is not word for word in your notes: check it before you learn it")
    lines += [printable(f"not used: {problem}") for problem in plan["problems"][:10]]
    if len(plan["problems"]) > 10:
        lines.append(f"not used: ... and {len(plan['problems']) - 10} more line(s)")
    lines.append(f"{plan['new']} new card(s) for '{plan['deck']}'"
                 + (f"; {plan['known']} already there" if plan["known"] else ""))
    return lines


def command(args) -> int:
    from . import status
    from .cards import one_line
    from .commands import count
    from .core import Tutor, deck_name, printable, stats_lines, summary_line

    workspace = Path(args.workspace).resolve()
    values = list(args.values)
    first = values[0] if values else ""
    named = " ".join(values)

    def show(value, lines) -> None:
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str) if args.json else "\n".join(lines))

    try:
        if args.action == "status":
            info = status(workspace)
            show(info, [f"{key}: {value}" for key, value in info.items()])
            return 0
        from . import policy

        policy.require(workspace)
        tutor = Tutor(workspace)
        if args.action == "decks":
            rows = tutor.decks()
            show(rows, [f"{row['deck']}: {row['cards']} card(s), {row['due']} due, {row['new']} new, "
                        f"{row['mature']} well known" for row in rows]
                 or ["no cards yet: nexus tutor add DECK QUESTION ANSWER"])
        elif args.action == "add":
            if len(values) != 3:
                raise ValueError('nexus tutor add DECK "QUESTION" "ANSWER"')
            done = tutor.add(values[0], values[1], values[2])
            show(done, [f"added {done['ids'][0]} to '{done['deck']}'"])
        elif args.action in ("import", "generate"):
            if len(values) != 1:
                raise ValueError(f"Which file? nexus tutor {args.action} FILE [--deck NAME]")
            try:
                deck = deck_name(args.deck or Path(first).stem)
            except ValueError:
                raise ValueError("Give the deck a name: --deck NAME") from None
            if args.action == "import":
                plan = tutor.read(first, deck)
            else:
                wanted = count(args.count) if args.count else 10
                if wanted is None:
                    raise ValueError("-n takes a number of cards, 1 to 30")
                plan = tutor.propose(first, deck, wanted)
                if plan["of_chars"] > plan["read_chars"]:
                    print(f"only the first {plan['read_chars']} of {plan['of_chars']} characters were read",
                          file=sys.stderr)
            print("\n".join(_preview(plan, marked=args.action == "generate")), file=sys.stderr)
            if not plan["new"]:
                show({"added": 0, "known": plan["known"]}, ["nothing new to add"])
                return 0
            if not _confirm(args, f"Add {plan['new']} card(s) to '{plan['deck']}'?"):
                print("Nothing added.")
                return 1
            done = tutor.add_many(plan["deck"], [("", card["front"], card["back"]) for card in plan["cards"]],
                                  source="model" if args.action == "generate" else "import", target=plan["file"])
            show(done, [f"added {done['added']} card(s) to '{done['deck']}'"])
        elif args.action == "list":
            found = tutor.cards(named, args.search, args.limit)
            lines = [printable(f"{row['id']:<6} {row['deck']}  [{row['stage']}, due {row['due']}]  "
                               f"{one_line(row['front'], 60)}  ->  {one_line(row['back'], 60)}") for row in found["rows"]]
            if found["matched"] > len(found["rows"]):
                lines.append(f"{found['matched'] - len(found['rows'])} more (--limit)")
            show(found, lines or ["no card matches"])
        elif args.action == "edit":
            if len(values) != 1:
                raise ValueError("nexus tutor edit ID [--front TEXT] [--back TEXT] (the ID is in: nexus tutor list)")
            row = tutor.edit(first, front=args.front, back=args.back)
            show(row, [printable(f"{row['id']}: {one_line(row['front'], 70)}  ->  {one_line(row['back'], 70)}")])
        elif args.action == "forget":
            if len(values) > 1:
                raise ValueError("nexus tutor forget ID   or   nexus tutor forget --deck NAME")
            plan = tutor.forget(first, deck=args.deck, dry_run=True)
            print(f"{plan['forgotten']}: {plan['cards']} card(s) and what you learned about them", file=sys.stderr)
            if not _confirm(args, "Forget them?"):
                print("Nothing forgotten.")
                return 1
            done = tutor.forget(first, deck=args.deck)
            show(done, [f"forgot {done['cards']} card(s)" + ("; the running session was closed" if done["session_closed"]
                                                             else "")])
        elif args.action == "export":
            sys.stdout.write(tutor.export(named))
        elif args.action in ("quiz", "review"):
            number = count(args.count)
            if args.count and number is None:
                raise ValueError("-n takes a number of cards, 1 to 100")
            summary = run(tutor, args.action, named, number)
            show(summary, [summary_line(summary)] if summary else ["No card was answered."])
        else:
            report = tutor.stats(named)
            show(report, stats_lines(report))
    except (ValueError, OSError) as exc:
        print(f"tutor: {exc}", file=sys.stderr)
        return 2
    return 0
