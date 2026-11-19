"""Spoken / typed commands for the knowledge graph (voice pipeline and ``/kg``)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import policy

_ABOUT = re.compile(r"^(?:what|tell me what) do you know about (?P<name>.+?)\??$", re.I)
_WHO = re.compile(r"^who is (?P<name>(?!home|there|at home)[^?]{2,60}?)\??$", re.I)
_RELATED = re.compile(r"^how (?:is|are) (?P<a>.+?) (?:related|connected|linked) to (?P<b>.+?)\??$", re.I)
REFUSED = "I only say what the graph holds on a trusted, verified request."
PRIVATE = "Voice privacy mode is active, so I won't say what the graph holds aloud. Look it up with: nexus kg about NAME."
_LINK = re.compile(r"^(?:remember|note) that (?P<a>[^,]{2,60}?) (?P<p>is related to|knows|works with) (?P<b>[^,.]{2,60})\.?$", re.I)


def parse(text: str) -> dict[str, Any] | None:
    spoken = " ".join(str(text or "").split())
    for kind, pattern in (("related", _RELATED), ("about", _ABOUT), ("about", _WHO), ("link", _LINK)):
        match = pattern.match(spoken)
        if match:
            return {"kind": kind, **{k: v.strip(" '\"") for k, v in match.groupdict().items()}}
    return None


def handle(workspace: Path | str, text: str, ctx: dict[str, Any] | None = None) -> tuple[str, str] | None:
    """``(reply, intent)`` when the utterance is a graph command and the graph is ON."""
    intent = parse(text)
    if intent is None or not policy.enabled(workspace):
        return None
    from .graph import GraphError, KnowledgeGraph

    ctx = ctx or {}

    def trusted() -> bool:
        return not (ctx.get("tainted") or ctx.get("speaker_verified") is False)

    try:
        graph = KnowledgeGraph(workspace, keyring_module=ctx.get("keyring_module"))
        if intent["kind"] == "about":
            found = graph.about(intent["name"], limit=6)
            if not found["entity"]:
                if intent["name"].casefold() in {"me", "myself"}:
                    return "I have no linked facts about you yet.", "kg_about"
                return None  # not a known entity: let the normal assistant answer
            if not trusted():
                return REFUSED, "kg_refused"
            if ctx.get("privacy_mode"):
                return PRIVATE, "kg_private"
            if not found["facts"]:
                return f"I know the name {found['entity']['name']} but no facts about it.", "kg_about"
            lines = "; ".join(graph.sentence(f) for f in found["facts"])
            return f"About {found['entity']['name']}: {lines}.", "kg_about"
        if intent["kind"] == "related":
            if graph.resolve(intent["a"]) is None and graph.resolve(intent["b"]) is None:
                return None  # neither name is in the graph: a general question, the normal assistant's
            if not trusted():
                return REFUSED, "kg_refused"
            if ctx.get("privacy_mode"):
                return PRIVATE, "kg_private"
            chain = graph.path(intent["a"], intent["b"])
            if not chain:
                return f"I do not know a link between {intent['a']} and {intent['b']}.", "kg_path"
            return "Link: " + "; then ".join(graph.sentence(f) for f in chain) + ".", "kg_path"
        if not trusted():
            return "I can only add graph facts from a trusted, verified request.", "kg_refused"
        predicate = {"is related to": "related_to", "knows": "knows", "works with": "works_with"}[intent["p"].lower()]
        fact = graph.add_fact(intent["a"], predicate, intent["b"], source_kind="user", source_ref="voice")
        if ctx.get("privacy_mode"):
            return "Noted.", "kg_add"
        return f"Noted: {fact['subject']} {intent['p'].lower()} {fact['object']}.", "kg_add"
    except (GraphError, ValueError, OSError) as exc:
        return f"Knowledge graph unavailable: {str(exc)[:160]}", "kg_error"


def slash(workspace: Path | str, argument: str, ctx: dict[str, Any] | None = None) -> str:
    """``/kg <name>`` | ``/kg path A | B`` | ``/kg stats`` | ``/kg forget <name>``."""
    from .graph import KnowledgeGraph

    cfg_ok = policy.enabled(workspace)
    if not cfg_ok:
        return "Knowledge graph is OFF (operator opt-in: knowledge_graph.yaml enabled: true)."
    graph = KnowledgeGraph(workspace, keyring_module=(ctx or {}).get("keyring_module"))
    words = argument.strip()
    if not words or words == "stats":
        s = graph.stats()
        return f"{s['entities']} entities, {s['relations']} relations ({s['unverified']} unverified), encrypted={s['encrypted']}"
    if words.startswith("path "):
        first, _, second = words[5:].partition("|")
        chain = graph.path(first.strip(), second.strip())
        return "\n".join(graph.sentence(f) for f in chain) or "No link found."
    if words.startswith("forget "):
        out = graph.forget(words[7:])
        return f"Removed {out['removed_relations']} relation(s), {out['removed_entities']} entity(ies)."
    found = graph.about(words)
    if not found["entity"]:
        hits = graph.search(words)
        return "\n".join(graph.sentence(f) for f in hits) or f"Nothing known about {words}."
    return "\n".join(graph.sentence(f) for f in found["facts"]) or "No facts."
