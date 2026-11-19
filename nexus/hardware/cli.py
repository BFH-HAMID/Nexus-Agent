"""``nexus hardware`` - devices, one-off commands, safe state and the command log."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def add_parser(sub) -> None:
    parser = sub.add_parser("hardware", help="allowlisted serial / MQTT / GPIO devices (opt-in, dry-run by default)")
    parser.add_argument("action", choices=["status", "devices", "send", "safe", "log"])
    parser.add_argument("device", nargs="?", default="")
    parser.add_argument("command", nargs="?", default="")
    parser.add_argument("-p", "--param", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--yes", action="store_true", help="send: confirm an actuate/critical command")
    parser.add_argument("-C", "--workspace", default=".")
    parser.add_argument("--limit", "-n", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=run)


def _params(pairs: list[str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            raise ValueError("--param takes NAME=VALUE")
        out[name] = int(value) if value.lstrip("+-").isdigit() else value
    return out


def run(args) -> int:
    from . import status
    from .core import Hardware

    workspace = Path(args.workspace).resolve()
    try:
        if args.action == "status":
            info = status(workspace)
            print(json.dumps(info, indent=2) if args.json else "\n".join(f"{k}: {v}" for k, v in info.items()))
            return 0
        hardware = Hardware(workspace)
        if args.action == "devices":
            rows = hardware.catalogue()
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                for row in rows:
                    print(f"{row['device']} ({row['transport']})" + (f" - {row['description']}" if row["description"] else "")
                          + (f"  [safe: {row['safe']}]" if row["safe"] else ""))
                    for command, body in row["commands"].items():
                        print(f"  {command:<16} {body['class']:<9} " + ", ".join(body["params"]))
                if not rows:
                    print("No devices configured in hardware.yaml.")
        elif args.action == "send":
            if not args.device or not args.command:
                raise ValueError("Usage: nexus hardware send DEVICE COMMAND [-p NAME=VALUE] [--yes]")
            params = _params(args.param)
            plan = hardware.resolve(args.device, args.command, params)
            if plan["class"] != "read" and not args.yes and not hardware.cfg["dry_run"]:
                raise ValueError(f"'{args.device}.{args.command}' is a {plan['class']} command that will be transmitted: "
                                 "add --yes to confirm")
            print(json.dumps(hardware.send(args.device, args.command, params, source="cli"), indent=2, ensure_ascii=False))
        elif args.action == "safe":
            rows = hardware.safe(args.device or None, source="cli")
            print(json.dumps(rows, indent=2) if args.json else
                  "\n".join(f"{row['device']}.{row['command']}: {'ok' if row['ok'] else 'FAILED'} ({row['detail']})"
                            for row in rows) or "No device defines a safe command.")
            return 0 if all(row["ok"] for row in rows) else 1
        else:
            from .. import audit

            rows = audit.tail(workspace, limit=max(1, min(args.limit, 500)), action="hardware.")
            if args.json:
                print(json.dumps(rows, indent=2, ensure_ascii=False))
            else:
                print("\n".join(f"{row.get('when', '')}  {row.get('who', ''):<8} {row.get('target', ''):<28} "
                                f"{row.get('result', '')}" for row in rows) or "No hardware commands recorded.")
        return 0
    except (ValueError, OSError) as exc:
        print(f"nexus hardware: {exc}", file=sys.stderr)
        return 2
