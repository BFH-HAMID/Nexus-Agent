"""``nexus explain`` and ``nexus audit explain|export`` - grounded "why", and audit export."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def add_parser(sub) -> None:
    parser = sub.add_parser("explain", help="why did Nexus do that? grounded in the audit chain (opt-in)")
    parser.add_argument("action", choices=["status", "last", "action", "run", "export"])
    parser.add_argument("target", nargs="?", default="", help="action: audit seq | run: trace id or 'last'")
    _options(parser)
    parser.set_defaults(func=command)


def _options(parser) -> None:
    parser.add_argument("-C", "--workspace", default=".")
    parser.add_argument("--format", choices=["jsonl", "json", "csv"], default="jsonl", help="export format")
    parser.add_argument("--since", default="", help="export: ISO date/time or epoch seconds")
    parser.add_argument("--until", default="", help="export: ISO date/time or epoch seconds")
    parser.add_argument("--filter", default="", help="export: action prefix, e.g. permission.")
    parser.add_argument("--output", "-o", default="", help="export: write FILE and FILE.manifest.json (never overwrites)")
    parser.add_argument("--json", action="store_true")


def add_audit_options(parser) -> None:
    """Extra arguments for the existing ``nexus audit`` parser (explain / export actions)."""
    parser.add_argument("target", nargs="?", default="", help="explain: last | SEQ | run:ID")
    parser.add_argument("--format", choices=["jsonl", "json", "csv"], default="jsonl", help="export format")
    parser.add_argument("--since", default="", help="export: ISO date/time or epoch seconds")
    parser.add_argument("--until", default="", help="export: ISO date/time or epoch seconds")
    parser.add_argument("--output", "-o", default="", help="export: write FILE and FILE.manifest.json")


def _when(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Not a date/time: {value!r} (use 2026-11-10 or 2026-11-10T08:00:00Z)") from None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def _explain(workspace: Path, target: str, as_json: bool) -> int:
    from .core import explain_action, explain_run, render

    target = (target or "last").strip()
    if target.startswith("run:"):
        result = explain_run(workspace, target[4:] or "last")
    elif target == "last":
        result = explain_action(workspace)
    elif target.isdigit():
        result = explain_action(workspace, int(target))
    else:
        raise ValueError("Usage: explain last | SEQ | run:ID")
    print(json.dumps(result, indent=2, ensure_ascii=False) if as_json else render(result))
    if not result.get("found"):
        return 1
    return 0 if result.get("chain", {}).get("ok") and not result.get("warning") else 3


def _export(workspace: Path, args, action_filter: str) -> int:
    from .core import export, write_export

    text, manifest = export(workspace, args.format, since=_when(args.since), until=_when(args.until), action=action_filter)
    if args.output:
        path = write_export(args.output, text, manifest)
        print(f"Exported {manifest['events']} event(s) to {path} (sha256 {manifest['sha256'][:16]}…, "
              f"chain {'verified' if manifest['chain_ok'] else 'NOT VERIFIED'})")
    else:
        sys.stdout.write(text)
        print(json.dumps({k: manifest[k] for k in ("events", "truncated", "chain_ok", "sha256")}), file=sys.stderr)
    return 0 if manifest["chain_ok"] and manifest["rows_intact"] else 3


def command(args) -> int:
    from . import status

    workspace = Path(args.workspace).resolve()
    try:
        if args.action == "status":
            info = status(workspace)
            print(json.dumps(info, indent=2) if args.json else "\n".join(f"{k}: {v}" for k, v in info.items()))
            return 0
        if args.action == "export":
            return _export(workspace, args, args.filter)
        target = {"last": "last", "action": args.target, "run": "run:" + (args.target or "last")}[args.action]
        return _explain(workspace, target, args.json)
    except (ValueError, OSError) as exc:
        print(f"nexus explain: {exc}", file=sys.stderr)
        return 2


def audit_command(args, workspace: Path) -> int:
    """``nexus audit explain [last|SEQ|run:ID]`` and ``nexus audit export``."""
    try:
        if args.action == "export":
            return _export(workspace, args, str(getattr(args, "filter_action", "") or ""))
        return _explain(workspace, getattr(args, "target", "") or "last", bool(getattr(args, "json", False)))
    except (ValueError, OSError) as exc:
        print(f"nexus audit {args.action}: {exc}", file=sys.stderr)
        return 2
