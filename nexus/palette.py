"""Compact search index for the dashboard command palette.

``GET /api/roles`` returns the full roster (4.4 MB in the reference workspace)
because the Run composer needs personas. The palette only needs names, titles,
categories and the routed agent, so it gets its own ~90 KB projection instead of
pulling the whole roster into the browser a second time.

Everything here is a projection of the live configuration - there is no cache
and no invented field. ``built_at`` is the moment the projection ran.
"""

from __future__ import annotations

import time
from typing import Any


def _text(value: Any, limit: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    clean = " ".join(value.split())
    return clean[:limit]


def build(config: Any) -> dict[str, Any]:
    """Project roles, agents and pipelines into a light index."""
    started = time.time()
    roles = [
        {
            "name": role.name,
            "title": _text(getattr(role, "title", "") or role.name),
            "category": _text(getattr(role, "category", "") or "general", 60),
            "agent": _text(getattr(role, "agent", "") or "", 60),
        }
        for role in config.roles.roles.values()
    ]
    roles.sort(key=lambda row: row["name"])
    agents = [
        {
            "name": name,
            "provider": _text(getattr(agent, "provider", ""), 60),
            "model": _text(getattr(agent, "model", ""), 120),
            "key_present": bool(getattr(agent, "resolved_api_key", None)),
        }
        for name, agent in sorted(config.agents.items())
    ]
    pipelines = [
        {
            "name": name,
            "description": _text(getattr(pipe, "description", ""), 200),
            "roles": [_text(str(step.get("role") or ""), 60) for step in getattr(pipe, "steps", [])[:24]],
        }
        for name, pipe in sorted(config.pipelines.items())
    ]
    return {
        "built_at": time.time(),
        "elapsed_ms": round((time.time() - started) * 1000, 1),
        "default_agent": _text(getattr(config, "default_agent", ""), 60),
        "roles": roles,
        "agents": agents,
        "pipelines": pipelines,
    }
