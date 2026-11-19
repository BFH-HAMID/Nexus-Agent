"""Council review of a Chief-of-Staff plan (opt-in: ``council.yaml`` ``chief.review_high_risk_plans``)."""
from __future__ import annotations

import json
from typing import Any

from . import policy

HIGH = frozenset({"high", "critical"})


def review(chief: Any, mission: Any) -> dict[str, Any] | None:
    """Return ``{"kind", "line", "hold", "detail"}`` for the approval card, or ``None`` when not applicable.

    Fail-closed: when the operator asked for a review and it can not be
    produced, the plan is held for an explicit human decision.
    """
    try:
        cfg = policy.load(chief.workspace)
    except ValueError:
        return None
    if not cfg["enabled"] or not cfg["chief"]["review_high_risk_plans"]:
        return None
    if not any(step.risk_level in HIGH for step in mission.steps):
        return None
    from .core import Council

    plan = [{"id": s.id, "title": s.title, "role": s.role, "risk": s.risk_level, "read_only": s.read_only,
             "depends_on": list(s.depends_on), "kind": s.kind} for s in mission.steps]
    try:
        record = Council(chief.workspace, chief.config, chief.providers).deliberate(
            "Should this plan be approved to run as written?",
            context=json.dumps({"goal": mission.goal, "success_criteria": mission.success_criteria, "steps": plan,
                                "estimated_cost_usd": mission.estimated_cost_usd}, ensure_ascii=False),
            kind="plan", subject=mission.id)
    except Exception as exc:  # noqa: BLE001 - fail closed, never break planning
        return {"kind": "council", "hold": True, "line": "Council review unavailable; explicit approval required.",
                "detail": {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}}
    votes = record["votes"]
    concern = next((item for item in record["dissent"]), "") if record["decision"] == "approve" else record["summary"]
    line = (f"Council: {record['decision'].replace('_', ' ')} ({votes['approve']} approve, {votes['revise']} revise, "
            f"{votes['reject']} reject)." + (f" {concern[:220]}" if concern else ""))
    return {"kind": "council", "line": line, "hold": record["decision"] != "approve" and cfg["chief"]["block_on_reject"],
            "detail": {"session": record["id"], "decision": record["decision"], "votes": votes,
                       "cost_usd": record["usage"]["cost_usd"]}}
