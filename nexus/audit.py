"""Unified immutable hash-chained audit helpers built on securitycenter."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import redaction, securitycenter

_SENSITIVE_KEY_WORDS = frozenset({
    "token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "pin",
    "authorization",
    "auth",
    "credential",
    "private_key",
})

# Keys whose literal name or raw body must never be dumped verbatim to audit logs.
_BODY_SUMMARY_KEYS: dict[str, str] = {
    "content": "body",
    "old_text": "old_snippet",
    "new_text": "new_snippet",
    "file_base64": "file_b64",
    "audio_base64": "audio_b64",
    "raw": "raw_payload",
}


def utc_iso(ts: float | None = None) -> str:
    value = datetime.now(timezone.utc) if ts is None else datetime.fromtimestamp(float(ts), tz=timezone.utc)
    return value.isoformat(timespec="seconds")


def redact_args(value: Any, workspace: Path | str | None = None, *, depth: int = 0) -> Any:
    """Mask secrets and summarize raw content payloads without leaking secrets or file bodies."""
    if depth > 6:
        return "[truncated]"
    secret_values = redaction.secrets(workspace)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:40]:
            key_str = str(key)
            lowered = key_str.casefold()
            if any(word in lowered for word in _SENSITIVE_KEY_WORDS):
                out[key_str] = "[REDACTED_SECRET]"
                continue
            if lowered in _BODY_SUMMARY_KEYS:
                alias = _BODY_SUMMARY_KEYS[lowered]
                raw_str = item if isinstance(item, str) else json.dumps(item, default=str, ensure_ascii=False)
                out[f"{alias}_chars"] = len(raw_str)
                out[f"{alias}_sha256"] = hashlib.sha256(raw_str.encode("utf-8", errors="replace")).hexdigest()[:16]
                continue
            out[key_str] = redact_args(item, workspace, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_args(item, workspace, depth=depth + 1) for item in list(value)[:30]]
    if isinstance(value, str):
        masked = redaction.mask(value, secret_values)
        return masked[:300] + ("…" if len(masked) > 300 else "")
    return value


def args_digest(args: Any) -> str:
    return hashlib.sha256(json.dumps(args or {}, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def record(
    workspace: Path | str,
    action: str,
    *,
    who: str = "agent",
    target: str = "",
    result: str = "ok",
    risk: str = "low",
    args: Any = None,
    detail: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one hash-chained audit event with ``who/what/when/args/result``."""
    now = round(time_now(), 3)
    event: dict[str, Any] = {
        "at": now,
        "when": utc_iso(now),
        "who": str(who)[:80],
        "actor": str(who)[:80],
        "what": str(action)[:120],
        "action": str(action)[:120],
        "target": str(target)[:300],
        "result": str(result)[:120],
        "risk": str(risk)[:40],
        "detail": redact_args(detail or {}, workspace),
    }
    if args is not None:
        event["args_sha256"] = args_digest(args)
        event["detail"]["redacted_args"] = redact_args(args, workspace)
    if extra:
        for k, v in extra.items():
            if k not in event:
                event[k] = redact_args(v, workspace)
    return securitycenter.append_event(workspace, event)


def time_now() -> float:
    import time

    return time.time()


def verify(workspace: Path | str) -> dict[str, Any]:
    """Verify the workspace audit hash chain and include its file path."""
    path = securitycenter.audit_file(workspace)
    report = securitycenter.verify_chain(workspace)
    return {**report, "events": report.get("checked", 0), "path": str(path)}


def tail(workspace: Path | str, *, limit: int = 20, action: str = "") -> list[dict[str, Any]]:
    """Return the last *limit* audit entries in chronological order."""
    path = securitycenter.audit_file(workspace)
    if not path.exists() or path.is_symlink() or path.stat().st_size > securitycenter.AUDIT_LIMIT_BYTES:
        return []
    wanted = action.strip()
    limit = max(1, min(int(limit), securitycenter.MAX_EVENTS))
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 512_000))
        lines = handle.read(512_000).decode("utf-8", "replace").splitlines()
    rows: list[dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        act = str(row.get("what") or row.get("action") or "")
        if wanted and not act.startswith(wanted):
            continue
        rows.append({
            "seq": row.get("seq"),
            "at": row.get("at"),
            "when": row.get("when") or (utc_iso(row["at"]) if isinstance(row.get("at"), (int, float)) else ""),
            "who": row.get("who") or row.get("actor", ""),
            "what": act[:120],
            "target": str(row.get("target", ""))[:300],
            "result": str(row.get("result", ""))[:120],
            "risk": row.get("risk", "low"),
            "args": row.get("args") if "args" in row else (row.get("detail") or {}).get("redacted_args"),
            "detail": row.get("detail") if isinstance(row.get("detail"), dict) else {},
            "hash": str(row.get("hash", ""))[:64],
            "prev": str(row.get("prev", ""))[:64],
        })
        if len(rows) >= limit:
            break
    rows.reverse()
    return rows
