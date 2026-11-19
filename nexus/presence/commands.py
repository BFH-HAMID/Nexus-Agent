"""Spoken / typed presence commands (voice pipeline and ``/presence``)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import policy

_HOME = re.compile(r"^(?:nexus[, ]+)?i(?:'m| am) (?:home|back)(?: now)?[.!]?$", re.I)
_AWAY = re.compile(r"^(?:nexus[, ]+)?i(?:'m| am) (?:leaving|heading out|going out|off)(?: now)?[.!]?$", re.I)
_ASK = re.compile(r"^(?:nexus[, ]+)?(?:presence status|am i (?:home|away|present)|what(?:'s| is) my presence)\??$", re.I)


def handle(workspace: Path | str, text: str, ctx: dict[str, Any] | None = None) -> tuple[str, str] | None:
    spoken = " ".join(str(text or "").split())
    wanted = "home" if _HOME.match(spoken) else "away" if _AWAY.match(spoken) else "ask" if _ASK.match(spoken) else ""
    if not wanted or not policy.enabled(workspace):
        return None
    from .engine import PresenceEngine

    ctx = ctx or {}
    engine = PresenceEngine(workspace)
    if wanted == "ask":
        view = engine.status()
        return f"You are marked {view['state']}" + (f" ({view['manual']} set manually)." if view["manual"] != "auto" else "."), "presence_status"
    if not engine.cfg["signals"]["manual"]:
        return None
    if ctx.get("tainted") or ctx.get("speaker_verified") is False:
        return "I can only change presence for a verified speaker in a trusted session.", "presence_refused"
    view = engine.set_manual(wanted, source=str(ctx.get("source") or "voice"))
    done = [row["rule"] for row in view["fired"] if row["ok"]]
    reply = "Welcome home." if wanted == "home" else "Okay, marked as away."
    return reply + (f" ({len(done)} presence rule(s) ran.)" if done else ""), "presence_set"


def slash(workspace: Path | str, argument: str, ctx: dict[str, Any] | None = None) -> str:
    from .engine import PresenceEngine

    if not policy.enabled(workspace):
        return "Home presence is OFF (operator opt-in: presence.yaml enabled: true)."
    word = argument.strip().lower()
    engine = PresenceEngine(workspace)
    if word in {"home", "away", "auto"}:
        view = engine.set_manual(word, source=str((ctx or {}).get("source") or "cli"))
    elif word in {"", "status"}:
        view = engine.tick()
    else:
        return "Usage: /presence [status|home|away|auto]"
    return (f"Presence: {view['state']} (manual: {view['manual']}; signals: {', '.join(view['signals']) or 'none'})"
            + "".join(f"\n  rule {row['rule']}: {row['detail']}" for row in view.get("fired", [])))
