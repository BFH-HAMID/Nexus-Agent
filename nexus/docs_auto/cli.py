"""``nexus docs-auto`` - scan the inbox, review proposals, file documents, fill templates."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def add_parser(sub) -> None:
    parser = sub.add_parser("docs-auto", help="classify inbox documents, propose filing, fill templates (opt-in)")
    parser.add_argument("action", choices=["status", "scan", "list", "show", "find", "file", "undo", "forget",
                                           "templates", "generate"])
    parser.add_argument("values", nargs="*", help="show/file/undo/forget: ID | find: WORDS | generate: TEMPLATE")
    parser.add_argument("-C", "--workspace", default=".")
    parser.add_argument("--category", default="", help="file: use this category instead of the proposed one")
    parser.add_argument("--all", action="store_true", help="file: every document that has a proposal")
    parser.add_argument("--missing", action="store_true", help="forget: every filed record whose file is gone")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="generate: a placeholder value")
    parser.add_argument("--from", dest="from_doc", default="", metavar="ID", help="generate: take values from an indexed document")
    parser.add_argument("--yes", action="store_true", help="file/generate: do not ask for confirmation")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=command)


def _confirm(args, question: str) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise ValueError("Review the proposal, then confirm with --yes (no terminal to ask on)")
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


def command(args) -> int:
    from . import policy, status
    from .core import DocsAuto, brief, printable

    workspace = Path(args.workspace).resolve()
    values = list(args.values)

    def show(value, lines) -> None:
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str) if args.json else "\n".join(lines))

    try:
        if args.action == "status":
            info = status(workspace)
            show(info, [f"{key}: {value}" for key, value in info.items()])
            return 0
        policy.require(workspace)
        docs = DocsAuto(workspace)
        if args.action == "scan":
            found = docs.scan()
            lines = [brief(row) for row in found["documents"]] or ["nothing new in the inbox"]
            lines += [printable(f"skipped {row['name']}: {row['reason']}") for row in found["skipped"]]
            if found["not_looked_at"]:
                lines.append(f"{found['not_looked_at']} more file(s) not looked at (limits.max_files_per_scan)")
            if any(row["status"] == "proposed" for row in found["documents"]):
                lines.append("file one: nexus docs-auto file ID    file all: nexus docs-auto file --all")
            show(found, lines)
        elif args.action == "list":
            rows = docs.records(category=args.category)
            show(rows, [brief(row) + ("\n    the file is no longer there (nexus docs-auto forget --missing)"
                                      if docs.gone(row) else "") for row in rows] or ["the index is empty"])
        elif args.action == "find":
            rows = docs.find(" ".join(values))
            show(rows, [brief(row) for row in rows] or ["no match"])
        elif args.action == "show":
            row = docs.get(values[0] if values else "")
            show(row, [brief(row), *(printable(f"  {key}: {value}") for key, value in row["fields"].items())])
        elif args.action == "file":
            wanted = [row["id"] for row in docs.records(status="proposed")] if args.all else values
            if not wanted:
                raise ValueError("Nothing to file: give an ID, or --all after a scan with proposals")
            plans = [docs.plan(ident, args.category) for ident in wanted]
            for plan in plans:
                print(printable(f"{plan['from']}  ->  {plan['to']}"), file=sys.stderr)
            if not _confirm(args, f"Move {len(plans)} file(s)?"):
                print("Nothing moved.")
                return 1
            done = [docs.file(plan["id"], args.category, expect_to=plan["to"]) for plan in plans]
            show(done, [printable(f"filed {row['name']} as {row['filed']}") for row in done]
                 + ["undo one: nexus docs-auto undo ID"])
        elif args.action == "undo":
            row = docs.undo(values[0] if values else "")
            show(row, [printable(f"moved back to {row['source']}")])
        elif args.action == "forget" and args.missing:
            rows = docs.forget_missing()
            show(rows, [f"forgot {len(rows)} record(s) whose file is gone"])
        elif args.action == "forget":
            row = docs.forget(values[0] if values else "")
            show(row, [printable(f"forgot {row['id']} ({row['name']}); the file was not touched")])
        elif args.action == "templates":
            rows = docs.templates()
            show(rows, [f"{row['name']}: {', '.join(row['placeholders']) or 'no placeholders'}" for row in rows]
                 or [f"no .md or .txt templates in {docs.cfg['templates']['folder']}/"])
        else:
            pairs = {}
            for item in args.set:
                name, sep, value = item.partition("=")
                if not sep:
                    raise ValueError("--set takes NAME=VALUE")
                pairs[name.strip()] = value
            draft = docs.draft(values[0] if values else "", pairs, from_doc=args.from_doc)
            print(draft["text"], file=sys.stderr)
            if not _confirm(args, f"Write this to {draft['to']}?"):
                print("Nothing written.")
                return 1
            done = docs.generate(values[0], pairs, from_doc=args.from_doc)
            show(done, [f"wrote {done['path']} ({done['chars']} characters)"])
    except (ValueError, OSError) as exc:
        print(f"docs-auto: {exc}", file=sys.stderr)
        return 2
    return 0
