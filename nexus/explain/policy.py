"""Operator policy for explainability and audit export (``explain.yaml``)."""
from __future__ import annotations

from .. import policyfile

NAME = "explain.yaml"
DEFAULTS = {
    "schema": 1,
    "enabled": False,
    "record": "non_safe",
    "max_evidence": 8,
    "voice": True,
    "export": {"max_events": 5000},
}


class ExplainPolicyError(policyfile.PolicyError):
    """explain.yaml is invalid or explainability is OFF."""


POLICY = policyfile.Policy(
    NAME, DEFAULTS, label="Explainability", error=ExplainPolicyError,
    ranges={"max_evidence": (1, 40), "export.max_events": (1, 50_000)},
    choices={"record": ("all", "non_safe", "denied", "off")},
)
load = POLICY.load
require = POLICY.require
enabled = POLICY.enabled
digest = policyfile.digest
