"""Agent tool for the council. Registered only while ``council.yaml`` is ON."""
from __future__ import annotations

import json

from . import policy


def register(reg, ctx) -> None:
    if not policy.enabled(ctx.workspace) or ctx.config is None:
        return
    from ..tools import ToolFailure
    from ..types import Risk

    def ask(args):
        from .core import Council

        try:
            record = Council(ctx.workspace, ctx.config, ctx.providers).deliberate(
                str(args.get("question") or ""), context=str(args.get("context") or ""), subject="tool:council_ask")
        except ValueError as exc:
            raise ToolFailure(str(exc)) from None
        return json.dumps({k: record[k] for k in ("id", "decision", "votes", "agreement", "safety_veto", "summary",
                                                  "dissent", "usage")} | {"advisory": True}, ensure_ascii=False)

    reg.register(
        "council_ask",
        "Ask the operator-configured model council a question. Returns a tallied verdict "
        "(approve/revise/reject/no_quorum), a summary and dissent. Advisory only: it never approves an action. "
        "Spends model calls within the caps in council.yaml.",
        {"type": "object", "properties": {"question": {"type": "string"}, "context": {"type": "string"}},
         "required": ["question"]},
        ask, risk=Risk.WRITE)
