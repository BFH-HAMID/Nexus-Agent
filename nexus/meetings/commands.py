"""Spoken / typed meeting commands (voice pipeline and ``/meeting``)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import policy

_START = re.compile(r"^(?:please )?(?:start|begin|take) (?:the )?meeting notes?(?: (?:for|about|on) (?P<title>.{2,100}))?[.!]?$", re.I)
_AGREED = re.compile(r"^(?:yes,? )?every(?:one|body) (?:has )?agree[sd](?: to (?:the )?notes?)?,? (?:please )?"
                     r"(?:start|begin|take) (?:the )?meeting notes?(?: (?:for|about|on) (?P<title>.{2,100}))?[.!]?$", re.I)
_NOTE = re.compile(r"^(?:meeting note|add to (?:the )?meeting notes?|note for the meeting)[:,]? (?P<text>.{2,})$", re.I)
_STOP = re.compile(r"^(?:please )?(?:stop|end|finish|close) (?:the )?meeting(?: notes?)?[.!]?$", re.I)
_SUMMARY = re.compile(r"^(?:please )?(?:summari[sz]e (?:the |this |that |our )?(?:last )?meeting|"
                      r"(?:what (?:are|were) the|list the) action items(?: (?:from|of) the (?:last )?meeting)?|"
                      r"(?:give me )?(?:the )?meeting summary)[.?!]?$", re.I)
OFF = "Meeting assistant is OFF (operator opt-in: meetings.yaml enabled: true)."


def parse(text: str) -> dict[str, Any] | None:
    spoken = " ".join(str(text or "").split())
    for kind, pattern in (("agreed", _AGREED), ("start", _START), ("note", _NOTE), ("stop", _STOP), ("summary", _SUMMARY)):
        match = pattern.match(spoken)
        if match:
            return {"kind": kind, "title": (match.groupdict().get("title") or "").strip(" .!'\""),
                    "text": (match.groupdict().get("text") or "").strip()}
    return None


def handle(workspace: Path | str, text: str, ctx: dict[str, Any] | None = None) -> tuple[str, str] | None:
    """``(reply, intent)`` when the utterance is a meeting command and the assistant is ON."""
    intent = parse(text)
    if intent is None or not policy.enabled(workspace):
        return None
    from .core import Meetings, spoken

    ctx = ctx or {}
    try:
        meetings = Meetings(workspace, ctx.get("config"), ctx.get("providers"), keyring_module=ctx.get("keyring_module"))
        kind = intent["kind"]
        trusted = kind == "stop" or not (ctx.get("tainted") or ctx.get("speaker_verified") is False)
        if kind in ("start", "agreed", "note") and not trusted:
            return "I only take meeting notes on a trusted, verified request.", "meeting_refused"
        if kind == "summary" and not trusted:
            return "I only read meeting notes aloud on a trusted, verified request.", "meeting_refused"
        if kind == "summary" and ctx.get("privacy_mode"):
            return ("Voice privacy mode is active, so I won't read the meeting notes aloud. "
                    "Read them with: nexus meetings summary."), "meeting_private"
        if kind == "start":
            # consent is a statement by a person, so asking for notes is not enough
            return ("Has everyone here agreed to notes being taken? If so, say: everyone agreed, start meeting notes.",
                    "meeting_consent")
        if kind == "agreed":
            row = meetings.start(intent.get("title") or "Meeting", "Spoken confirmation that everyone present agreed "
                                 "to notes being taken", by="voice")
            return f"{row['notice']} Say 'meeting note', then what to write down.", "meeting_start"
        if kind == "note":
            row = meetings.note(intent["text"])
            return f"Noted, {row['segments']} so far.", "meeting_note"
        if kind == "stop":
            row = meetings.stop()
            return f"Meeting notes stopped with {row['segments']} lines. Say 'summarize the meeting' for the summary.", "meeting_stop"
        return spoken(meetings.summary("latest", model=False)), "meeting_summary"
    except (ValueError, OSError) as exc:
        return f"Meeting assistant: {str(exc)[:200]}", "meeting_error"


def slash(workspace: Path | str, argument: str, ctx: dict[str, Any] | None = None) -> str:
    """``/meeting`` | ``start TITLE | CONSENT`` | ``note TEXT`` | ``stop`` | ``summary [ID]`` | ``list``."""
    if not policy.enabled(workspace):
        return OFF
    from .core import Meetings, brief, render

    ctx = ctx or {}
    meetings = Meetings(workspace, ctx.get("config"), ctx.get("providers"), keyring_module=ctx.get("keyring_module"))
    verb, _, rest = argument.strip().partition(" ")
    rest = rest.strip()
    if verb == "start":
        title, _, consent = rest.partition("|")
        if not consent.strip():
            return "Usage: /meeting start TITLE | who was told that notes are taken and agreed"
        row = meetings.start(title, consent)
        return f"started {row['id']}: {row['title']}\ntell everyone: {row['notice']}"
    if verb == "note":
        row = meetings.note(rest)
        return f"noted ({row['segments']} lines)"
    if verb == "stop":
        row = meetings.stop()
        return f"stopped {row['id']} ({row['segments']} lines); /meeting summary"
    if verb == "summary":
        return render(meetings.summary(rest or "latest"))
    if verb in ("", "list", "status"):
        rows = meetings.rows()
        current = meetings.open_meeting()
        head = f"open: {current['title']} ({current['segments']} lines)" if current else "no open meeting"
        return "\n".join([head, *(brief(row) for row in rows[:10])])
    return "Usage: /meeting [list] | start TITLE | CONSENT | note TEXT | stop | summary [ID]"
