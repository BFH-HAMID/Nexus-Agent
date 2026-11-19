"""``nexus devpilot`` - patch, test in a throw-away checkout, review, apply."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def add_parser(sub) -> None:
    parser = sub.add_parser("devpilot", help="model patch -> isolated checkout -> tests -> reviewed apply (opt-in)")
    parser.add_argument("action", choices=["status", "detect", "run", "list", "show", "diff", "apply", "discard"])
    parser.add_argument("values", nargs="*", help="run: the task | show/diff/apply/discard: run id")
    parser.add_argument("-C", "--workspace", default=".")
    parser.add_argument("-f", "--file", action="append", default=[], help="run: a file the model should see (repeatable)")
    parser.add_argument("--max-iterations", type=int, help="run: fewer model calls than devpilot.yaml allows")
    parser.add_argument("--include-flagged", action="store_true",
                        help="run: also show the model files that contain instruction-like text")
    parser.add_argument("--yes", action="store_true", help="apply: do not ask for confirmation")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=command)


def _one(values: list[str], action: str) -> str:
    if len(values) != 1:
        raise ValueError(f"Usage: nexus devpilot {action} RUN_ID")
    return values[0]


def command(args) -> int:
    from . import core, policy, status

    workspace = Path(args.workspace).resolve()

    def show(value, text: str) -> None:
        print(json.dumps(value, indent=2, ensure_ascii=False) if args.json else text)

    try:
        if args.action == "status":
            info = status(workspace)
            show(info, "\n".join(f"{key}: {value}" for key, value in info.items()))
            return 0
        policy.require(workspace)
        if args.action == "detect":
            found = core.DevPilot(workspace).commands()
            rows = {kind: {"command": value["display"], "source": value["source"]} for kind, value in found.items()}
            show(rows, "\n".join(f"{kind}: {row['command'] or '(none)'}  [{row['source']}]" for kind, row in rows.items())
                 + "\nThese run on this machine inside a throw-away checkout of your last commit.")
            return 0
        if args.action == "list":
            rows = core.runs(workspace)
            show(rows, "\n".join(f"{row['id']}  {row['status']:<10} {len(row['files'])} file(s)  {row['task'][:60]}"
                                 for row in rows) or "No runs.")
            return 0
        if args.action == "run":
            task = " ".join(args.values)
            pilot = core.DevPilot(workspace)
            plan = pilot.preview(task, args.file, args.max_iterations)
            if not args.json:
                print("DevPilot runs on this machine, in a checkout of commit " + plan["from_commit"] + ": "
                      + (" and ".join(plan["runs_on_this_machine"]) or "(no check command)"), file=sys.stderr)
            record = pilot.run(task, args.file, max_iterations=args.max_iterations,
                               include_flagged=args.include_flagged, expect=plan["runs_on_this_machine"])
            show(core.brief(record), core.render(record))
            return 0 if record["status"] == "passed" else 1
        ident = _one(args.values, args.action)
        if args.action == "show":
            record = core.load_run(workspace, ident)
            show(record, core.render(record))
            return 0
        if args.action == "diff":
            print(core.diff(workspace, ident), end="")
            return 0
        if args.action == "discard":
            core.discard(workspace, ident)
            show({"discarded": ident}, f"Discarded {ident}.")
            return 0
        record = core.load_run(workspace, ident)
        summary = core.review(workspace, ident, record["patch_sha256"])
        if not args.yes:
            print(core.render(record), file=sys.stderr)
            if not sys.stdin.isatty():
                raise ValueError("Review the diff, then confirm with --yes (no terminal to ask on)")
            if input(f"Write {len(summary['files'])} file(s) into {workspace}? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("Not applied.")
                return 1
        done = core.apply(workspace, ident, record["patch_sha256"])
        show(done, f"Applied {len(done['applied'])} file(s): {', '.join(done['applied'])}\n{done['note']}")
        return 0
    except (ValueError, OSError) as exc:
        print(f"nexus devpilot: {exc}", file=sys.stderr)
        return 2
