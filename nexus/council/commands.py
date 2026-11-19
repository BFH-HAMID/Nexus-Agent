"""Spoken / typed council commands (voice pipeline and ``/council``)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import policy

_ASK = re.compile(r"^(?:ask|convene|consult) the council(?: (?:about|whether|if|on))?[:,]? (?P<q>.{3,})$", re.I)


def handle(workspace: Path | str, text: str, ctx: dict[str, Any] | None = None) -> tuple[str, str] | None:
    match = _ASK.match(" ".join(str(text or "").split()))
    if match is None or not policy.enabled(workspace):
        return None
    ctx = ctx or {}
    if ctx.get("tainted") or ctx.get("speaker_verified") is False:
        # a deliberation spends model calls on a question somebody chose: only for the operator
        return "I only convene the council on a trusted, verified request.", "council_refused"
    return slash(workspace, match.group("q"), ctx), "council_ask"


def slash(workspace: Path | str, argument: str, ctx: dict[str, Any] | None = None) -> str:
    from .core import Council, render

    ctx = ctx or {}
    if not policy.enabled(workspace):
        return "Council is OFF (operator opt-in: council.yaml enabled: true with 2-5 members)."
    if not argument.strip():
        return "Usage: /council <question>"
    config = ctx.get("config")
    if config is None:
        from ..config import load_config

        config = load_config(workspace)
    return render(Council(workspace, config, ctx.get("providers")).deliberate(argument, subject=ctx.get("source", "")))
