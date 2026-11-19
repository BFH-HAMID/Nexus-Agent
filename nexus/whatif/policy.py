"""Operator policy for the what-if simulator (``whatif.yaml``)."""
from __future__ import annotations

from .. import policyfile

NAME = "whatif.yaml"
DEFAULTS = {
    "schema": 1,
    "enabled": False,
    "mode": "static",
    "agent": "",
    "allow_cloud": False,
    "max_tokens_per_call": 900,
    "max_steps": 40,
    "max_diff_lines": 60,
    "max_file_bytes": 400_000,
    "store_scenarios": True,
    "keep_scenarios": 50,
    "chief": {"simulate_before_approval": False, "hold_irreversible": True},
}


class WhatIfPolicyError(policyfile.PolicyError):
    """whatif.yaml is invalid or the simulator is OFF."""


POLICY = policyfile.Policy(
    NAME, DEFAULTS, label="What-if simulator", error=WhatIfPolicyError,
    ranges={"max_tokens_per_call": (100, 4000), "max_steps": (1, 200), "max_diff_lines": (5, 400),
            "max_file_bytes": (1000, 5_000_000), "keep_scenarios": (1, 500)},
    choices={"mode": ("static", "model")},
)
load = POLICY.load
require = POLICY.require
enabled = POLICY.enabled
digest = policyfile.digest
