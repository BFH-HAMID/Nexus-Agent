"""``nexus meetings`` - consented meeting notes, transcript import, offline summaries."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def add_parser(sub) -> None:
    parser = sub.add_parser("meetings", help="consented meeting notes, transcripts and summaries (opt-in)")
    parser.add_argument("action", choices=["status", "start", "note", "stop", "import", "list", "show", "summary",
                                           "export", "delete", "purge"])
    parser.add_argument("values", nargs="*", help="start: TITLE | note: TEXT | import: FILE | show/summary/export/delete: ID")
    parser.add_argument("-C", "--workspace", default=".")
    parser.add_argument("--consent", default="", help="start/import: who was told that notes are taken and agreed")
    parser.add_argument("--title", default="", help="import: title (default: the file name)")
    parser.add_argument("--speaker", default="", help="note: who said it")
    parser.add_argument("--transcript", action="store_true", help="show: print the stored transcript too")
    parser.add_argument("--offline", action="store_true", help="summary: skip the model even when summary.mode is model")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=command)


def command(args) -> int:
    from . import policy, status
    from .core import Meetings, brief, render, transcript_text

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
        meetings = Meetings(workspace)
        target = values[0] if values else "latest"
        if args.action == "start":
            row = meetings.start(" ".join(values), args.consent)
            show(row, [f"started {row['id']}: {row['title']}", f"tell everyone: {row['notice']}",
                       'add notes: nexus meetings note "..."   finish: nexus meetings stop'])
        elif args.action == "note":
            row = meetings.note(" ".join(values), args.speaker)
            show(row, [f"noted ({row['segments']} lines in {row['id']})"])
        elif args.action == "stop":
            row = meetings.stop()
            show(row, [f"stopped {row['id']} ({row['segments']} lines)", f"summary: nexus meetings summary {row['id']}"])
        elif args.action == "import":
            if len(values) != 1:
                raise ValueError('Usage: nexus meetings import FILE --consent "who agreed"')
            row = meetings.import_file(values[0], args.consent, title=args.title)
            show(row, [f"imported {row['id']}: {row['title']} ({row['segments']} lines)",
                       f"summary: nexus meetings summary {row['id']}"])
        elif args.action == "list":
            rows = meetings.rows()
            show(rows, [brief(row) for row in rows] or ["no meetings"])
        elif args.action == "show":
            row, doc = meetings.get(target)
            lines = [brief(row), f"consent: {row['consent']['statement']} (recorded by {row['consent']['by']})"]
            if args.transcript:
                lines += ["", transcript_text(doc)]
            show({"meeting": row, **({"segments": doc["segments"]} if args.transcript else {})}, lines)
        elif args.action in ("summary", "export"):
            result = meetings.summary(target, model=False if args.offline or args.action == "export" else None)
            show(result, [render(result, markdown=args.action == "export")])
        elif args.action == "delete":
            if not values:
                raise ValueError("Usage: nexus meetings delete ID")
            row = meetings.delete(values[0])
            show(row, [f"deleted {row['id']}: {row['title']}"])
        else:
            gone = meetings.purge()
            show({"deleted": gone}, [f"deleted {len(gone)} meeting(s) past retention"])
    except (ValueError, OSError) as exc:
        print(f"meetings: {exc}", file=sys.stderr)
        return 2
    return 0
