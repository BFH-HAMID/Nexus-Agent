"""Operator policies for home presence (``presence.yaml`` and ``presence_rules.yaml``)."""
from __future__ import annotations

import re
from typing import Any

from .. import policyfile

NAME = "presence.yaml"
RULES_NAME = "presence_rules.yaml"
EVENTS = ("arrive", "leave", "idle", "active")
ACTIONS = ("notify", "brief", "dnd_on", "dnd_off", "mark_present", "mark_away", "propose")
IMPORTANCE = ("low", "medium", "high")
_WINDOW = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")
_HASH = re.compile(r"^[0-9a-f]{32}$")
_RULE_KEYS = {"id", "when", "do", "text", "importance", "after_away_minutes", "between", "cooldown_minutes",
              "dnd_minutes"}

DEFAULTS = {
    "schema": 1,
    "enabled": False,
    "signals": {"manual": True, "input_idle": False, "vision": False, "lan_devices": False},
    "idle_after_seconds": 300,
    "away_after_seconds": 1800,
    "debounce_seconds": 20,
    "manual_hold_minutes": 720,
    "lan": {"devices": []},
    "history": {"keep_events": 200, "keep_days": 14},
    "actions": {"max_per_hour": 6},
}
RULES_DEFAULTS = {"schema": 1, "enabled": False, "rules": []}


class PresencePolicyError(policyfile.PolicyError):
    """presence.yaml / presence_rules.yaml is invalid or presence is OFF."""


def _validate(cfg: dict[str, Any]) -> None:
    if cfg["away_after_seconds"] <= cfg["idle_after_seconds"]:
        raise PresencePolicyError("presence.yaml: away_after_seconds must be greater than idle_after_seconds")
    if any(not _HASH.match(item) for item in cfg["lan"]["devices"]):
        raise PresencePolicyError("presence.yaml: lan.devices takes hashes from `nexus presence device-hash`, "
                                  "never raw addresses")


def _int(rule: dict[str, Any], key: str, low: int, high: int, default: int) -> int:
    value = rule.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise PresencePolicyError(f"presence_rules.yaml: rule '{rule.get('id')}' {key} must be between {low} and {high}")
    return value


def _validate_rules(cfg: dict[str, Any]) -> None:
    rules = cfg["rules"]
    if len(rules) > 20:
        raise PresencePolicyError("presence_rules.yaml: at most 20 rules")
    seen: set[str] = set()
    clean = []
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - _RULE_KEYS:
            unknown = sorted(set(rule) - _RULE_KEYS)[0] if isinstance(rule, dict) else "?"
            raise PresencePolicyError(f"presence_rules.yaml: unknown setting '{unknown}' in a rule")
        ident = rule.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", ident) or ident in seen:
            raise PresencePolicyError("presence_rules.yaml: every rule needs a unique id (a-z, 0-9, - and _)")
        seen.add(ident)
        if rule.get("when") not in EVENTS:
            raise PresencePolicyError(f"presence_rules.yaml: rule '{ident}' when must be one of {', '.join(EVENTS)}")
        if rule.get("do") not in ACTIONS:
            raise PresencePolicyError(f"presence_rules.yaml: rule '{ident}' do must be one of {', '.join(ACTIONS)}")
        text = policyfile.clean_text(rule.get("text", ""), 200, label=f"presence_rules.yaml: rule '{ident}' text",
                                     error=PresencePolicyError)
        if rule["do"] in {"notify", "propose"} and not text.strip():
            raise PresencePolicyError(f"presence_rules.yaml: rule '{ident}' needs text")
        between = rule.get("between", "")
        if between and not (isinstance(between, str) and _WINDOW.match(between)):
            raise PresencePolicyError(f"presence_rules.yaml: rule '{ident}' between must look like \"06:00-23:00\"")
        if rule.get("importance", "low") not in IMPORTANCE:
            raise PresencePolicyError(f"presence_rules.yaml: rule '{ident}' importance must be one of low, medium, high")
        clean.append({"id": ident, "when": rule["when"], "do": rule["do"], "text": text.strip(),
                      "importance": rule.get("importance", "low"), "between": between,
                      "after_away_minutes": _int(rule, "after_away_minutes", 0, 10_080, 0),
                      "cooldown_minutes": _int(rule, "cooldown_minutes", 0, 1440, 30),
                      "dnd_minutes": _int(rule, "dnd_minutes", 1, 1440, 60)})
    cfg["rules"] = clean


POLICY = policyfile.Policy(
    NAME, DEFAULTS, label="Home presence", error=PresencePolicyError, validate=_validate,
    ranges={"idle_after_seconds": (30, 86_400), "away_after_seconds": (60, 172_800), "debounce_seconds": (0, 600),
            "manual_hold_minutes": (0, 10_080), "history.keep_events": (10, 2000), "history.keep_days": (1, 365),
            "actions.max_per_hour": (0, 60)},
    list_limits={"lan.devices": 8},
)
RULES = policyfile.Policy(RULES_NAME, RULES_DEFAULTS, label="Presence rules", error=PresencePolicyError,
                          free=("rules",), validate=_validate_rules)
load = POLICY.load
require = POLICY.require
enabled = POLICY.enabled
digest = policyfile.digest


def rules(workspace) -> list[dict[str, Any]]:
    """Validated rules, or ``[]`` while presence_rules.yaml is OFF. Raises on an invalid file."""
    cfg = RULES.load(workspace)
    return cfg["rules"] if cfg["enabled"] else []
