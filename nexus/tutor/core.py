"""The tutor: cards, the daily queue, quiz and review sessions, statistics.

Cards live in one sealed document in the Nexus state folder.  Every answer is
saved at once, so a session that is interrupted loses nothing.  No model is
called unless the operator turned ``model.grade`` or ``model.generate`` on,
and never while the card store is locked.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import time
import unicodedata
from pathlib import Path
from typing import Any

from .. import policyfile
from . import cards as cardtext
from . import policy, schedule

AREA = "tutor"
KINDS = ("quiz", "review")
KEEP_DAYS = 400            # daily counters kept for the statistics
MAX_REPEATS = 2            # a missed card comes back at most this often in one session
MAX_BATCH = 2000           # cards added in one go
MAX_NOTES = 12_000         # characters of notes a model reads to propose cards
MAX_PROPOSED = 30
_EMPTY: dict[str, Any] = {"version": 1, "seq": 0, "tick": 0, "cards": {}, "days": {}, "session": None}
_CARD_TEXT = ("id", "deck", "front", "back", "due")
_CARD_NUMBERS = ("n", "reps", "lapses", "ease", "interval", "asked")
_JUDGE = """You check one flashcard answer. The user message holds the question, the answer written on the card \
and the answer the learner gave. Decide whether the learner's answer says the same thing as the card's answer: a \
synonym, another spelling, another word order or the same number written another way is the same; a different fact, \
a part of the answer or no answer is not. Reply {"same": true} or {"same": false}."""
_WRITE = """You write flashcards from a learner's notes. Reply {"cards": [{"q": "question", "a": "answer"}]}. \
Every question must be answerable from the notes alone and every answer must be stated in the notes. An answer is \
short: a word, a name, a number or one short sentence. Write in the language of the notes. Do not add facts."""


class TutorError(policy.TutorPolicyError):
    """A tutor operation was refused or failed."""


def deck_name(value: Any) -> str:
    """A deck is named with letters, digits, spaces, ``-`` or ``_``; upper and lower case are the same deck."""
    name = " ".join(unicodedata.normalize("NFC", str(value or "")).split()).casefold()
    if not 1 <= len(name) <= 40 or not name[0].isalnum() or not all(
            ch.isalnum() or ch in " -_" or unicodedata.category(ch) in ("Mn", "Mc") for ch in name):
        raise TutorError("A deck name is 1 to 40 letters, digits, spaces, '-' or '_', and starts with a letter or digit")
    return name


def printable(text: Any) -> str:
    """Card text is cleaned when it comes in; this guards the terminal against a store edited by hand."""
    return "".join(ch if ch.isprintable() or ch in "\n\u200c\u200d" else "?" for ch in str(text))


def _is_date(text: str) -> bool:
    """True for YYYY-MM-DD only (newer Pythons also read week dates such as 2026-W46-2; those are not stored)."""
    try:
        return dt.date.fromisoformat(text).isoformat() == text
    except ValueError:
        return False


def _checked(data: dict[str, Any]) -> dict[str, Any]:
    """The stored document with the shape this module relies on, or an error that names what is wrong."""
    for key, kind in (("cards", dict), ("days", dict), ("seq", int), ("tick", int)):
        if key not in data:
            data[key] = kind()
        if type(data[key]) is not kind:
            raise TutorError(f"The card store is damaged ('{key}' has the wrong shape); restore it from a backup")
    for key, card in data["cards"].items():
        good = (isinstance(card, dict) and card.get("id") == key
                and all(isinstance(card.get(name), str) and card[name] for name in _CARD_TEXT)
                and _is_date(card["due"])
                and all(type(card.get(name)) is int and card[name] >= 0 for name in _CARD_NUMBERS)
                and (card.get("last") is None or isinstance(card["last"], str)))
        if not good:
            raise TutorError(f"The card store is damaged (card '{str(key)[:20]}'); restore it from a backup")
    if not isinstance(data.get("session"), dict):
        data["session"] = None
    return data


class Tutor:
    def __init__(self, workspace: Path | str, config: Any = None, providers: Any = None, *, keyring_module: Any = None,
                 clock=time.time) -> None:
        self.workspace = Path(workspace).resolve()
        self.cfg = policy.require(self.workspace)
        self.config, self.providers, self.clock = config, providers, clock
        limits = self.cfg["limits"]
        budget = limits["max_cards"] * (limits["max_card_chars"] * 8 + 600)
        self.store = policyfile.SealedJSON(self.workspace, AREA, "cards", encrypt=self.cfg["encrypt_at_rest"],
                                           keyring_module=keyring_module, error=TutorError,
                                           limit=min(max(2_000_000, budget), 96_000_000))

    # -- store ------------------------------------------------------------------------------- #
    def today(self) -> dt.date:
        return dt.date.fromtimestamp(self.clock())

    def _load(self) -> dict[str, Any]:
        return _checked(self.store.load(_EMPTY))

    def _update(self):
        return self.store.update(_EMPTY)

    def _sealable(self) -> None:
        problem = self.store.problem()
        if problem:   # said before anything is typed or reviewed, not at the first save
            raise TutorError(f"The cards cannot be sealed ({problem}). Install nexus-agent[secure-state] with a keyring "
                             "backend, or set encrypt_at_rest: false in tutor.yaml to keep them as plain text")

    def _scope(self, data: dict[str, Any], deck: str) -> tuple[str, list[dict[str, Any]]]:
        """``(deck, its cards)``; an empty deck name means every deck. An unknown deck is an error."""
        rows = list(data["cards"].values())
        if not deck:
            return "", rows
        deck = deck_name(deck)
        chosen = [card for card in rows if card["deck"] == deck]
        if not chosen:
            names = sorted({card["deck"] for card in rows})
            raise TutorError(f"No deck '{deck}'" + (f" (decks: {', '.join(names[:12])})" if names else
                                                    f". Add a card first: nexus tutor add \"{deck}\" QUESTION ANSWER"))
        return deck, chosen

    # -- cards ------------------------------------------------------------------------------- #
    def _prepare(self, data: dict[str, Any], deck: str, pairs: list[tuple[Any, Any, Any]]) -> dict[str, Any]:
        """Sort candidate cards into new, already known and unusable. ``pairs`` holds ``(where, front, back)``."""
        deck = deck_name(deck)
        limits = self.cfg["limits"]
        if len(pairs) > MAX_BATCH:
            raise TutorError(f"More than {MAX_BATCH} cards at once; split the file")
        existing = {cardtext.normalize(card["front"]): card for card in data["cards"].values() if card["deck"] == deck}
        if not existing and len({card["deck"] for card in data["cards"].values()}) >= limits["max_decks"]:
            raise TutorError(f"There are already {limits['max_decks']} decks (limits.max_decks in tutor.yaml)")
        new: list[dict[str, str]] = []
        problems: list[str] = []
        known = 0
        seen: set[str] = set()
        for where, front, back in pairs:
            label = f"{where}: " if where else ""
            try:
                front = cardtext.clean(front, limits["max_card_chars"], "question")
                back = cardtext.clean(back, limits["max_card_chars"], "answer")
            except cardtext.Unusable as exc:
                problems.append(f"{label}{exc}")
                continue
            key = cardtext.normalize(front)
            if key in seen:
                known += 1
            elif key in existing:
                known += 1
                if sorted(cardtext.accepted(existing[key]["back"])) != sorted(cardtext.accepted(back)):
                    problems.append(f"{label}this question is already in the deck with another answer; the card stays "
                                    f"as it is (to change it: nexus tutor edit {existing[key]['id']} --back ...)")
            else:
                new.append({"front": front, "back": back})
            seen.add(key)
        room = limits["max_cards"] - len(data["cards"])
        if len(new) > room:
            raise TutorError(f"{len(new)} new card(s), but there is room for {max(room, 0)} "
                             "(limits.max_cards in tutor.yaml). Nothing was added")
        return {"deck": deck, "cards": new, "new": len(new), "known": known, "problems": problems}

    def check(self, deck: str, pairs: list[tuple[Any, Any, Any]]) -> dict[str, Any]:
        """What adding these cards would do. Stores nothing."""
        self._sealable()
        return self._prepare(self._load(), deck, pairs)

    def add_many(self, deck: str, pairs: list[tuple[Any, Any, Any]], *, source: str = "manual", by: str = "cli",
                 target: str = "") -> dict[str, Any]:
        """Add cards to a deck. A question that is already there is left alone. Returns what happened."""
        self._sealable()
        with self._update() as data:
            _checked(data)
            plan = self._prepare(data, deck, pairs)
            today = self.today()
            ids = []
            for row in plan["cards"]:
                data["seq"] += 1
                card = {"id": f"c{data['seq']}", "n": data["seq"], "deck": plan["deck"], "front": row["front"],
                        "back": row["back"], "source": source, "created": today.isoformat(), "asked": 0,
                        **schedule.start(today, self.cfg["schedule"])}
                data["cards"][card["id"]] = card
                ids.append(card["id"])
        if ids:
            policyfile.audit(self.workspace, "tutor.add", who=by, target=target or plan["deck"], result="added",
                             detail={"cards": len(ids), "source": source})
        return {"deck": plan["deck"], "added": len(ids), "ids": ids, "known": plan["known"], "problems": plan["problems"]}

    def add(self, deck: str, front: str, back: str, *, by: str = "cli") -> dict[str, Any]:
        done = self.add_many(deck, [("", front, back)], by=by)
        if done["problems"]:
            raise TutorError(done["problems"][0])
        if not done["added"]:
            raise TutorError(f"That question is already in '{done['deck']}'")
        return done

    def read(self, path: Path | str, deck: str) -> dict[str, Any]:
        """Parse a card file and say what an import would add. Stores nothing."""
        self._sealable()
        name, text, sha = self._text(path, cardtext.SUFFIXES)
        try:
            found, problems = cardtext.parse(text, Path(path).suffix)
        except cardtext.Unusable as exc:
            raise TutorError(f"{name}: {exc}") from None
        if not found:
            raise TutorError(f"{name}: no card found" + (f" ({problems[0]})" if problems else ""))
        plan = self._prepare(self._load(), deck, [(f"line {line}", front, back) for line, front, back in found])
        return {**plan, "file": name, "sha256": sha, "problems": problems + plan["problems"]}

    def _text(self, path: Path | str, suffixes: tuple[str, ...]) -> tuple[str, str, str]:
        """``(printable name, text, sha256)`` of a small text file the operator named."""
        path = Path(path).expanduser()
        name = printable(path.name)[:80]
        if path.suffix.casefold() not in suffixes:
            raise TutorError(f"{name}: expected a {', '.join(suffixes)} file")
        try:
            if path.is_symlink() or not path.is_file():
                raise TutorError(f"{name}: not a regular file")
            data = policyfile.read_bounded(path, self.cfg["limits"]["max_import_bytes"])
            return name, cardtext.decode(data), cardtext.digest(data)
        except TutorError:
            raise
        except cardtext.Unusable as exc:
            raise TutorError(f"{name}: {exc}") from None
        except OSError as exc:
            raise TutorError(f"{name}: could not be read ({type(exc).__name__})") from None
        except ValueError:
            raise TutorError(f"{name}: larger than limits.max_import_bytes, or not a plain file") from None

    def cards(self, deck: str = "", search: str = "", limit: int = 50) -> dict[str, Any]:
        """Cards of a deck (or all), in the order they were added; ``search`` keeps those with every word."""
        data = self._load()
        deck, rows = self._scope(data, deck)
        words = cardtext.normalize(search).split()[:12]

        def has_all(card: dict[str, Any]) -> bool:
            text = cardtext.normalize(card["front"] + " " + card["back"])
            return all(word in text for word in words)

        if words:
            rows = [card for card in rows if has_all(card)]
        rows.sort(key=lambda card: (card["deck"], card["n"]))
        limit = max(1, min(int(limit), 500))
        return {"deck": deck, "matched": len(rows), "rows": [self._row(card) for card in rows[:limit]]}

    def _row(self, card: dict[str, Any]) -> dict[str, Any]:
        return {"id": card["id"], "deck": card["deck"], "front": card["front"], "back": card["back"],
                "stage": schedule.stage(card), "due": card["due"], "interval_days": card["interval"],
                "lapses": card["lapses"], "source": card.get("source", "manual")}

    def edit(self, card_id: str, *, front: str | None = None, back: str | None = None, by: str = "cli") -> dict[str, Any]:
        """Correct a card. Its schedule is kept."""
        if front is None and back is None:
            raise TutorError("Nothing to change: give --front and/or --back")
        limit = self.cfg["limits"]["max_card_chars"]
        with self._update() as data:
            card = _checked(data)["cards"].get(str(card_id))
            if card is None:
                raise TutorError(f"No card '{printable(card_id)[:20]}' (ids are shown by: nexus tutor list)")
            try:
                if front is not None:
                    front = cardtext.clean(front, limit, "question")
                    key = cardtext.normalize(front)
                    if any(other["deck"] == card["deck"] and other["id"] != card["id"]
                           and cardtext.normalize(other["front"]) == key for other in data["cards"].values()):
                        raise TutorError(f"That question is already in '{card['deck']}'")
                    card["front"] = front
                if back is not None:
                    card["back"] = cardtext.clean(back, limit, "answer")
            except cardtext.Unusable as exc:
                raise TutorError(str(exc)) from None
            row = self._row(card)
        policyfile.audit(self.workspace, "tutor.edit", who=by, target=row["id"], result="changed")
        return row

    def forget(self, card_id: str = "", *, deck: str = "", dry_run: bool = False, by: str = "cli") -> dict[str, Any]:
        """Remove one card, or a whole deck with its history."""
        if bool(card_id) == bool(deck):
            raise TutorError("Forget what? Give a card id, or --deck NAME for a whole deck")
        with self._update() as data:
            _checked(data)
            if card_id:
                if str(card_id) not in data["cards"]:
                    raise TutorError(f"No card '{printable(card_id)[:20]}' (ids are shown by: nexus tutor list)")
                gone, what = [str(card_id)], f"card {card_id}"
            else:
                deck, rows = self._scope(data, deck)
                gone, what = [card["id"] for card in rows], f"deck '{deck}'"
            closed = False
            if not dry_run:
                session = self._live(data)
                if session is not None and set(gone) & set(session["queue"][session["at"]:]):
                    self._close(data, session, "stopped")     # its next question may be one of these cards
                    closed = True
                for key in gone:
                    del data["cards"][key]
        if not dry_run:
            policyfile.audit(self.workspace, "tutor.forget", who=by, target=what, result="removed",
                             detail={"cards": len(gone)})
        return {"forgotten": what, "cards": len(gone), "session_closed": closed}

    def export(self, deck: str) -> str:
        """A deck as tab-separated text (question, answer): a backup, and a file other flashcard programs read."""
        if not deck:
            raise TutorError("Which deck? nexus tutor export DECK")
        _deck, rows = self._scope(self._load(), deck)
        out = io.StringIO(newline="")
        writer = csv.writer(out, delimiter="\t", lineterminator="\n")
        for card in sorted(rows, key=lambda card: card["n"]):
            writer.writerow([card["front"], card["back"]])
        return out.getvalue()

    # -- the queue --------------------------------------------------------------------------- #
    def _left(self, data: dict[str, Any], today: dt.date) -> tuple[int, int]:
        """``(new cards, reviews)`` still allowed today."""
        day = data["days"].get(today.isoformat())
        day = day if isinstance(day, dict) else {}
        used_new, used_reviews = (day.get(key) if type(day.get(key)) is int else 0 for key in ("new", "reviews"))
        settings = self.cfg["schedule"]
        return max(0, settings["new_per_day"] - used_new), max(0, settings["reviews_per_day"] - used_reviews)

    def _queue(self, data: dict[str, Any], kind: str, rows: list[dict[str, Any]], count: int | None,
               today: dt.date) -> tuple[list[str], dict[str, int]]:
        iso = today.isoformat()
        new_left, reviews_left = self._left(data, today)
        due = sorted((card for card in rows if card["last"] and card["due"] <= iso),
                     key=lambda card: (card["due"], card["asked"], card["n"]))
        fresh = sorted((card for card in rows if not card["last"]), key=lambda card: card["n"])
        facts = {"due": len(due), "new": len(fresh), "new_held": max(0, len(fresh) - new_left),
                 "reviews_held": 0, "new_left": new_left}
        if kind == "review":
            facts["reviews_held"] = max(0, len(due) - reviews_left)
            chosen = due[:reviews_left] + fresh[:new_left]
            chosen = chosen[:count] if count else chosen
        else:
            # a quiz also takes cards that are not due yet: the ones that were asked longest ago come first
            later = sorted((card for card in rows if card["last"] and card["due"] > iso),
                           key=lambda card: (card["asked"], card["due"], card["n"]))
            chosen = (due + fresh[:new_left] + later)[:count or self.cfg["quiz"]["questions"]]
        return [card["id"] for card in chosen], facts

    def _nothing(self, kind: str, deck: str, rows: list[dict[str, Any]], facts: dict[str, int], today: dt.date) -> str:
        where = f"in '{deck}' " if deck else ""
        settings = self.cfg["schedule"]
        if not rows:
            return "There are no cards yet. Add one: nexus tutor add DECK QUESTION ANSWER"
        if kind == "review" and facts["reviews_held"]:
            return (f"Today's {settings['reviews_per_day']} reviews are done; {facts['reviews_held']} more card(s) "
                    f"{where}wait (schedule.reviews_per_day in tutor.yaml)")
        if facts["new_held"] and (kind == "review" or facts["new"] == len(rows)):
            if not settings["new_per_day"]:
                return (f"New cards are switched off (schedule.new_per_day: 0 in tutor.yaml); {facts['new_held']} "
                        f"card(s) {where}wait")
            return (f"Today's {settings['new_per_day']} new cards are done; {facts['new_held']} more {where}wait for "
                    "tomorrow (schedule.new_per_day in tutor.yaml)")
        coming = min((card["due"] for card in rows if card["last"] and card["due"] > today.isoformat()), default="")
        return (f"Nothing {where}is due today." + (f" The next card is due on {coming}." if coming else "")
                + " To practise anyway, start a quiz.")

    # -- sessions ---------------------------------------------------------------------------- #
    def _live(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """The session that is still running, moved past cards that were removed meanwhile; else ``None``."""
        session = data.get("session")
        if not isinstance(session, dict) or session.get("kind") not in KINDS:
            return None
        queue, at, touched = session.get("queue"), session.get("at"), session.get("touched")
        if not isinstance(queue, list) or type(at) is not int or at < 0 or not isinstance(touched, (int, float)):
            return None
        if session.get("day") != self.today().isoformat():
            return None    # yesterday's queue is out of date
        if session.get("origin") == "voice" and not 0 <= self.clock() - touched <= self.cfg["voice"]["idle_minutes"] * 60:
            return None    # a spoken quiz nobody answers must not keep taking what is said
        while at < len(queue) and queue[at] not in data["cards"]:
            at += 1
            session["pending"] = None
        session["at"] = at
        return session if at < len(queue) else None

    @staticmethod
    def _view(data: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        card = data["cards"][session["queue"][session["at"]]]
        pending = session.get("pending") if isinstance(session.get("pending"), dict) else None
        return {"kind": session["kind"], "origin": session.get("origin", ""), "deck": card["deck"], "card": card["id"],
                "front": card["front"], "number": session["at"] + 1, "of": len(session["queue"]),
                "stage": schedule.stage(card), "pending": bool(pending),
                **({"back": card["back"], "given": str(pending.get("given", ""))} if pending else {})}

    def _summary(self, data: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        iso = self.today().isoformat()
        deck = session.get("deck", "")
        waiting = sum(1 for card in data["cards"].values()
                      if card["last"] and card["due"] <= iso and (not deck or card["deck"] == deck))
        return {"kind": session["kind"], "deck": deck, "answers": session["right"] + session["wrong"],
                "right": session["right"], "wrong": session["wrong"], "due_today": waiting}

    def _close(self, data: dict[str, Any], session: dict[str, Any], result: str) -> dict[str, Any]:
        summary = self._summary(data, session)
        data["session"] = None
        policyfile.audit(self.workspace, "tutor.session", who=str(session.get("origin", ""))[:20],
                         target=summary["deck"] or "all decks", result=result,
                         detail={"kind": summary["kind"], "right": summary["right"], "wrong": summary["wrong"]})
        return summary

    def begin(self, kind: str, deck: str = "", count: int | None = None, *, origin: str = "cli") -> dict[str, Any]:
        """Start a quiz or a review. A session that was still open is closed; its answers are already saved."""
        if kind not in KINDS:
            raise TutorError("A session is a quiz or a review")
        if count is not None and not 1 <= int(count) <= 100:
            raise TutorError("A session has 1 to 100 cards")
        self._sealable()
        with self._update() as data:
            _checked(data)
            deck, rows = self._scope(data, deck)
            today = self.today()
            queue, facts = self._queue(data, kind, rows, count, today)
            if not queue:
                raise TutorError(self._nothing(kind, deck, rows, facts, today))
            before = self._live(data)
            if before is not None:
                self._close(data, before, "replaced")
            session = {"id": policyfile.new_id(kind), "kind": kind, "origin": origin, "deck": deck,
                       "day": today.isoformat(), "queue": queue, "at": 0, "repeats": {}, "right": 0, "wrong": 0,
                       "pending": None, "touched": self.clock()}
            data["session"] = session
            view = self._view(data, session)
        policyfile.audit(self.workspace, "tutor.session", who=origin, target=deck or "all decks", result="start",
                         detail={"kind": kind, "cards": len(queue)})
        note = ""
        wanted = count or (self.cfg["quiz"]["questions"] if kind == "quiz" else None)
        if facts["new_held"] and (wanted is None or len(queue) < wanted):
            note = (f"{facts['new_held']} new card(s) wait for another day "
                    f"(schedule.new_per_day: {self.cfg['schedule']['new_per_day']})")
        return {"question": view, "cards": len(queue), "note": note}

    def current(self) -> dict[str, Any] | None:
        """The question that is waiting for an answer, or ``None`` when no session is running."""
        data = self._load()
        session = self._live(data)
        return self._view(data, session) if session is not None else None

    def _need(self, data: dict[str, Any], kind: str = "") -> dict[str, Any]:
        session = self._live(data)
        if session is None:
            raise TutorError("No quiz or review is running. Start one: quiz [DECK] or review [DECK]")
        if kind and session["kind"] != kind:
            raise TutorError("A review is running: show the answer, then grade yourself with again, hard, good or easy"
                             if session["kind"] == "review" else "A quiz is running: give your answer, or skip")
        return session

    def _advance(self, mark: tuple[str, int], *, right: bool, grade: str = "", by: str, given: str = "") -> dict[str, Any]:
        """Record the answer to the current card and move on. ``mark`` names the question that was answered."""
        with self._update() as data:
            _checked(data)
            session = self._need(data)
            if (session["id"], session["at"]) != mark:
                raise TutorError("The session moved on in the meantime; nothing was recorded for this answer")
            today = self.today()
            iso = today.isoformat()
            card = data["cards"][session["queue"][session["at"]]]
            was_new = not card["last"]
            was_due = was_new or card["due"] <= iso
            if not grade:
                # a quiz never pushes a card away before its time; a miss always brings it back
                grade = "again" if not right else ("good" if was_due else "")
            if grade:
                card.update(schedule.after(card, grade, today, self.cfg["schedule"]))
            data["tick"] += 1
            card["asked"] = data["tick"]
            day = data["days"].get(iso)
            if not isinstance(day, dict):
                day = data["days"][iso] = {}
            for key, hit in (("new", was_new), ("reviews", was_due and not was_new), ("right", right), ("wrong", not right)):
                day[key] = (day.get(key) if type(day.get(key)) is int else 0) + int(hit)
            for stale in sorted(data["days"])[:-KEEP_DAYS]:
                del data["days"][stale]
            session["right" if right else "wrong"] += 1
            again = False
            repeats = session["repeats"] if isinstance(session.get("repeats"), dict) else {}
            if not right and int(repeats.get(card["id"], 0)) < MAX_REPEATS:
                repeats[card["id"]] = int(repeats.get(card["id"], 0)) + 1
                session["queue"].append(card["id"])
                again = True
            session.update(repeats=repeats, at=session["at"] + 1, pending=None, touched=self.clock())
            result = {"kind": session["kind"], "result": "right" if right else "wrong", "by": by,
                      "front": card["front"], "expected": card["back"], "given": given, "again": again, "due": card["due"],
                      "in_days": (dt.date.fromisoformat(card["due"]) - today).days, "next": None, "summary": None}
            following = self._live(data)
            if following is not None:
                result["next"] = self._view(data, following)
            else:
                result["summary"] = self._close(data, session, "end")
        return result

    def answer(self, text: Any, *, also: tuple[str, ...] = ()) -> dict[str, Any]:
        """Quiz: check an answer. ``result`` is ``right``, ``wrong`` or ``unsure`` (the learner decides: ``settle``).

        ``also`` holds other readings of the same answer (a spoken answer without "it is"); one match is enough.
        """
        given = cardtext.clean_answer(text, self.cfg["limits"]["max_card_chars"])
        data = self._load()
        session = self._need(data, "quiz")
        if session.get("pending"):
            raise TutorError("First say whether you were right: yes or no")
        card = data["cards"][session["queue"][session["at"]]]
        mark = (session["id"], session["at"])
        if not given:
            return self._advance(mark, right=False, by="skip")
        if any(cardtext.matches(reading, card["back"]) for reading in (given, *also[:4])):
            return self._advance(mark, right=True, by="match", given=given)
        if self._same(card["front"], card["back"], given):     # a model call: made while nothing is locked
            return self._advance(mark, right=True, by="model", given=given)
        with self._update() as data:
            _checked(data)
            session = self._need(data, "quiz")
            if (session["id"], session["at"]) != mark:
                raise TutorError("The session moved on in the meantime; nothing was recorded for this answer")
            session.update(pending={"given": given}, touched=self.clock())
            view = self._view(data, session)
        return {"kind": "quiz", "result": "unsure", "by": "", "front": card["front"], "expected": card["back"],
                "given": given, "again": False, "next": view, "summary": None}

    def fits(self, text: Any, *, also: tuple[str, ...] = ()) -> bool:
        """Quiz: True when the text is the answer of the card that is being asked. Records nothing.

        For a caller with control words of its own (the spoken quiz): a card's answer may be "pass" or "again".
        """
        given = cardtext.clean_answer(text, self.cfg["limits"]["max_card_chars"])
        data = self._load()
        session = self._live(data)
        if not given or session is None or session["kind"] != "quiz" or session.get("pending"):
            return False
        card = data["cards"][session["queue"][session["at"]]]
        return any(cardtext.matches(reading, card["back"]) for reading in (given, *also[:4]))

    def settle(self, right: bool) -> dict[str, Any]:
        """Quiz: the learner saw the card's answer and says whether their own answer was right."""
        data = self._load()
        session = self._need(data, "quiz")
        pending = session.get("pending")
        if not isinstance(pending, dict):
            raise TutorError("There is no answer waiting for your verdict")
        return self._advance((session["id"], session["at"]), right=bool(right), by="you", given=str(pending.get("given", "")))

    def skip(self) -> dict[str, Any]:
        """Quiz: "I do not know". Counts as a miss and shows the answer."""
        data = self._load()
        session = self._need(data, "quiz")
        return self._advance((session["id"], session["at"]), right=False, by="skip")

    def reveal(self) -> dict[str, Any]:
        """Review: the back of the current card."""
        data = self._load()
        session = self._need(data, "review")
        card = data["cards"][session["queue"][session["at"]]]
        return {"front": card["front"], "back": card["back"], "card": card["id"]}

    def grade(self, grade: str) -> dict[str, Any]:
        """Review: how well the learner knew the card (``again``, ``hard``, ``good`` or ``easy``)."""
        if grade not in policy.GRADES:
            raise TutorError(f"A grade is one of {', '.join(policy.GRADES)}")
        data = self._load()
        session = self._need(data, "review")
        return self._advance((session["id"], session["at"]), right=grade != "again", grade=grade, by="you")

    def stop(self) -> dict[str, Any] | None:
        """End the running session. Returns its summary, or ``None`` when none was running."""
        with self._update() as data:
            _checked(data)
            session = self._live(data)
            if session is None:
                data["session"] = None
                return None
            return self._close(data, session, "stopped")

    # -- optional model ---------------------------------------------------------------------- #
    def _ask(self, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One model call under the operator's settings. Raises ``TutorError`` when there is no usable answer."""
        settings = self.cfg["model"]
        try:
            if self.config is None:
                from ..config import load_config

                self.config = load_config(self.workspace)
            agent = policyfile.pick_agent(self.config, settings["agent"])
            if policyfile.is_cloud_agent(self.config, agent) and not settings["allow_cloud"]:
                raise TutorError("tutor.yaml keeps your cards local: choose a local agent for model.agent or set "
                                 "model.allow_cloud: true")
            if not policyfile.rate_allow(policyfile.state_dir(self.workspace, AREA) / "calls.json",
                                         settings["calls_per_hour"], 3600, now=self.clock()):
                raise TutorError("The model was asked model.calls_per_hour times in the last hour (tutor.yaml)")
            providers = self.providers if self.providers is not None else policyfile.providers_for(self.config)
            value, _usage = policyfile.ask_json(providers, agent, system, payload, allow_cloud=settings["allow_cloud"],
                                                max_tokens=settings["max_tokens_per_call"], workspace=self.workspace)
        except TutorError:
            raise
        except (RuntimeError, ValueError, OSError) as exc:   # provider down, reply not JSON, unknown agent
            raise TutorError(f"The model gave no usable answer ({str(exc)[:120]})") from None
        return value

    def _same(self, front: str, back: str, given: str) -> bool:
        """True only when grading by model is on and the model says the answer means the same as the card's."""
        if not self.cfg["model"]["grade"]:
            return False
        try:
            value = self._ask(_JUDGE, {"question": front, "card_answer": back, "learner_answer": given})
        except TutorError:
            return False     # no verdict: the learner is asked instead
        return value.get("same") is True

    def propose(self, path: Path | str, deck: str, count: int = 10) -> dict[str, Any]:
        """Ask the model for cards from a notes file. Stores nothing: the operator reads them first."""
        if not self.cfg["model"]["generate"]:
            raise TutorError("Cards are proposed by a model: set model.generate: true in tutor.yaml")
        if not 1 <= int(count) <= MAX_PROPOSED:
            raise TutorError(f"Ask for 1 to {MAX_PROPOSED} cards")
        deck = deck_name(deck)
        self._sealable()
        name, text, _sha = self._text(path, (".txt", ".md"))
        notes = text[:MAX_NOTES]
        if not notes.strip():
            raise TutorError(f"{name}: the file is empty")
        value = self._ask(_WRITE, {"notes": notes, "cards": int(count)})
        rows = value.get("cards")
        pairs = [("", row.get("q"), row.get("a")) for row in (rows if isinstance(rows, list) else [])[:int(count)]
                 if isinstance(row, dict)]
        if not pairs:
            raise TutorError("The model proposed no card")
        plan = self._prepare(self._load(), deck, pairs)
        searched = cardtext.normalize(notes)
        for card in plan["cards"]:
            card["in_notes"] = cardtext.normalize(card["back"]) in searched
        return {**plan, "file": name, "read_chars": len(notes), "of_chars": len(text)}

    # -- statistics -------------------------------------------------------------------------- #
    def decks(self) -> list[dict[str, Any]]:
        data = self._load()
        iso = self.today().isoformat()
        out: dict[str, dict[str, Any]] = {}
        for card in data["cards"].values():
            row = out.setdefault(card["deck"], {"deck": card["deck"], "cards": 0, "due": 0, "new": 0, "learning": 0,
                                                "mature": 0})
            row["cards"] += 1
            row[schedule.stage(card)] += 1
            row["due"] += bool(card["last"] and card["due"] <= iso)
        return [out[name] for name in sorted(out)]

    def stats(self, deck: str = "") -> dict[str, Any]:
        """Counts and dates: how many cards, how many are due, and what was answered lately."""
        data = self._load()
        deck, rows = self._scope(data, deck)
        today = self.today()
        iso = today.isoformat()
        stages = {"new": 0, "learning": 0, "mature": 0}
        for card in rows:
            stages[schedule.stage(card)] += 1
        new_left, _reviews_left = self._left(data, today)

        def day(offset: int) -> dict[str, int]:
            found = data["days"].get((today - dt.timedelta(days=offset)).isoformat())
            found = found if isinstance(found, dict) else {}
            return {key: found[key] if type(found.get(key)) is int else 0 for key in ("new", "reviews", "right", "wrong")}

        week = [day(offset) for offset in range(7)]
        month = [day(offset) for offset in range(30)]
        streak = 0
        for offset in range(KEEP_DAYS):
            found = day(offset)
            if found["right"] + found["wrong"]:
                streak += 1
            elif offset:       # a day without answers ends the run; today may still be open
                break
        answered = sum(row["right"] + row["wrong"] for row in month)
        return {
            "deck": deck, "cards": len(rows), **stages,
            "due": sum(1 for card in rows if card["last"] and card["due"] <= iso),
            "new_today": min(stages["new"], new_left),
            "next_due": min((card["due"] for card in rows if card["last"] and card["due"] > iso), default=None),
            "today": {"answers": week[0]["right"] + week[0]["wrong"], "right": week[0]["right"]},
            "week": {"answers": sum(row["right"] + row["wrong"] for row in week), "right": sum(row["right"] for row in week)},
            "month_right_percent": round(100 * sum(row["right"] for row in month) / answered) if answered else None,
            "streak_days": streak,
        }


# --------------------------------------------------------------------------- #
# words for the terminal
# --------------------------------------------------------------------------- #


def question_line(view: dict[str, Any]) -> str:
    return printable(f"[{view['number']}/{view['of']}] {view['deck']}: {view['front']}")


def outcome_line(result: dict[str, Any]) -> str:
    """One answer as a line: whether it was right, and when the card comes back."""
    expected = printable(result["expected"])
    if result["result"] == "unsure":
        return f"The card says: {expected}\nWere you right?"
    if result["result"] == "right":
        when = f"Back in {_days(result['in_days'])}." if result.get("in_days", 0) > 0 else ""
        if result["kind"] == "review":
            return when or "Noted."
        how = {"model": " (accepted by the model)", "you": " (on your word)"}.get(result["by"], "")
        return f"Right{how}. {when}".rstrip()
    back = "It comes back in this session." if result["again"] else "It is due again today."
    if result["kind"] == "review" or result["by"] == "you":
        return f"Not yet. {back}"
    return ("Skipped." if result["by"] == "skip" else "Not yet.") + f" The card says: {expected}\n{back}"


def _days(count: int) -> str:
    return "1 day" if count == 1 else f"{count} days"


def summary_line(summary: dict[str, Any], *, stopped: bool = False) -> str:
    """How a session went. ``stopped``: it was ended before its last card."""
    ended = "stopped" if stopped or summary.get("stopped") else "finished"
    text = f"{summary['kind'].capitalize()} {ended}: {summary['right']} of {summary['answers']} right."
    if summary["due_today"]:
        text += f" {summary['due_today']} card(s) are still due today."
    return text


def stats_lines(stats: dict[str, Any]) -> list[str]:
    scope = f"deck '{stats['deck']}'" if stats["deck"] else "all decks"
    lines = [f"{scope}: {stats['cards']} card(s): {stats['new']} new, {stats['learning']} learning, "
             f"{stats['mature']} well known",
             f"due today: {stats['due']} review(s), {stats['new_today']} new"
             + (f"; next review on {stats['next_due']}" if stats["next_due"] and not stats["due"] else "")]
    week = stats["week"]
    lines.append(f"answered (all decks): today {stats['today']['answers']}, last 7 days {week['answers']}"
                 + (f", {stats['month_right_percent']}% right over 30 days" if stats["month_right_percent"] is not None else "")
                 + (f"; {_days(stats['streak_days'])} in a row" if stats["streak_days"] else ""))
    return lines
