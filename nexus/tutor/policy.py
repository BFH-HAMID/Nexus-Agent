"""Operator policy for the tutor (``tutor.yaml``)."""
from __future__ import annotations

from typing import Any

from .. import policyfile

NAME = "tutor.yaml"
GRADES = ("again", "hard", "good", "easy")
DEFAULTS = {
    "schema": 1,
    "enabled": False,
    "encrypt_at_rest": False,
    "schedule": {"new_per_day": 10, "reviews_per_day": 100, "starting_ease": 2.5, "first_interval_days": 1,
                 "second_interval_days": 6, "max_interval_days": 365},
    "quiz": {"questions": 10},
    "limits": {"max_decks": 50, "max_cards": 5000, "max_card_chars": 600, "max_import_bytes": 500_000},
    "model": {"grade": False, "generate": False, "agent": "", "allow_cloud": False, "max_tokens_per_call": 900,
              "calls_per_hour": 120},
    "voice": {"enabled": False, "idle_minutes": 3},
    "agent_tools": False,
}


class TutorPolicyError(policyfile.PolicyError):
    """tutor.yaml is invalid or the tutor is OFF."""


def _validate(cfg: dict[str, Any]) -> None:
    schedule = cfg["schedule"]
    if schedule["second_interval_days"] < schedule["first_interval_days"]:
        raise TutorPolicyError("tutor.yaml: 'schedule.second_interval_days' must not be shorter than 'first_interval_days'")
    if schedule["max_interval_days"] < schedule["second_interval_days"]:
        raise TutorPolicyError("tutor.yaml: 'schedule.max_interval_days' must not be shorter than 'second_interval_days'")


POLICY = policyfile.Policy(
    NAME, DEFAULTS, label="Tutor", error=TutorPolicyError, validate=_validate,
    ranges={"schedule.new_per_day": (0, 200), "schedule.reviews_per_day": (1, 2000), "schedule.starting_ease": (1.3, 3.5),
            "schedule.first_interval_days": (1, 30), "schedule.second_interval_days": (1, 60),
            "schedule.max_interval_days": (7, 3650), "quiz.questions": (1, 100), "limits.max_decks": (1, 200),
            "limits.max_cards": (10, 50_000), "limits.max_card_chars": (40, 2000),
            "limits.max_import_bytes": (1000, 5_000_000), "model.max_tokens_per_call": (100, 4000),
            "model.calls_per_hour": (1, 5000),
            "voice.idle_minutes": (1, 30)},
    limit=32_000,
)
load = POLICY.load
require = POLICY.require
enabled = POLICY.enabled
