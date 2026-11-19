"""Opt-in plan reviews attached to the Chief-of-Staff approval card (Tasks 19 and 20).

``attach_plan_reviews`` runs right after a plan is built.  A review may add one
sentence to ``mission.plan_summary`` and may place a *hold*: the plan then needs
an explicit human GO even when autonomy or an up-front ``--confirm GO`` would
have started it.  Reviews can never approve, start, reorder or edit a plan, and
with ``whatif.yaml`` / ``council.yaml`` absent this module does nothing.
"""
from __future__ import annotations

from typing import Any

HOLD_EVENT = "review.hold"


def attach_plan_reviews(chief: Any, mission: Any, *, persist: bool = True) -> list[dict[str, Any]]:
    """Run the enabled reviews once per mission; returns the attached review rows."""
    if any(str(entry.get("event", "")).startswith("review.") for entry in mission.logs):
        return []
    rows: list[dict[str, Any]] = []
    reviewers = []
    try:
        from .whatif import chief as whatif_chief

        reviewers.append(whatif_chief.review)
    except ImportError:  # pragma: no cover - package always ships
        pass
    if persist:  # previews never spend model calls on a council
        try:
            from .council import chief as council_chief

            reviewers.append(council_chief.review)
        except ImportError:  # pragma: no cover
            pass
    for reviewer in reviewers:
        try:
            row = reviewer(chief, mission)
        except Exception as exc:  # noqa: BLE001 - a broken review holds the plan instead of breaking planning
            row = {"kind": getattr(reviewer, "__module__", "review").split(".")[-2], "hold": True,
                   "line": "A configured plan review failed; explicit approval required.",
                   "detail": {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}}
        if not row:
            continue
        rows.append(row)
        if row.get("line"):
            mission.plan_summary = f"{mission.plan_summary} {row['line']}".strip()
        mission.log(f"review.{row['kind']}", hold=bool(row.get("hold")), **(row.get("detail") or {}))
        if row.get("hold"):
            mission.log(HOLD_EVENT, by=row["kind"])
            chief._audit("chief.review_hold", mission.id, result="held", risk="medium", by=row["kind"])
    return rows


def plan_review_hold(mission: Any) -> bool:
    """True when a review asked for an explicit human decision on this plan."""
    return any(entry.get("event") == HOLD_EVENT for entry in mission.logs)
