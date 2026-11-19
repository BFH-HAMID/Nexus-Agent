"""Task 18 - personal knowledge graph (opt-in, ``knowledge_graph.yaml``).

Links facts the user explicitly stated into entities and relations, answers
"what do you know about X" / "how is X related to Y", and forgets together with
long-term memory.  See ``docs/knowledge-graph.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import policy


def status(workspace: Path | str) -> dict[str, Any]:
    """Never-raising summary for ``nexus kg status``, ``doctor`` and the HUD."""
    info = policy.POLICY.describe(workspace)
    if not info.get("enabled"):
        return info
    try:
        from .graph import KnowledgeGraph

        cfg = policy.load(workspace)
        graph = KnowledgeGraph(workspace)
        info.update(graph.stats())
        info.update(extraction=cfg["extraction"]["mode"], sources=[k for k, v in cfg["sources"].items() if v],
                    inject=cfg["context"]["inject"])
        problem = graph.store.problem()
        if problem:
            info["error"] = f"the graph cannot be sealed: {problem}"[:240]
    except (ValueError, OSError) as exc:
        info["error"] = str(exc)[:240]
    return info
