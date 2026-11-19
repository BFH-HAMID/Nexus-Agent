"""Never-raising glue: put devices in their safe state when the emergency stop is latched."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import policy


def emergency_stop(workspace: Path | str, *, source: str = "stop") -> list[dict[str, Any]]:
    """Send every device's ``safe`` command (no-op while hardware.yaml is OFF or ``safe_on_stop`` is false)."""
    try:
        if not policy.enabled(workspace, "safe_on_stop"):
            return []
        from .core import Hardware

        return Hardware(workspace).safe(source=source)
    except Exception as exc:  # noqa: BLE001 - the stop itself must always complete
        return [{"device": "", "command": "", "ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}]
