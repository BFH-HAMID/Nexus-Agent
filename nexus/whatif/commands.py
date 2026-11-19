"""Spoken / typed what-if commands (voice pipeline and ``/whatif``)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import policy

_ASK = re.compile(r"^what (?:if|happens if|would happen if) .{3,}$", re.I)


def handle(workspace: Path | str, text: str, ctx: dict[str, Any] | None = None) -> tuple[str, str] | None:
    spoken = " ".join(str(text or "").split())
    if not _ASK.match(spoken) or not policy.enabled(workspace):
        return None
    from .core import WhatIf, WhatIfError, render

    ctx = ctx or {}
    try:
        record = WhatIf(workspace, config=ctx.get("config"), providers=ctx.get("providers")).ask(spoken)
    except WhatIfError:
        return None  # an open question in static mode: let the normal assistant answer it
    return render(record), "whatif"


def slash(workspace: Path | str, argument: str, ctx: dict[str, Any] | None = None) -> str:
    from .core import WhatIf, render

    ctx = ctx or {}
    if not policy.enabled(workspace):
        return "What-if simulator is OFF (operator opt-in: whatif.yaml enabled: true)."
    words = argument.strip()
    if not words:
        return "Usage: /whatif <question> | /whatif run <command>"
    service = WhatIf(workspace, config=ctx.get("config"), providers=ctx.get("providers"))
    if words.startswith("run "):
        return render(service.command(words[4:]))
    return render(service.ask(words if words.lower().startswith("what") else "what if " + words))
