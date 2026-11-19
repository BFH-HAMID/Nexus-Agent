"""Spoken / typed translation commands (voice pipeline and ``/translate``), and interpreter mode."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import policy

_LANG = r"(?P<lang>[A-Za-z]{2,12})"
_TRANSLATE = re.compile(rf"^(?:please )?translate (?P<text>.{{1,1500}}?) (?:in)?to {_LANG}[.!?]?$", re.I | re.S)
_SAY = re.compile(rf"^(?:how (?:do|would) (?:you|i) say|say) (?P<text>.{{1,1500}}?) in {_LANG}[.!?]?$", re.I | re.S)
_START = re.compile(r"^(?:please )?(?:start|begin) (?:the )?interpreter(?: mode)?"
                    r"(?: (?:between|for) (?P<a>[A-Za-z]{2,12}) and (?P<b>[A-Za-z]{2,12}))?[.!]?$", re.I)
_STOP = re.compile(r"^(?:please )?(?:stop|end|exit) (?:the )?interpreter(?: mode)?[.!]?$", re.I)
OFF = "Translator is OFF (operator opt-in: translate.yaml enabled: true)."


def parse(text: str) -> dict[str, Any] | None:
    spoken = " ".join(str(text or "").split())
    for kind, pattern in (("stop", _STOP), ("start", _START), ("translate", _TRANSLATE), ("translate", _SAY)):
        match = pattern.match(spoken)
        if match:
            return {"kind": kind, **{key: (value or "").strip(" '\"") for key, value in match.groupdict().items()}}
    return None


def exclusive(workspace: Path | str) -> bool:
    """True while interpreter mode is active: every utterance then belongs to the translator."""
    if not policy.enabled(workspace, "interpreter.enabled"):
        return False
    try:
        from .core import Translator

        return Translator(workspace).interpreter() is not None
    except (ValueError, OSError):
        return False


def handle(workspace: Path | str, text: str, ctx: dict[str, Any] | None = None) -> tuple[str, str] | None:
    """``(reply, intent)`` for a translation command, or for any utterance while interpreter mode is active."""
    if not policy.enabled(workspace):
        return None
    from . import detect
    from .core import Translator

    ctx = ctx or {}
    intent = parse(text)
    try:
        translator = Translator(workspace, ctx.get("config"), ctx.get("providers"))
        active = translator.interpreter()
        if intent and intent["kind"] == "stop":
            return ("Interpreter stopped." if translator.stop_interpreter() else "The interpreter was not running."), "translate_stop"
        if intent and intent["kind"] == "start":
            if ctx.get("tainted") or ctx.get("speaker_verified") is False:
                return "I only start the interpreter on a trusted, verified request.", "translate_refused"
            pair = translator.start_interpreter(intent.get("a"), intent.get("b"))["pair"]
            return (f"Interpreter on: {detect.name_of(pair[0])} and {detect.name_of(pair[1])}. "
                    "Say 'stop interpreter' to end."), "translate_start"
        if active:
            return translator.interpret(text)["translation"], "translate_interpreter"
        if intent is None or not detect.code_of(intent["lang"]):
            return None  # "translate X to Y" with an unknown Y: let the normal assistant answer
        return translator.translate(intent["text"], intent["lang"], origin="voice")["translation"], "translate"
    except (ValueError, OSError) as exc:
        return f"Translator: {str(exc)[:200]}", "translate_error"


def slash(workspace: Path | str, argument: str, ctx: dict[str, Any] | None = None) -> str:
    """``/translate <language> <text>`` | ``/translate detect <text>`` | ``/translate languages``."""
    from . import detect

    words = argument.strip()
    verb, _, rest = words.partition(" ")
    if verb == "detect" and rest.strip():
        found = detect.detect(rest)
        return f"{detect.name_of(found['language']) if found['language'] != 'und' else 'unknown'} ({found['script'] or 'no'} script)"
    if not policy.enabled(workspace):
        return OFF
    from .core import Translator, render

    ctx = ctx or {}
    translator = Translator(workspace, ctx.get("config"), ctx.get("providers"))
    if verb == "languages":
        return ", ".join(f"{row['code']} {row['name']}" for row in translator.languages())
    if not rest.strip() or not detect.code_of(verb):
        return "Usage: /translate <language> <text> | /translate detect <text> | /translate languages"
    return render(translator.translate(rest, verb))
