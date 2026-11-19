"""``nexus finance`` - import exported statements, then look at where the money went. Read-only towards the world."""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

ACTIONS = ("status", "import", "summary", "months", "list", "recurring", "unusual", "accounts", "imports",
           "categories", "categorize", "suggest", "forget")


def add_parser(sub) -> None:
    parser = sub.add_parser("finance", help="read-only personal finance from exported bank statements (opt-in)")
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("values", nargs="*", help="import: FILE | summary/unusual: YYYY-MM | months: N | "
                                                  "categorize: ID CATEGORY | forget: IMPORT_ID")
    parser.add_argument("-C", "--workspace", default=".")
    parser.add_argument("--account", default="", help="import: which account the statement belongs to; reports: only this one")
    parser.add_argument("--month", default="", metavar="YYYY-MM", help="list: only this month")
    parser.add_argument("--category", default="", help="list: only this category")
    parser.add_argument("--search", default="", metavar="WORDS", help="list: descriptions with all of these words")
    parser.add_argument("--limit", type=int, default=50, help="list: at most this many rows")
    parser.add_argument("--before", default="", metavar="YYYY-MM-DD", help="forget: everything before this date")
    parser.add_argument("--all", action="store_true", help="forget: the whole ledger")
    parser.add_argument("--yes", action="store_true", help="import/forget/suggest: do not ask for confirmation")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=command)


def _confirm(args, question: str) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise ValueError("Look at what is shown above, then confirm with --yes (no terminal to ask on)")
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


def command(args) -> int:
    from . import policy, render, status
    from .commands import count
    from .core import Finance, brief
    from .money import fmt

    workspace = Path(args.workspace).resolve()
    values = list(args.values)
    first = values[0] if values else ""

    def show(value, lines) -> None:
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str) if args.json else "\n".join(lines))

    try:
        if args.action == "status":
            info = status(workspace)
            show(info, [f"{key}: {value}" for key, value in info.items()])
            return 0
        policy.require(workspace)
        finance = Finance(workspace)
        if args.action == "import":
            if not first:
                raise ValueError("Which file? nexus finance import statement.csv [--account NAME]")
            parsed = finance.read(first, args.account)
            print("\n".join(render.preview(parsed)), file=sys.stderr)
            if not parsed["new"]:
                show({"added": 0, "known": parsed["known"]}, ["nothing new: every transaction is already in the ledger"])
                return 0
            if not _confirm(args, f"Add {parsed['new']} transaction(s) to the ledger?"):
                print("Nothing imported.")
                return 1
            done = finance.add(first, args.account, expect=parsed["sha256"])
            show(done, [f"added {done['added']} transaction(s) as {done['batch']} "
                        f"(take them out again: nexus finance forget {done['batch']})"])
        elif args.action == "summary":
            report = finance.summary(first, account=args.account)
            show(report, render.summary(report))
        elif args.action == "months":
            rows = finance.months(count(first), account=args.account)
            show(rows, render.months(rows))
        elif args.action == "list":
            found = finance.transactions(month=args.month, account=args.account, category=args.category,
                                         search=args.search, limit=args.limit)
            lines = [f"{row['id']}  {brief(row)}" for row in found["rows"]]
            if found["matched"] > len(found["rows"]):
                lines.append(f"{found['matched'] - len(found['rows'])} more (--limit)")
            show(found, lines or ["no transactions match"])
        elif args.action == "recurring":
            items = finance.recurring()
            show(items, render.recurring(items))
        elif args.action == "unusual":
            report = finance.unusual(first)
            show(report, render.unusual(report))
        elif args.action == "accounts":
            rows = finance.accounts()
            show(rows, [f"{row['account']}: {row['transactions']} transactions, {row['first']} to {row['last']}, "
                        f"{', '.join(row['currencies'])}"
                        + (f"; balance {fmt(Decimal(row['balance']['balance']))} on {row['balance']['date']} "
                           f"(per {row['balance']['file']})" if row.get("balance") else "")
                        for row in rows] or ["no transactions yet: nexus finance import FILE"])
        elif args.action == "imports":
            rows = finance.imports()
            show(rows, [f"{row['id']}  {row['file']} -> {row['account']}: {row['transactions']} transaction(s), "
                        f"{row['first']} to {row['last']}" for row in rows] or ["nothing imported yet"])
        elif args.action == "categories":
            waiting = finance.uncategorized()
            lines = [f"{name}: {', '.join(words)}" for name, words in finance.rules.items()]
            lines.append("transfer: (money between your own accounts; not counted as income or spending)")
            if waiting:
                lines += ["", "without a category (add a keyword to finance.yaml, or: nexus finance categorize ID CATEGORY):"]
                lines += [f"  {row['description']}: {row['transactions']} transaction(s), {fmt(Decimal(row['total']))}"
                          for row in waiting]
            show({"categories": finance.rules, "uncategorized": waiting}, lines)
        elif args.action == "categorize":
            if len(values) != 2:
                raise ValueError("nexus finance categorize ID CATEGORY (the ID is in: nexus finance list)")
            done = finance.categorize(values[0], values[1])
            show(done, [f"'{done['description']}' is now {done['category']}: {done['transactions']} transaction(s)"])
        elif args.action == "suggest":
            proposals = finance.suggest()
            if not proposals:
                show([], ["the model had no suggestion"])
                return 0
            for row in proposals:
                print(f"{row['description']}  ->  {row['category']}", file=sys.stderr)
            if not _confirm(args, f"Remember these {len(proposals)} categories?"):
                print("Nothing remembered.")
                return 1
            finance.learn({row["description"]: row["category"] for row in proposals})
            show(proposals, [f"remembered {len(proposals)} description(s)"])
        else:
            plan = finance.forget(first, before=args.before, everything=args.all, dry_run=True)
            print(f"{plan['forgotten']}: {plan['transactions']} transaction(s). Statement files are not touched.",
                  file=sys.stderr)
            if not _confirm(args, "Forget them?"):
                print("Nothing forgotten.")
                return 1
            done = finance.forget(first, before=args.before, everything=args.all)
            show(done, [f"forgot {done['transactions']} transaction(s)"])
    except (ValueError, OSError) as exc:
        print(f"finance: {exc}", file=sys.stderr)
        return 2
    return 0
