"""Task 22 - home presence (opt-in, ``presence.yaml`` + ``presence_rules.yaml``).

Derives present / idle / away from coarse signals and maps state changes to a
closed set of actions.  Distinct from the HUD hand-off feature in
``nexus/hud/presence.py`` (``hud.yaml``).  See ``docs/home-presence.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import policy


def status(workspace: Path | str) -> dict[str, Any]:
    info = policy.POLICY.describe(workspace)
    if not info.get("enabled"):
        return info
    try:
        from .engine import PresenceEngine

        info.update(PresenceEngine(workspace).status())
        rules = policy.RULES.load(workspace)
        info.update(rules_enabled=rules["enabled"], rules=len(rules["rules"]) if rules["enabled"] else 0)
        if info.get("unavailable"):
            info["error"] = "signal(s) enabled but unavailable on this machine: " + ", ".join(info["unavailable"])
    except (ValueError, OSError) as exc:
        info["error"] = str(exc)[:240]
    return info
