"""Presence state machine and the closed rule dispatcher.

States: ``unknown`` -> ``present`` | ``idle`` | ``away``.

* ``present``  recent keyboard/mouse input, a camera event, or a manual "home" mark
* ``idle``     no recent input, but a sign of you (known device, camera) within ``away_after_seconds``
* ``away``     no sign at all for ``away_after_seconds``, or a manual "away" mark
* ``unknown``  no enabled signal has produced evidence yet (never treated as away)

A candidate state must hold for ``debounce_seconds`` before it replaces the
current one.  Accepted changes are appended to a bounded, content-free history
and may fire rules from ``presence_rules.yaml``.  A rule can only invoke one of
the functions in ``PresenceEngine._act``: there is no tool registry, no model
and no command execution anywhere in this package.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import policyfile
from . import policy, signals

STATES = ("unknown", "present", "idle", "away")
BRIEF_TEXT = "Welcome back. Say 'brief me' for an update."
_EVENT = {("away", "present"): "arrive", ("away", "idle"): "arrive", ("present", "away"): "leave",
          ("idle", "away"): "leave", ("present", "idle"): "idle", ("idle", "present"): "active"}


class PresenceError(policy.PresencePolicyError):
    """A presence operation was refused."""


def _blank() -> dict[str, Any]:
    return {"schema": 1, "state": "unknown", "since": 0.0, "pending": None, "manual": None, "seen": {},
            "away_since": None, "history": [], "fired": {}, "proposals": []}


class PresenceEngine:
    def __init__(self, workspace: Path | str, *, clock: Callable[[], float] = time.time,
                 idle_probe: Callable[[], float | None] | None = None,
                 neighbor_reader: Callable[[], set[str] | None] | None = None,
                 actions: dict[str, Callable[..., Any]] | None = None,
                 localtime: Callable[[float], time.struct_time] = time.localtime) -> None:
        self.workspace = Path(workspace).resolve()
        self.cfg = policy.require(self.workspace)
        self.clock = clock
        self.idle_probe = idle_probe or signals.idle_seconds
        self.neighbor_reader = neighbor_reader or signals.neighbor_macs
        self.actions = dict(actions or {})
        self.localtime = localtime
        self.folder = policyfile.state_dir(self.workspace, signals.AREA)
        self.path = self.folder / "state.json"

    # -- state --------------------------------------------------------------------- #
    def _load(self) -> dict[str, Any]:
        data = policyfile.read_json(self.path, None, limit=600_000)
        if not isinstance(data, dict) or data.get("state") not in STATES:
            return _blank()
        return {**_blank(), **data}

    def _save(self, data: dict[str, Any]) -> None:
        keep = self.cfg["history"]
        cutoff = self.clock() - keep["keep_days"] * 86_400
        data["history"] = [row for row in data["history"] if row.get("at", 0) >= cutoff][-keep["keep_events"]:]
        data["proposals"] = data["proposals"][-20:]
        policyfile.write_json(self.path, data)

    # -- inputs ---------------------------------------------------------------------- #
    def signal(self, kind: str, *, now: float | None = None) -> bool:
        """Record evidence of presence from an event source (``vision``). False when that signal is OFF."""
        if kind != "vision" or not self.cfg["signals"]["vision"]:
            return False
        now = self.clock() if now is None else float(now)
        with policyfile.locked(self.path):
            data = self._load()
            data["seen"]["vision"] = now
            self._save(data)
        return True

    def set_manual(self, state: str, *, source: str = "cli") -> dict[str, Any]:
        """``home`` / ``away`` take effect at once; ``auto`` returns control to the signals."""
        if not self.cfg["signals"]["manual"]:
            raise PresenceError("Manual presence is OFF: set signals.manual: true in presence.yaml")
        if state not in {"home", "away", "auto"}:
            raise PresenceError("Presence can be set to home, away or auto")
        now = self.clock()
        hold = self.cfg["manual_hold_minutes"]
        with policyfile.locked(self.path):
            data = self._load()
            data["manual"] = None if state == "auto" else {"state": state, "at": now,
                                                          "until": now + hold * 60 if hold else None}
            if state == "home":
                data["seen"]["manual"] = now
            fired = self._apply(data, now, immediate=state != "auto", source="manual")
            self._save(data)
        policyfile.audit(self.workspace, "presence.manual", who=source, target=state, result="ok", risk="low")
        return {**self._view(data, now), "fired": fired}

    # -- evaluation -------------------------------------------------------------------- #
    def _candidate(self, data: dict[str, Any], now: float) -> tuple[str, str, list[str]]:
        """``(state, source, unavailable_signals)`` from the manual mark and the enabled signals."""
        cfg, seen, unavailable = self.cfg, data["seen"], []
        manual = data.get("manual")
        if manual and manual.get("until") is not None and now >= manual["until"]:
            data["manual"] = manual = None
        if cfg["signals"]["input_idle"]:
            idle = self.idle_probe()
            if idle is None:
                unavailable.append("input_idle")
            else:
                seen["input"] = now - max(0.0, float(idle))
        if cfg["signals"]["lan_devices"] and cfg["lan"]["devices"]:
            table = self.neighbor_reader()
            if table is None:
                unavailable.append("lan_devices")
            else:
                wanted = set(cfg["lan"]["devices"])
                if any(signals.device_hash(self.workspace, mac) in wanted for mac in table):
                    seen["lan"] = now
        if manual:
            return ("present" if manual["state"] == "home" else "away"), "manual", unavailable
        active = [(seen[key], key) for key in ("input", "vision", "manual") if key in seen
                  and (key != "vision" or cfg["signals"]["vision"])]
        around = active + ([(seen["lan"], "lan")] if "lan" in seen else [])
        if not around:
            return "unknown", "none", unavailable
        last_active, active_source = max(active) if active else (0.0, "")
        if active and now - last_active < cfg["idle_after_seconds"]:
            return "present", active_source, unavailable
        last_seen, seen_source = max(around)
        if now - last_seen < cfg["away_after_seconds"]:
            return "idle", seen_source, unavailable
        return "away", "timeout", unavailable

    def _apply(self, data: dict[str, Any], now: float, *, immediate: bool = False, source: str = "") -> list[dict[str, Any]]:
        candidate, why, unavailable = self._candidate(data, now)
        data["unavailable"] = unavailable
        current = data["state"]
        if candidate == current:
            data["pending"] = None
            return []
        pending = data.get("pending")
        if not immediate and why != "manual" and self.cfg["debounce_seconds"]:
            if not pending or pending.get("state") != candidate:
                data["pending"] = {"state": candidate, "since": now}
                return []
            if now - pending["since"] < self.cfg["debounce_seconds"]:
                return []
        data.update(state=candidate, since=now, pending=None)
        data["history"].append({"at": round(now, 1), "from": current, "to": candidate, "source": source or why})
        away_for = now - data["away_since"] if current == "away" and data.get("away_since") else 0.0
        data["away_since"] = now if candidate == "away" else None
        policyfile.audit(self.workspace, "presence.transition", who="presence", target=candidate, result="ok",
                         risk="low", detail={"from": current, "source": source or why})
        event = _EVENT.get((current, candidate))
        return self._fire(data, event, now, away_for) if event else []

    def tick(self, now: float | None = None) -> dict[str, Any]:
        """Read the signals once, update the state and fire due rules."""
        now = self.clock() if now is None else float(now)
        with policyfile.locked(self.path):
            data = self._load()
            fired = self._apply(data, now)
            self._save(data)
        return {**self._view(data, now), "fired": fired}

    # -- rules --------------------------------------------------------------------------- #
    def _in_window(self, between: str, now: float) -> bool:
        if not between:
            return True
        start, end = between.split("-")
        local = self.localtime(now)
        minute = local.tm_hour * 60 + local.tm_min
        lo, hi = (int(part[:2]) * 60 + int(part[3:]) for part in (start, end))
        return lo <= minute < hi if lo <= hi else (minute >= lo or minute < hi)

    def _fire(self, data: dict[str, Any], event: str, now: float, away_for: float) -> list[dict[str, Any]]:
        from ..computer_control import EmergencyStop

        try:
            rules = [rule for rule in policy.rules(self.workspace) if rule["when"] == event]
        except ValueError as exc:
            return [{"rule": "", "action": "", "ok": False, "detail": f"presence_rules.yaml invalid: {str(exc)[:120]}"}]
        out = []
        for rule in rules:
            row = {"rule": rule["id"], "action": rule["do"], "ok": False, "detail": ""}
            if EmergencyStop().stopped():
                row["detail"] = "emergency stop latched"
            elif away_for < rule["after_away_minutes"] * 60:
                row["detail"] = "away for less than after_away_minutes"
            elif not self._in_window(rule["between"], now):
                row["detail"] = "outside the rule's time window"
            elif now - float(data["fired"].get(rule["id"], -1e12)) < rule["cooldown_minutes"] * 60:
                row["detail"] = "rule cooldown"
            elif not policyfile.rate_allow(self.folder / "actions.rate.json", self.cfg["actions"]["max_per_hour"],
                                           3600, now=now):
                row["detail"] = "hourly action limit reached"
            else:
                try:
                    row["detail"] = str(self._act(rule, data, now) or "")[:160]
                    row["ok"] = True
                    data["fired"][rule["id"]] = now
                except Exception as exc:  # noqa: BLE001 - one failing action must not stop presence tracking
                    row["detail"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            policyfile.audit(self.workspace, "presence.action", who="presence", target=f"{rule['id']}:{rule['do']}",
                             result="ok" if row["ok"] else "skipped", risk="low",
                             detail={"event": event, "detail": row["detail"]})
            out.append(row)
        return out

    def _act(self, rule: dict[str, Any], data: dict[str, Any], now: float) -> str:
        """The complete set of things presence can do."""
        action = rule["do"]
        if action in self.actions:  # test / embedding hook for the same closed names
            return self.actions[action](rule)
        if action in {"notify", "brief"}:
            from ..notifications import NotificationRouter

            receipt = NotificationRouter(self.workspace, clock=lambda: now).notify(
                rule["text"] if action == "notify" else BRIEF_TEXT, importance=rule["importance"], title="Nexus presence")
            return f"{receipt.channel}: {'delivered' if receipt.delivered else receipt.reason}"
        if action in {"dnd_on", "dnd_off"}:
            from ..jarvis.proactive import DNDManager

            DNDManager(self.workspace, clock=lambda: now).set_focus_mode(
                action == "dnd_on", minutes=rule["dnd_minutes"] if action == "dnd_on" else None,
                reason=f"presence:{rule['id']}")
            return "do-not-disturb " + ("on" if action == "dnd_on" else "off")
        if action == "mark_present":
            from ..notifications import mark_user_present

            mark_user_present(self.workspace, channel="presence", at=now)
            return "notifications routed as present"
        if action == "mark_away":
            from ..notifications import clear_user_presence

            clear_user_presence(self.workspace)
            return "notifications routed as away"
        if action == "propose":
            data["proposals"].append({"id": policyfile.new_id("prop"), "at": round(now, 1), "rule": rule["id"],
                                      "text": rule["text"]})
            return "proposal recorded"
        raise PresenceError(f"Unknown presence action '{action}'")

    # -- views --------------------------------------------------------------------------- #
    def _view(self, data: dict[str, Any], now: float) -> dict[str, Any]:
        manual = data.get("manual")
        return {"state": data["state"], "since": data["since"], "for_seconds": round(max(0.0, now - data["since"]), 1)
                if data["since"] else None, "manual": manual["state"] if manual else "auto",
                "pending": (data.get("pending") or {}).get("state"), "unavailable": data.get("unavailable", []),
                "signals": sorted(key for key, on in self.cfg["signals"].items() if on),
                "proposals": len(data["proposals"])}

    def status(self) -> dict[str, Any]:
        return self._view(self._load(), self.clock())

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._load()["history"][-max(1, min(int(limit), 2000)):]

    def proposals(self) -> list[dict[str, Any]]:
        return list(self._load()["proposals"])

    def dismiss(self, ident: str = "") -> int:
        with policyfile.locked(self.path):
            data = self._load()
            before = len(data["proposals"])
            data["proposals"] = [row for row in data["proposals"] if ident and row["id"] != ident]
            self._save(data)
        return before - len(data["proposals"])

    def forget(self) -> int:
        """Erase the stored history (the current state is kept)."""
        with policyfile.locked(self.path):
            data = self._load()
            count = len(data["history"])
            data["history"] = []
            self._save(data)
        policyfile.audit(self.workspace, "presence.forget", who="operator", target="history", result="ok", risk="low",
                         detail={"removed": count})
        return count
