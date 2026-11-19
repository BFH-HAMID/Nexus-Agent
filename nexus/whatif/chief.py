"""What-if line on the Chief-of-Staff approval card (opt-in: ``whatif.yaml`` ``chief.simulate_before_approval``)."""
from __future__ import annotations

from typing import Any

from . import policy


def review(chief: Any, mission: Any) -> dict[str, Any] | None:
    try:
        cfg = policy.load(chief.workspace)
    except ValueError:
        return None
    if not cfg["enabled"] or not cfg["chief"]["simulate_before_approval"]:
        return None
    from .core import WhatIf

    record = WhatIf(chief.workspace, config=chief.config).mission(mission)
    result = record["result"]
    counts = result["counts"]
    risky = counts["irreversible"] + counts["external"]
    line = (f"What-if: {counts['irreversible']} irreversible, {counts['external']} external of {counts['steps']} steps. "
            f"Worst case: {result['worst_case']}.") if risky else "What-if: local, reversible changes only."
    return {"kind": "whatif", "line": line, "hold": bool(risky and cfg["chief"]["hold_irreversible"]),
            "detail": {"scenario": record["id"], "risk_score": result["risk_score"], **counts}}
