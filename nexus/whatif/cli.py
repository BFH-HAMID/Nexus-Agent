"""``nexus whatif`` - simulate a tool call, a command or a plan without running it."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def add_parser(sub) -> None:
    parser = sub.add_parser("whatif", help="simulate a tool call, command or plan; never executes (opt-in)")
    parser.add_argument("action", choices=["status", "action", "command", "plan", "ask", "list", "show"])
    parser.add_argument("values", nargs="*", help="action: TOOL | command: the shell line | plan: MISSION_ID or "
                                                  "steps.json | ask: question | show: scenario id")
    parser.add_argument("-C", "--workspace", default=".")
    parser.add_argument("--args", default="{}", help="action: tool arguments as a JSON object")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=command)


def command(args) -> int:
    from . import policy, status
    from .core import WhatIf, render, scenario, scenarios

    workspace = Path(args.workspace).resolve()
    try:
        if args.action == "status":
            info = status(workspace)
            print(json.dumps(info, indent=2) if args.json else "\n".join(f"{k}: {v}" for k, v in info.items()))
            return 0
        policy.require(workspace)
        values = list(args.values)
        if args.action == "list":
            rows = scenarios(workspace)
            print(json.dumps(rows, indent=2) if args.json else
                  "\n".join(f"{r['id']}  {r['kind']:<8} {r['subject']}" for r in rows) or "No scenarios.")
            return 0
        service = WhatIf(workspace)
        if args.action == "show":
            record = scenario(workspace, values[0] if values else "")
        elif args.action == "action":
            if len(values) != 1:
                raise ValueError("Usage: nexus whatif action TOOL --args '{...}'")
            try:
                tool_args = json.loads(args.args)
            except ValueError:
                raise ValueError("--args must be a JSON object") from None
            record = service.action(values[0], tool_args)
        elif args.action == "command":
            record = service.command(" ".join(values))
        elif args.action == "plan":
            if len(values) != 1:
                raise ValueError("Usage: nexus whatif plan MISSION_ID | steps.json")
            if values[0].endswith(".json"):
                from ..workbench import safe_path

                steps = json.loads(safe_path(workspace, values[0]).read_text(encoding="utf-8")[:400_000])
                record = service.plan(steps if isinstance(steps, list) else steps.get("steps"), subject=values[0])
            else:
                from ..chief import load_mission

                record = service.mission(load_mission(workspace, values[0]))
        else:
            record = service.ask(" ".join(values))
        print(json.dumps(record, indent=2, ensure_ascii=False) if args.json else render(record))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"nexus whatif: {exc}", file=sys.stderr)
        return 2
