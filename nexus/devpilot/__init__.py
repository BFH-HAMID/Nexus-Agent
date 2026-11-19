"""Task 25 - DevPilot (opt-in, ``devpilot.yaml``).

A model proposes a patch, DevPilot applies it to a throw-away checkout of the
last commit, runs the project's checks there and repeats a few times. The real
working tree changes only through an approved ``apply``. DevPilot never
commits, pushes or opens a pull request. See ``docs/devpilot.md``.
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
    info.update(agent=cfg["agent"] or "(default)", allow_cloud=cfg["allow_cloud"], max_iterations=cfg["max_iterations"],
                apply_requires_passing_checks=cfg["apply"]["require_passing_checks"])
    try:
        from . import checks
        from .core import runs

        root = Path(workspace).resolve()
        for kind, command in checks.detect(root, cfg, root).items():
            info[f"{kind}_command"] = f"{command['display'] or '(none)'} [{command['source']}]"
        info["git_repository"] = (root / ".git").exists()
        recent = runs(root, 100)
        info["runs"] = len(recent)
        if recent:
            info["last_run"] = f"{recent[0]['id']} {recent[0]['status']}"
    except (ValueError, OSError) as exc:
        info["error"] = str(exc)[:240]
    return info
