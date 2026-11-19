"""Operator policy for the meeting assistant (``meetings.yaml``)."""
from __future__ import annotations

from .. import policyfile

NAME = "meetings.yaml"
DEFAULTS = {
    "schema": 1,
    "enabled": False,
    "encrypt_at_rest": True,
    "retention_days": 30,
    "consent_notice": "Notes are being taken for this meeting.",
    "limits": {"max_meetings": 200, "max_transcript_chars": 400_000, "max_import_bytes": 2_000_000},
    "summary": {"mode": "extractive", "agent": "", "allow_cloud": False, "max_tokens_per_call": 900, "max_points": 6},
    "audio": {"enabled": False, "model": "small", "language": "auto", "max_megabytes": 200},
    "agent_tools": False,
}


class MeetingsPolicyError(policyfile.PolicyError):
    """meetings.yaml is invalid or the meeting assistant is OFF."""


def _validate(cfg):
    if not cfg["consent_notice"].strip():
        raise MeetingsPolicyError("meetings.yaml: 'consent_notice' must not be empty")


POLICY = policyfile.Policy(
    NAME, DEFAULTS, label="Meeting assistant", error=MeetingsPolicyError, validate=_validate,
    ranges={"retention_days": (0, 3650), "limits.max_meetings": (1, 2000),
            "limits.max_transcript_chars": (1000, 2_000_000), "limits.max_import_bytes": (1000, 20_000_000),
            "summary.max_tokens_per_call": (200, 4000), "summary.max_points": (1, 20),
            "audio.max_megabytes": (1, 2000)},
    choices={"summary.mode": ("extractive", "model")},
)
load = POLICY.load
require = POLICY.require
enabled = POLICY.enabled
