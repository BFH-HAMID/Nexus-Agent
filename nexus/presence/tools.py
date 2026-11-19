"""Agent tool for presence: read-only. The model can not set presence or trigger a rule."""
from __future__ import annotations

import json

from . import policy


def register(reg, ctx) -> None:
    if not policy.enabled(ctx.workspace):
        return
    from ..tools import ToolFailure
    from ..types import Risk

    def read(_args):
        from .engine import PresenceEngine

        try:
            view = PresenceEngine(ctx.workspace).status()
        except ValueError as exc:
            raise ToolFailure(str(exc)) from None
        return json.dumps({key: view[key] for key in ("state", "for_seconds", "manual")})

    reg.register(
        "presence_status",
        "Whether the user is currently present, idle or away (coarse state only; no history, no locations).",
        {"type": "object", "properties": {}}, read, risk=Risk.READ, read_only=True)
