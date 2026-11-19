"""Operator policy for the translator (``translate.yaml``)."""
from __future__ import annotations

from .. import policyfile
from .detect import LANGUAGES

NAME = "translate.yaml"
DEFAULTS = {
    "schema": 1,
    "enabled": False,
    "agent": "",
    "allow_cloud": False,
    "default_target": "en",
    "targets": [],
    "max_chars": 4000,
    "max_tokens_per_call": 1500,
    "rate_per_hour": 120,
    "glossary": {},
    "interpreter": {"enabled": False, "pair": ["en", "bn"], "max_minutes": 30},
}


class TranslatePolicyError(policyfile.PolicyError):
    """translate.yaml is invalid or the translator is OFF."""


def _language(code, where: str) -> str:
    if not isinstance(code, str) or code not in LANGUAGES:
        raise TranslatePolicyError(f"translate.yaml: {where} must be a known language code such as en or bn")
    return code


def _validate(cfg) -> None:
    _language(cfg["default_target"], "'default_target'")
    for code in cfg["targets"]:
        _language(code, "'targets' entries")
    if cfg["targets"] and cfg["default_target"] not in cfg["targets"]:
        raise TranslatePolicyError("translate.yaml: 'default_target' must be one of 'targets'")
    pair = cfg["interpreter"]["pair"]
    if len(pair) != 2 or pair[0] == pair[1]:
        raise TranslatePolicyError("translate.yaml: 'interpreter.pair' must name two different languages")
    for code in pair:
        _language(code, "'interpreter.pair' entries")
        if cfg["targets"] and code not in cfg["targets"]:
            raise TranslatePolicyError("translate.yaml: 'interpreter.pair' languages must be in 'targets'")
    glossary = cfg["glossary"]
    if len(glossary) > 20:
        raise TranslatePolicyError("translate.yaml: 'glossary' has at most 20 languages")
    clean: dict[str, dict[str, str]] = {}
    for code, terms in glossary.items():
        _language(code, "'glossary' keys")
        if not isinstance(terms, dict) or len(terms) > 300:
            raise TranslatePolicyError(f"translate.yaml: 'glossary.{code}' must be a mapping of at most 300 terms")
        clean[code] = {}
        for source, target in terms.items():
            source = policyfile.clean_text(source, 80, label=f"translate.yaml: 'glossary.{code}' term",
                                           error=TranslatePolicyError).strip()
            target = policyfile.clean_text(target, 120, label=f"translate.yaml: 'glossary.{code}.{source}'",
                                           error=TranslatePolicyError).strip()
            if len(source) < 2 or not target or "\u27e6" in source + target or "\u27e7" in source + target:
                raise TranslatePolicyError(f"translate.yaml: 'glossary.{code}' has an empty or unusable term")
            clean[code][source] = target
    cfg["glossary"] = clean


POLICY = policyfile.Policy(
    NAME, DEFAULTS, label="Translator", error=TranslatePolicyError, validate=_validate, free=("glossary",),
    ranges={"max_chars": (50, 40_000), "max_tokens_per_call": (100, 8000), "rate_per_hour": (1, 5000),
            "interpreter.max_minutes": (1, 720)},
    list_limits={"targets": 40, "interpreter.pair": 2}, limit=64_000,
)
load = POLICY.load
require = POLICY.require
enabled = POLICY.enabled
