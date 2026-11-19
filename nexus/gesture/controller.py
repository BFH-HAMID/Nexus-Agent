"""Turn a stream of classified poses into at most a few deliberate actions.

A pose must be held (``hold_ms``), released before it can fire again, and -
when arming is required - preceded by the arming pose within the window.
Actions come from a closed table (``GestureController._act``).  Rules that are
not configurable:

* ``emergency_stop`` fires as soon as it is held: no arming, cooldown or rate
  limit applies, and there is no action that resumes from a stop;
* ``approve`` never applies to a DANGEROUS action, a tainted session, a session
  that requires speaker verification, or while the stop is latched;
* a gesture resolves only the one approval that is pending right now, once.
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import policyfile
from . import policy

_RANK = {"SAFE": 0, "MEDIUM": 1, "DANGEROUS": 2}


class GestureError(policy.GesturePolicyError):
    """Gesture control could not start."""


@dataclass
class Sinks:
    """What the surrounding process lets gestures reach. Everything is optional."""

    approvals: Any = None                                   # voice SpokenApprovalManager (in-process only)
    tainted: Callable[[], bool] = lambda: False             # has untrusted content been read this session?
    stop_speaking: Callable[[], Any] | None = None          # barge-in on the voice pipeline


class GestureController:
    def __init__(self, workspace: Path | str, *, sinks: Sinks | None = None,
                 clock: Callable[[], float] = time.monotonic, dry_run: bool = False) -> None:
        self.workspace = Path(workspace).resolve()
        self.cfg = policy.require(self.workspace)
        self.bindings = policy.bindings(self.workspace, self.cfg)
        self.sinks = sinks or Sinks()
        self.clock = clock
        self.dry_run = dry_run
        self._pose: str | None = None
        self._since = 0.0
        self._latched: str | None = None
        self._armed_until = -1.0
        self._cooldown_until = -1.0
        self._fired: deque[float] = deque()
        self.events: deque[dict[str, Any]] = deque(maxlen=50)

    @property
    def armed(self) -> bool:
        return self.clock() < self._armed_until

    def feed(self, pose: str | None, confidence: float = 1.0, now: float | None = None) -> dict[str, Any] | None:
        """One classified frame in; an event dict out when something fired, armed or was refused."""
        now = self.clock() if now is None else float(now)
        if pose not in policy.POSES or confidence < self.cfg["min_confidence"]:
            self._pose = self._latched = None
            return None
        if pose != self._pose:
            self._pose, self._since = pose, now
            if self._latched != pose:
                self._latched = None
            return None
        if now - self._since < self.cfg["hold_ms"] / 1000.0 or self._latched == pose:
            return None
        self._latched = pose
        action = self.bindings.get(pose, "none")
        arming = self.cfg["arming"]
        if action == "emergency_stop":
            return self._finish(pose, action, now, *self._act(action))
        if now < self._cooldown_until:
            return self._event(pose, action, False, "cooldown")
        if arming["required"] and pose == arming["gesture"]:
            self._armed_until = now + arming["window_seconds"]
            return self._event(pose, "arm", True, f"armed for {arming['window_seconds']}s")
        if action == "none":
            return None
        if arming["required"] and now >= self._armed_until:
            return self._event(pose, action, False, "not armed: show the arming pose first")
        while self._fired and now - self._fired[0] >= 60.0:
            self._fired.popleft()
        if len(self._fired) >= self.cfg["max_per_minute"]:
            return self._event(pose, action, False, "rate limit")
        self._armed_until = -1.0
        self._cooldown_until = now + self.cfg["cooldown_ms"] / 1000.0
        self._fired.append(now)
        return self._finish(pose, action, now, *self._act(action))

    # -- bookkeeping ---------------------------------------------------------------- #
    def _event(self, pose: str, action: str, ok: bool, detail: str) -> dict[str, Any]:
        event = {"pose": pose, "action": action, "ok": ok, "detail": detail, "dry_run": self.dry_run}
        self.events.append(event)
        return event

    def _finish(self, pose: str, action: str, now: float, ok: bool, detail: str) -> dict[str, Any]:
        policyfile.audit(self.workspace, "gesture.action", who="gesture", target=action,
                         result=("dry-run" if self.dry_run else "ok") if ok else "refused",
                         risk="high" if action in {"approve", "emergency_stop"} else "low",
                         detail={"pose": pose, "detail": detail[:160]})
        return self._event(pose, action, ok, detail)

    # -- the complete action surface --------------------------------------------------- #
    def _act(self, action: str) -> tuple[bool, str]:
        from ..computer_control import EmergencyStop

        if self.dry_run:
            return True, f"would run {action}"
        try:
            if action == "emergency_stop":
                EmergencyStop().set(True)
                from ..hardware import hooks as hardware_hooks  # opt-in; a no-op while hardware.yaml is OFF

                safe = hardware_hooks.emergency_stop(self.workspace, source="gesture")
                return True, ("emergency stop latched; resume from a terminal: nexus resume"
                              + (f"; {sum(1 for row in safe if row['ok'])}/{len(safe)} device(s) set safe" if safe else ""))
            if action == "stop_speaking":
                if self.sinks.stop_speaking is None:
                    return False, "no voice session in this process"
                self.sinks.stop_speaking()
                return True, "speech interrupted"
            if action in {"focus_on", "focus_off"}:
                from ..jarvis.proactive import DNDManager

                DNDManager(self.workspace).set_focus_mode(action == "focus_on", reason="gesture")
                return True, "focus mode " + ("on" if action == "focus_on" else "off")
            if action in {"approve", "deny"}:
                return self._answer(action == "approve")
        except Exception as exc:  # noqa: BLE001 - a failing sink must not kill the camera loop
            return False, f"{type(exc).__name__}: {str(exc)[:120]}"
        return False, f"unknown action '{action}'"

    def _answer(self, approve: bool) -> tuple[bool, str]:
        from ..autonomy import classify_action_risk
        from ..computer_control import EmergencyStop

        manager = self.sinks.approvals
        item = getattr(manager, "pending", None) if manager is not None else None
        if item is None:
            return False, "nothing is waiting for approval in this process"
        if not approve:
            done = manager.decide_pending(item, False, reason="Cancelled by gesture")
            return done, "denied" if done else "the request had already been resolved"
        rule = self.cfg["approve"]
        risk = classify_action_risk(item.spec, item.args).value
        if not rule["enabled"]:
            return False, "approving by gesture is off (gesture.yaml approve.enabled)"
        if _RANK.get(risk, 2) > _RANK[rule["max_risk"]]:
            return False, f"{risk} actions can not be approved by gesture; answer by voice or keyboard"
        if self.sinks.tainted():
            return False, "untrusted content was read in this session; answer by voice or keyboard"
        if getattr(manager, "verify_speaker", None) is not None or getattr(manager, "require_second_factor", False):
            return False, "this session requires speaker or second-factor verification"
        if EmergencyStop().stopped():
            return False, "emergency stop is latched"
        done = manager.decide_pending(item, True, reason="Confirmed by gesture (operator opt-in)")
        return done, f"approved {item.spec.name} ({risk})" if done else "the request had already been resolved"
