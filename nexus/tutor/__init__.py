"""Task 30 - the tutor (opt-in, ``tutor.yaml``).

Flashcards with spaced repetition: you add cards (by hand, from a file, or -
opt-in - proposed by a model from your notes), and the tutor asks each card
again after a pause that grows while you keep knowing it (the SM-2 method).
A quiz checks typed or spoken answers; a review lets you grade yourself.
Offline unless you turn a model option on.  See ``docs/tutor.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import policy


def status(workspace: Path | str) -> dict[str, Any]:
    """Never-raising summary for ``nexus tutor status``, ``doctor`` and the HUD. Counts only, no card text."""
    info = policy.POLICY.describe(workspace)
    if not info.get("enabled"):
        return info
    try:
        from .core import Tutor

        cfg = policy.load(workspace)
        info.update(encrypt_at_rest=cfg["encrypt_at_rest"], voice=cfg["voice"]["enabled"], agent_tools=cfg["agent_tools"],
                    model_grade=cfg["model"]["grade"], model_generate=cfg["model"]["generate"])
        tutor = Tutor(workspace)
        problem = tutor.store.problem()
        if problem:
            info["error"] = f"the cards cannot be sealed: {problem}"[:240]
        decks = tutor.decks()
        info.update(decks=len(decks), cards=sum(row["cards"] for row in decks), due=sum(row["due"] for row in decks),
                    new=sum(row["new"] for row in decks))
        session = tutor.current()
        if session is not None:
            info["session"] = f"{session['kind']} ({session['origin']})"
    except (ValueError, OSError) as exc:
        info["error"] = str(exc)[:240]
    return info
