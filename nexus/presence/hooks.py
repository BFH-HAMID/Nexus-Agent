"""Never-raising glue for existing subsystems. Everything is a no-op while ``presence.yaml`` is OFF."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import policy

_VISION_KINDS = {"owner_greeting"}


def vision_event(workspace: Path | str, event: Any) -> bool:
    """Called by the camera pipeline for each event it emits; counts owner / person sightings."""
    try:
        if not isinstance(event, dict) or not policy.enabled(workspace, "signals.vision"):
            return False
        kind = event.get("kind")
        if kind not in _VISION_KINDS and not (kind == "object" and event.get("label") == "person"):
            return False
        from .engine import PresenceEngine

        return PresenceEngine(workspace).signal("vision")
    except Exception:  # noqa: BLE001 - presence must never break the camera loop
        return False


def daemon_tick(workspace: Path | str, *, now: float | None = None) -> dict[str, Any] | None:
    """One presence evaluation for the Jarvis daemon cycle; ``None`` while presence is OFF."""
    try:
        if not policy.enabled(workspace):
            return None
        from .engine import PresenceEngine

        result = PresenceEngine(workspace).tick(now)
        return {"state": result["state"], "fired": [row["rule"] for row in result["fired"] if row["ok"]]}
    except Exception as exc:  # noqa: BLE001 - a presence fault must not stop the daemon
        return {"state": "unknown", "fired": [], "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
