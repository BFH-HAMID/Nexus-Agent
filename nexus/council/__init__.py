"""Task 19 - model council (opt-in, ``council.yaml``).

Several agents answer independently through different lenses, optionally
critique each other, and a deterministic tally produces an advisory verdict.
See ``docs/council.md``.
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
    info.update(members=list(cfg["members"]), lenses=list(cfg["lenses"]), rounds=cfg["rounds"],
                quorum=cfg["quorum"], max_calls=cfg["max_calls"], max_cost_usd=cfg["max_cost_usd"],
                reviews_high_risk_plans=cfg["chief"]["review_high_risk_plans"], allow_cloud=cfg["allow_cloud"])
    try:
        from .core import sessions

        info["sessions"] = len(sessions(workspace, 200))
    except (ValueError, OSError) as exc:
        info["error"] = str(exc)[:240]
    return info
