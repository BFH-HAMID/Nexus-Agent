"""Operator policy for the model council (``council.yaml``)."""
from __future__ import annotations

from .. import policyfile

NAME = "council.yaml"
LENSES = {
    "planner": "You judge whether the proposal achieves the goal with the fewest, clearest steps.",
    "skeptic": "You look for wrong assumptions, missing evidence and ways the proposal fails.",
    "safety": "You look only for irreversible, destructive, privacy or security harm and missing rollbacks.",
    "user_advocate": "You judge whether this is what the user actually asked for and would want.",
    "cost": "You judge time, money and token cost against the benefit.",
    "domain_expert": "You judge technical correctness in the relevant domain.",
}
DEFAULTS = {
    "schema": 1,
    "enabled": False,
    "members": [],
    "lenses": ["planner", "skeptic", "safety"],
    "judge": "",
    "rounds": 1,
    "quorum": 2,
    "allow_cloud": False,
    "max_tokens_per_call": 700,
    "max_calls": 12,
    "max_cost_usd": 0.25,
    "timeout_seconds": 120,
    "store_sessions": True,
    "keep_sessions": 50,
    "chief": {"review_high_risk_plans": False, "block_on_reject": True},
}


class CouncilPolicyError(policyfile.PolicyError):
    """council.yaml is invalid or the council is OFF."""


def _validate(cfg):
    if not cfg["lenses"]:
        raise CouncilPolicyError("council.yaml: give at least one lens")
    if cfg["enabled"]:
        if not 2 <= len(cfg["members"]) <= 5:
            raise CouncilPolicyError("council.yaml: an enabled council needs 2-5 members")
        if cfg["quorum"] > len(cfg["members"]):
            raise CouncilPolicyError("council.yaml: quorum cannot exceed the number of members")


POLICY = policyfile.Policy(
    NAME, DEFAULTS, label="Council", error=CouncilPolicyError, validate=_validate,
    ranges={"rounds": (0, 2), "quorum": (2, 5), "max_tokens_per_call": (100, 4000), "max_calls": (2, 40),
            "max_cost_usd": (0.0, 20.0), "timeout_seconds": (5, 900), "keep_sessions": (1, 500)},
    choices={"lenses": tuple(LENSES)},
    list_limits={"members": 5, "lenses": 6},
)
load = POLICY.load
require = POLICY.require
enabled = POLICY.enabled
digest = policyfile.digest
