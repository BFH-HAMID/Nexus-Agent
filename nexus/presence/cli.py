"""``nexus presence`` - home presence state, signals and rules."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def add_parser(sub) -> None:
    parser = sub.add_parser("presence", help="home presence: present / idle / away from coarse signals (opt-in)")
    parser.add_argument("action", choices=["status", "tick", "watch", "set", "history", "forget", "rules",
                                           "proposals", "dismiss", "device-hash"])
    parser.add_argument("value", nargs="?", default="", help="set: home|away|auto | device-hash: MAC | dismiss: id")
    parser.add_argument("-C", "--workspace", default=".")
    parser.add_argument("--interval", type=float, default=30.0, help="watch: seconds between evaluations")
    parser.add_argument("--count", type=int, default=0, help="watch: stop after N evaluations (0 = until interrupted)")
    parser.add_argument("--limit", "-n", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=command)


def _show(value, as_json: bool) -> None:
    if as_json or not isinstance(value, dict):
        print(json.dumps(value, indent=2, ensure_ascii=False))
    else:
        print("\n".join(f"{key}: {item}" for key, item in value.items()))


def command(args) -> int:
    from . import policy, signals, status
    from .engine import PresenceEngine

    workspace = Path(args.workspace).resolve()
    try:
        if args.action == "status":
            _show(status(workspace), args.json)
            return 0
        policy.require(workspace)
        if args.action == "device-hash":
            print(signals.device_hash(workspace, args.value))
            return 0
        if args.action == "rules":
            cfg = policy.RULES.load(workspace)
            _show({"enabled": cfg["enabled"], "rules": cfg["rules"]}, True)
            return 0
        engine = PresenceEngine(workspace)
        if args.action == "tick":
            _show(engine.tick(), args.json)
        elif args.action == "watch":
            if not 1 <= args.interval <= 3600:
                raise ValueError("--interval must be 1-3600 seconds")
            done = 0
            try:
                while True:
                    result = engine.tick()
                    print(f"{time.strftime('%H:%M:%S')} {result['state']}"
                          + "".join(f" [{row['rule']}: {row['detail']}]" for row in result["fired"]), flush=True)
                    done += 1
                    if args.count and done >= args.count:
                        break
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                pass
        elif args.action == "set":
            _show(engine.set_manual(args.value), args.json)
        elif args.action == "history":
            rows = engine.history(args.limit)
            if args.json:
                _show(rows, True)
            else:
                print("\n".join(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r['at']))}  {r['from']} -> {r['to']}"
                                f"  ({r['source']})" for r in rows) or "No presence changes recorded.")
        elif args.action == "forget":
            print(f"Removed {engine.forget()} presence history event(s).")
        elif args.action == "proposals":
            _show(engine.proposals(), True)
        else:
            print(f"Dismissed {engine.dismiss(args.value)} proposal(s).")
        return 0
    except (ValueError, OSError) as exc:
        print(f"nexus presence: {exc}", file=sys.stderr)
        return 2
