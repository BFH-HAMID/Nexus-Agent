"""Spoken / typed "why" commands (voice pipeline and ``/why``)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import policy

_WHY = re.compile(
    r"^(?:nexus[, ]+)?(?:why did you (?:just )?(?:do|run|write|delete|change|send|refuse|block|deny) (?:that|this|it)"
    r"|why (?:was|did) (?:that|this|it) (?:get )?(?:blocked|denied|refused|approved|allowed)"
    r"|explain (?:that|your last action|the last action|what you (?:just )?did)"
    r"|what did you just do)\??$", re.I)


def handle(workspace: Path | str, text: str, ctx: dict[str, Any] | None = None) -> tuple[str, str] | None:
    if not _WHY.match(" ".join(str(text or "").split())):
        return None
    try:
        cfg = policy.load(workspace)
    except ValueError:
        return None
    if not cfg["enabled"] or not cfg["voice"]:
        return None
    from .core import explain_action, spoken

    return spoken(explain_action(workspace)), "explain_last"


def slash(workspace: Path | str, argument: str, ctx: dict[str, Any] | None = None) -> str:
    from .core import explain_action, explain_run, render

    if not policy.enabled(workspace):
        return "Explainability is OFF (operator opt-in: explain.yaml enabled: true)."
    word = argument.strip()
    if word.startswith("run"):
        return render(explain_run(workspace, word[3:].strip(": ") or "last"))
    if word and not word.isdigit():
        return "Usage: /why [SEQ] | /why run [ID]"
    return render(explain_action(workspace, int(word) if word else None))
