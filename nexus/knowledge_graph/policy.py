"""Operator policy for the personal knowledge graph (``knowledge_graph.yaml``)."""
from __future__ import annotations

from .. import policyfile

NAME = "knowledge_graph.yaml"
CATEGORIES = ("credentials", "finance", "health")
DEFAULTS = {
    "schema": 1,
    "enabled": False,
    "encrypt_at_rest": True,
    "extraction": {"mode": "rules", "agent": "", "allow_cloud": False, "max_facts_per_text": 12},
    "sources": {"memory": True, "knowledge": False},
    "limits": {"max_entities": 5000, "max_relations": 20000, "max_hops": 3, "max_results": 25},
    "exclude_categories": ["credentials", "finance", "health"],
    "context": {"inject": False, "max_chars": 1200},
}


class GraphPolicyError(policyfile.PolicyError):
    """knowledge_graph.yaml is invalid or the graph is OFF."""


def _validate(cfg):
    if "credentials" not in cfg["exclude_categories"]:
        raise GraphPolicyError("knowledge_graph.yaml: 'credentials' must stay in exclude_categories")


POLICY = policyfile.Policy(
    NAME, DEFAULTS, label="Knowledge graph", error=GraphPolicyError, validate=_validate,
    ranges={"extraction.max_facts_per_text": (1, 50), "limits.max_entities": (10, 50_000),
            "limits.max_relations": (10, 200_000), "limits.max_hops": (1, 5),
            "limits.max_results": (1, 200), "context.max_chars": (200, 6000)},
    choices={"extraction.mode": ("rules", "model"), "exclude_categories": CATEGORIES},
)
load = POLICY.load
require = POLICY.require
enabled = POLICY.enabled
digest = policyfile.digest
