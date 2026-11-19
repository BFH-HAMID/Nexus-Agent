"""Task 26 - meeting assistant (opt-in, ``meetings.yaml``).

Consented meeting notes: dictated or typed notes and imported transcripts,
stored sealed, summarised offline with action items, deleted after the
retention period.  Nothing is ever sent anywhere automatically.
See ``docs/meetings.md``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from . import policy


def status(workspace: Path | str) -> dict[str, Any]:
    """Never-raising summary for ``nexus meetings status``, ``doctor`` and the HUD."""
    info = policy.POLICY.describe(workspace)
    if not info.get("enabled"):
        return info
    try:
        from .core import Meetings

        cfg = policy.load(workspace)
        info.update(summary=cfg["summary"]["mode"], retention_days=cfg["retention_days"],
                    encrypt_at_rest=cfg["encrypt_at_rest"], audio=cfg["audio"]["enabled"],
                    agent_tools=cfg["agent_tools"], sends_automatically=False)
        if cfg["audio"]["enabled"] and importlib.util.find_spec("faster_whisper") is None:
            info["missing"] = ["faster-whisper"]
        meetings = Meetings(workspace)
        rows = meetings.rows()
        info.update(meetings=len(rows), open=next((row["id"] for row in rows if row["status"] == "open"), None))
        problem = meetings.index.problem()
        if problem:
            info["error"] = f"meetings cannot be sealed: {problem}"[:240]
    except (ValueError, OSError) as exc:
        info["error"] = str(exc)[:240]
    return info
