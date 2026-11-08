"""Operator-configured per-minute action rate limits and daily token/cost caps."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any

from filelock import FileLock

LIMITS_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "actions_per_minute": 30,
    "daily_tokens": 0,
    "daily_cost_usd": 0.0,
}


class LimitExceeded(RuntimeError):
    """Raised when an operator-configured rate limit or daily cap is exceeded."""


def parse_limits_policy(value: Any) -> dict[str, Any]:
    """Validate optional ``limits:`` block in ``permissions.yaml``."""
    if value is None:
        return dict(LIMITS_DEFAULTS)
    if not isinstance(value, dict):
        raise ValueError("Invalid limits policy: expected a mapping")
    unknown = set(value) - set(LIMITS_DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown limits policy field(s): {', '.join(sorted(unknown))}")
    cfg = {**LIMITS_DEFAULTS, **value}
    if type(cfg["enabled"]) is not bool:
        raise ValueError("limits.enabled must be true or false")
    apm = cfg["actions_per_minute"]
    if type(apm) is not int or not 0 <= apm <= 10_000:
        raise ValueError("limits.actions_per_minute must be an integer between 0 and 10000")
    tokens = cfg["daily_tokens"]
    if type(tokens) is not int or not 0 <= tokens <= 1_000_000_000:
        raise ValueError("limits.daily_tokens must be an integer between 0 and 1000000000")
    cost = cfg["daily_cost_usd"]
    if type(cost) not in (int, float) or isinstance(cost, bool) or not math.isfinite(cost) or not 0 <= float(cost) <= 1_000_000:
        raise ValueError("limits.daily_cost_usd must be a finite non-negative number")
    cfg["daily_cost_usd"] = float(cost)
    return cfg


def _state_path(workspace: Path | str) -> Path:
    from .config import nexus_home

    home = nexus_home().resolve()
    key = hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()
    root = (home / "limits").resolve()
    if not root.is_relative_to(home):
        raise ValueError("Rate limit state must remain inside Nexus home")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root / f"{key}.json"


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.stem}.{uuid.uuid4().hex[:8]}.tmp")
    raw = (json.dumps(data, sort_keys=True) + "\n").encode("utf-8")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _load_policy(workspace: Path | str) -> dict[str, Any]:
    from .permissions import Permissions

    return Permissions(workspace).load().get("limits") or dict(LIMITS_DEFAULTS)


def _record_limit_audit(workspace: Path | str, kind: str, message: str, detail: dict[str, Any]) -> None:
    from . import audit

    try:
        audit.record(
            workspace,
            "limit.exceeded",
            who="system",
            target=kind,
            result="blocked",
            risk="high",
            detail={"reason": message[:300], **detail},
        )
    except (OSError, ValueError):
        pass


def check_and_record_action(
    workspace: Path | str,
    tool_name: str,
    *,
    record: bool = True,
    now: float | None = None,
) -> None:
    """Enforce per-minute action limit when ``limits.enabled`` is true."""
    cfg = _load_policy(workspace)
    limit = int(cfg.get("actions_per_minute") or 0)
    if not cfg.get("enabled") or limit <= 0:
        return
    if tool_name == "finish":
        return

    path = _state_path(workspace)
    lock = path.with_suffix(".lock")
    if path.is_symlink() or lock.is_symlink():
        raise LimitExceeded("Rate limit state symlink refused")

    current = time.time() if now is None else float(now)
    cutoff = current - 60.0
    with FileLock(str(lock), timeout=5):
        timestamps: list[float] = []
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("actions"), list):
                    timestamps = [float(t) for t in loaded["actions"] if isinstance(t, (int, float)) and float(t) > cutoff]
            except (OSError, ValueError, json.JSONDecodeError):
                timestamps = []
        if len(timestamps) >= limit:
            msg = f"rate limit exceeded: {limit} actions/minute ({len(timestamps)} in last 60s)"
            _record_limit_audit(workspace, "actions_per_minute", msg, {"tool": tool_name, "limit": limit})
            raise LimitExceeded(msg)
        if record:
            timestamps.append(current)
            _atomic_write(path, {"schema": 1, "actions": timestamps[-limit * 2:]})


def check_daily_caps(
    workspace: Path | str,
    *,
    session_tokens: int = 0,
    session_cost_usd: float = 0.0,
) -> None:
    """Enforce ``limits.daily_tokens`` and ``limits.daily_cost_usd`` when enabled."""
    cfg = _load_policy(workspace)
    if not cfg.get("enabled"):
        return

    token_cap = int(cfg.get("daily_tokens") or 0)
    cost_cap = float(cfg.get("daily_cost_usd") or 0.0)
    if token_cap <= 0 and cost_cap <= 0.0:
        return

    from . import costcenter

    costcenter.sync(workspace)
    today = costcenter.spend(workspace, costcenter._day_start())  # noqa: SLF001

    if token_cap > 0:
        used_tokens = int(today["input"]) + int(today["output"]) + max(0, int(session_tokens))
        if used_tokens >= token_cap:
            msg = f"daily token cap of {token_cap} reached ({used_tokens} tokens used today)"
            _record_limit_audit(workspace, "daily_tokens", msg, {"cap": token_cap, "used": used_tokens})
            raise LimitExceeded(msg)

    if cost_cap > 0.0:
        used_usd = float(today["usd"]) + max(0.0, float(session_cost_usd))
        if used_usd >= cost_cap - 1e-9:
            msg = f"daily cost cap of ${cost_cap:.2f} reached (${used_usd:.4f} spent today)"
            _record_limit_audit(workspace, "daily_cost_usd", msg, {"cap": cost_cap, "used": round(used_usd, 6)})
            raise LimitExceeded(msg)
