"""Mission and agent watchdogs: stuck steps, infinite tool loops, and runaway cost."""

from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .autonomy import load_autonomy_dict
from .notifications import NotificationRouter


def tool_call_signature(tool_name: str, args: dict[str, Any] | None) -> str:
    payload = json.dumps(
        {"tool": (tool_name or "").strip(), "args": args or {}},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class WatchdogTripped(Exception):
    kind: str  # "stuck_step" | "infinite_loop" | "runaway_cost"
    reason: str
    step_id: str = ""

    def __str__(self) -> str:
        return self.reason


class InfiniteLoopDetector:
    """Detect repeated identical ``(tool, args)`` calls inside an agent run."""

    def __init__(self, max_identical: int = 3) -> None:
        self.max_identical = max(2, int(max_identical))
        self._last_sig: str = ""
        self._consecutive: int = 0
        self._recent: deque[str] = deque(maxlen=max(6, self.max_identical * 3))

    def observe(self, tool_name: str, args: dict[str, Any] | None) -> str | None:
        sig = tool_call_signature(tool_name, args)
        if sig == self._last_sig:
            self._consecutive += 1
        else:
            self._last_sig = sig
            self._consecutive = 1
        self._recent.append(sig)
        window_count = sum(1 for item in self._recent if item == sig)
        if self._consecutive >= self.max_identical:
            return (
                f"watchdog: infinite loop detected — '{tool_name}' called "
                f"{self._consecutive} times in a row with identical arguments"
            )
        if window_count >= max(self.max_identical + 1, 4):
            return (
                f"watchdog: infinite loop detected — '{tool_name}' repeated "
                f"{window_count} times with identical arguments in recent calls"
            )
        return None


def notify_watchdog_trigger(
    workspace: Path | str,
    *,
    mission_id: str,
    kind: str,
    reason: str,
    step_id: str = "",
    notify_router: NotificationRouter | None = None,
) -> None:
    """Send an immediate high-importance notification when a watchdog stops a mission."""
    try:
        policy = load_autonomy_dict(workspace)
        if not (policy.get("watchdog") or {}).get("notify_on_trigger", True):
            return
    except (ValueError, OSError):
        pass
    try:
        router = notify_router or NotificationRouter(workspace)
        title = f"Nexus watchdog stopped {mission_id} ({kind})"
        detail = f"[{step_id}] {reason}" if step_id else reason
        router.notify(importance="critical", title=title, message=detail[:240])
    except Exception:  # pragma: no cover - defensive against broken external channels
        pass


def check_stuck_missions(
    workspace: Path | str,
    *,
    policy: dict[str, Any] | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Inspect running missions on disk/memory for stuck steps beyond configured timeout.

    Only acts when ``autonomy.yaml`` ``watchdog.enabled`` is ``true``.
    """
    from .chief import _get_runtime, list_missions, save_mission

    root = Path(workspace).resolve()
    eff_policy = policy if policy is not None else load_autonomy_dict(root)
    wd = eff_policy.get("watchdog") or {}
    if not wd.get("enabled", False):
        return []
    timeout_s = float(wd.get("step_timeout_seconds", 300.0))
    now_ts = time.time() if now is None else float(now)
    tripped: list[dict[str, Any]] = []

    for mission in list_missions(root, limit=50):
        if mission.status != "running":
            continue
        rt = _get_runtime(root, mission.id)
        for step in mission.steps:
            if step.status != "running":
                continue
            ref = float(step.last_progress_at or step.started_at or mission.started_at or mission.updated_at or now_ts)
            elapsed = now_ts - ref
            if elapsed < timeout_s:
                continue
            reason = (
                f"Watchdog: stuck step '{step.id}' made no progress for "
                f"{elapsed:.2f}s (timeout {timeout_s:.2f}s)"
            )
            if rt is not None:
                rt.cancel_event.set()
            step.status = "failed"
            step.finished_at = now_ts
            step.error = reason[:500]
            mission.status = "failed"
            mission.watchdog_reason = "stuck_step"
            mission.error = reason[:500]
            mission.finished_at = now_ts
            mission.log(
                "watchdog.triggered",
                kind="stuck_step",
                step_id=step.id,
                elapsed_seconds=round(elapsed, 3),
                timeout_seconds=timeout_s,
            )
            save_mission(root, mission)
            notify_watchdog_trigger(
                root,
                mission_id=mission.id,
                kind="stuck_step",
                reason=reason,
                step_id=step.id,
            )
            tripped.append({
                "mission_id": mission.id,
                "step_id": step.id,
                "kind": "stuck_step",
                "reason": reason,
            })
            break
    return tripped


__all__ = [
    "InfiniteLoopDetector",
    "WatchdogTripped",
    "check_stuck_missions",
    "notify_watchdog_trigger",
    "tool_call_signature",
]
