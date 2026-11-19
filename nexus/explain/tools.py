"""Gate observer + agent tool for explainability. Active only while ``explain.yaml`` is ON."""
from __future__ import annotations

import json

from . import policy


def register(reg, ctx) -> None:
    if not policy.enabled(ctx.workspace):
        return
    from ..tools import ToolFailure
    from ..types import Risk
    from .record import install

    install(ctx.gate, ctx.workspace)

    def explain(args):
        from .core import explain_action

        seq = args.get("seq")
        try:
            result = explain_action(ctx.workspace, seq if seq is None else int(seq))
        except (ValueError, TypeError) as exc:
            raise ToolFailure(str(exc)) from None
        return json.dumps(result, ensure_ascii=False)

    reg.register(
        "explain_action",
        "Explain why a recorded action was approved or refused, using only the audit chain "
        "(no guessing). Without 'seq' it explains the most recent recorded decision. Quote the "
        "returned factors and any warning verbatim; do not add reasons that are not listed.",
        {"type": "object", "properties": {"seq": {"type": "integer", "minimum": 0}}},
        explain, risk=Risk.READ, read_only=True)
