"""Task 20 - what-if simulator (opt-in, ``whatif.yaml``).

Predicts the effects of a tool call, a shell command or a Chief plan without
executing anything.  See ``docs/whatif.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import policy


def status(workspace: Path | str) -> dict[str, Any]:
    info = policy.POLICY.describe(workspace)
    if not info.get("enabled"):
        return info
    cfg = policy.load(workspace)
    info.update(mode=cfg["mode"], simulate_before_approval=cfg["chief"]["simulate_before_approval"],
                hold_irreversible=cfg["chief"]["hold_irreversible"], executes=False)
    try:
        from .core import scenarios

        info["scenarios"] = len(scenarios(workspace, 200))
    except (ValueError, OSError) as exc:
        info["error"] = str(exc)[:240]
    return info
