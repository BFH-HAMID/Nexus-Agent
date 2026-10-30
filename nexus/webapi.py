"""Guardrails shared by the newer dashboard JSON endpoints.

Endpoints added after the original dashboard follow one contract:

* the existing GUI auth check runs first (token header, query token or signed
  cookie) - nothing here authenticates on its own;
* every input is validated before any state is touched;
* one in-process rate limiter per endpoint group;
* the response is ``{"ok": true, "data": ...}`` or
  ``{"ok": false, "error": {"code", "message"}}``. The legacy ``error`` string
  is kept alongside it so the older panel code keeps working;
* one redacted audit record is appended.

The limiter is deliberately per-process: this is a single local server, not a
fleet. It is a load guard, not an authorisation control.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from filelock import FileLock

AUDIT_LIMIT_BYTES = 5_000_000


class Limiter:
    """Sliding-window limiter with one bucket per caller key."""

    def __init__(self, limit: int = 120, window: float = 60.0, max_keys: int = 512):
        if type(limit) is not int or limit < 1 or float(window) <= 0:
            raise ValueError("Invalid limiter bounds")
        self.limit = limit
        self.window = float(window)
        self.max_keys = int(max_keys)
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Record one call. False means the caller is over its window."""
        stamp = time.monotonic() if now is None else float(now)
        name = str(key or "anonymous")[:120]
        with self._lock:
            bucket = self._buckets.setdefault(name, deque())
            while bucket and stamp - bucket[0] > self.window:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(stamp)
            if len(self._buckets) > self.max_keys:
                for other, item in list(self._buckets.items()):
                    if not item or stamp - item[-1] > self.window:
                        self._buckets.pop(other, None)
            return True

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        stamp = time.monotonic() if now is None else float(now)
        with self._lock:
            bucket = self._buckets.get(str(key or "anonymous")[:120])
            if not bucket or len(bucket) < self.limit:
                return 0
            return max(1, int(self.window - (stamp - bucket[0]) + 0.5))


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def failed(code: str, message: str) -> dict[str, Any]:
    """Structured error that stays readable by the pre-6.0 panel code."""
    return {
        "ok": False,
        "error": {"code": str(code)[:80], "message": str(message)[:600]},
        "error_message": str(message)[:600],
    }


def audit_path(workspace: Path) -> Path:
    from .config import nexus_home

    home = nexus_home().resolve()
    root = (home / "permissions" / hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()).resolve()
    if not root.is_relative_to(home):
        raise ValueError("API audit must stay in the trusted Nexus state directory")
    root.mkdir(parents=True, exist_ok=True)
    return root / "audit.jsonl"


def audit_api(workspace: Path, action: str, *, actor: str = "user", target: str = "",
              result: str = "ok", risk: str = "low", detail: Any = None) -> dict[str, Any]:
    """Append one audit line for a dashboard API call.

    Raises ``ValueError``/``OSError`` when the record cannot be written; callers
    treat that as a hard failure instead of silently losing the trail.
    """
    from .studio import redact

    if not isinstance(action, str) or not action or len(action) > 120:
        raise ValueError("Audit action must be a bounded string")
    if risk not in {"low", "medium", "high"}:
        raise ValueError("Audit risk must be low, medium or high")
    path = audit_path(workspace)
    lock = path.with_suffix(".lock")
    if path.is_symlink() or lock.is_symlink():
        raise ValueError("API audit symlinks are forbidden")
    event = {"at": round(time.time(), 3), "actor": str(actor)[:80], "action": action,
             "target": str(target)[:300], "result": str(result)[:120],
             "risk": risk, "detail": detail}
    line = json.dumps(redact(event), ensure_ascii=False, allow_nan=False)
    with FileLock(str(lock), timeout=5):
        if path.exists() and path.stat().st_size > AUDIT_LIMIT_BYTES:
            raise ValueError("API audit full; archive it manually before further use")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return event


def read_audit(workspace: Path, limit: int = 40) -> list[dict[str, Any]]:
    """Tail of the audit file. Read-only; never creates the file."""
    try:
        path = audit_path(workspace)
    except ValueError:
        return []
    if not path.exists():
        return []
    if path.is_symlink() or path.stat().st_size > AUDIT_LIMIT_BYTES:
        return []
    rows: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 64_000))
        tail = handle.read(64_000).decode("utf-8", "replace").splitlines()
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-max(1, min(int(limit), 500)):]
