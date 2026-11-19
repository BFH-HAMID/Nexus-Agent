"""``nexus council`` - ask several models, get a tallied verdict (advisory, tool-free)."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def add_parser(sub) -> None:
    parser = sub.add_parser("council", help="model council: independent opinions + deterministic verdict (opt-in)")
    parser.add_argument("action", choices=["status", "ask", "list", "show"])
    parser.add_argument("values", nargs="*", help="ask: the question | show: session id")
    parser.add_argument("-C", "--workspace", default=".")
    parser.add_argument("--context-file", help="ask: workspace file given to the council as data")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=command)


def command(args) -> int:
    from . import policy, status
    from .core import Council, render, session, sessions

    workspace = Path(args.workspace).resolve()
    try:
        if args.action == "status":
            info = status(workspace)
            print(json.dumps(info, indent=2) if args.json else "\n".join(f"{k}: {v}" for k, v in info.items()))
            return 0
        policy.require(workspace)
        if args.action == "list":
            rows = sessions(workspace)
            print(json.dumps(rows, indent=2) if args.json else
                  "\n".join(f"{r['id']}  {str(r['decision']):<10} {r['question'][:70]}" for r in rows) or "No sessions.")
            return 0
        if args.action == "show":
            if len(args.values) != 1:
                raise ValueError("Usage: nexus council show SESSION_ID")
            record = session(workspace, args.values[0])
            print(json.dumps(record, indent=2, ensure_ascii=False) if args.json else render(record))
            return 0
        from ..config import load_config

        question = " ".join(args.values)
        context = ""
        if args.context_file:
            from ..workbench import safe_path

            context = safe_path(workspace, args.context_file).read_text(encoding="utf-8", errors="replace")[:12_000]
        record = Council(workspace, load_config(workspace)).deliberate(question, context=context)
        print(json.dumps(record, indent=2, ensure_ascii=False) if args.json else render(record))
        return 0 if record["decision"] in {"approve", "revise", "reject"} else 1
    except (ValueError, OSError) as exc:
        print(f"nexus council: {exc}", file=sys.stderr)
        return 2
