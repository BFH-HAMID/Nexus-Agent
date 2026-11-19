"""Agent tool for the simulator. Registered only while ``whatif.yaml`` is ON."""
from __future__ import annotations

import json

from . import policy


def register(reg, ctx) -> None:
    if not policy.enabled(ctx.workspace):
        return
    from ..tools import ToolFailure
    from ..types import Risk

    def simulate(args):
        from .core import WhatIf

        try:
            service = WhatIf(ctx.workspace, config=ctx.config, providers=ctx.providers)
            if args.get("command"):
                record = service.command(str(args["command"]))
            else:
                record = service.action(str(args.get("tool") or ""), args.get("args") or {}, registry=reg)
        except ValueError as exc:
            raise ToolFailure(str(exc)) from None
        return json.dumps({"id": record["id"], **record["result"]}, ensure_ascii=False, default=str)

    reg.register(
        "whatif_simulate",
        "Predict what a tool call or shell command WOULD do (files touched, diff preview, reversibility, "
        "external effects, approval needed) without running it. Use before risky actions. Never executes.",
        {"type": "object", "properties": {"tool": {"type": "string"}, "args": {"type": "object"},
                                          "command": {"type": "string"}}},
        simulate, risk=Risk.READ, read_only=True)
