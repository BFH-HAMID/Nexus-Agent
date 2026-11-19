"""Glue between long-term memory / the knowledge index and the graph.

Every hook is a no-op while ``knowledge_graph.yaml`` is absent or OFF and never
raises: the graph is an optional mirror, so it must not be able to break a
memory write, a forget or an index run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import policy

_ERRORS = (ValueError, OSError, RuntimeError, TypeError, KeyError)


def _graph(workspace: Path | str, source: str, keyring_module: Any = None):
    try:
        cfg = policy.load(workspace)
    except _ERRORS:
        return None
    if not cfg["enabled"] or not cfg["sources"].get(source, False):
        return None
    from .graph import KnowledgeGraph

    try:
        return KnowledgeGraph(workspace, keyring_module=keyring_module)
    except _ERRORS:
        return None


def memory_written(workspace: Path | str, row: dict[str, Any], *, keyring_module: Any = None,
                   tainted: bool = False) -> int:
    """Mirror one confirmed long-term memory fact into the graph. Returns facts added."""
    graph = _graph(workspace, "memory", keyring_module)
    if graph is None or not isinstance(row, dict) or row.get("status", "confirmed") != "confirmed":
        return 0
    try:
        result = graph.ingest(str(row.get("text") or ""), source_kind="memory", source_ref=str(row.get("id") or ""),
                              trusted=True, tainted=tainted)
    except _ERRORS:
        return 0
    return int(result["added"])


def memory_forgotten(workspace: Path | str, ids: list[str] | tuple[str, ...], *, needle: str = "",
                     keyring_module: Any = None) -> int:
    """Cascade a memory forget: drop facts sourced only from the removed entries."""
    graph = _graph(workspace, "memory", keyring_module)
    if graph is None:
        return 0
    removed = 0
    try:
        removed += graph.forget_sources(list(ids))
        if needle and len(needle.strip()) >= 3 and not needle.startswith(("mem_", "pf_", "cmp_")):
            outcome = graph.forget(needle)
            removed += outcome["removed_relations"]
    except _ERRORS:
        return removed
    return removed


def wants_knowledge(workspace: Path | str) -> bool:
    try:
        cfg = policy.load(workspace)
    except _ERRORS:
        return False
    return bool(cfg["enabled"] and cfg["sources"]["knowledge"])


def knowledge_indexed(workspace: Path | str, collection: str, documents: list[tuple[str, str]]) -> int:
    """Store document-derived facts as UNVERIFIED. ``documents`` is ``[(doc_id, text)]``."""
    graph = _graph(workspace, "knowledge")
    if graph is None:
        return 0
    added = 0
    try:
        graph.forget_sources(prefix=f"knowledge:{collection}:")  # re-index replaces the collection's facts
    except _ERRORS:
        return 0
    for ident, text in documents[:200]:
        ref = f"knowledge:{collection}:{ident}"
        try:
            added += int(graph.ingest(text[:20_000], source_kind="knowledge", source_ref=ref, trusted=False)["added"])
        except _ERRORS:
            continue
    return added


def knowledge_removed(workspace: Path | str, collection: str, ident: str = "") -> int:
    graph = _graph(workspace, "knowledge")
    if graph is None:
        return 0
    try:
        return graph.forget_sources(prefix=f"knowledge:{collection}:{ident}")
    except _ERRORS:
        return 0


def prompt_block(workspace: Path | str, task: str) -> str:
    """Trusted related facts for the system prompt; empty unless ``context.inject`` is on."""
    try:
        cfg = policy.load(workspace)
        if not cfg["enabled"] or not cfg["context"]["inject"]:
            return ""
        from .graph import KnowledgeGraph

        return KnowledgeGraph(workspace).context_block(task)
    except _ERRORS:
        return ""
