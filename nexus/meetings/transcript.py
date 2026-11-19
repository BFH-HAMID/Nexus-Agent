"""Read a transcript file (plain text, WebVTT, SubRip) into ``[{t, speaker, text}]``.

Pure text handling: nothing here opens the network or runs a model.
"""
from __future__ import annotations

import re
from typing import Any

TEXT_SUFFIXES = (".txt", ".md", ".vtt", ".srt")
AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".ogg", ".m4a")

_TIMING = re.compile(r"^\s*(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_VOICE = re.compile(r"<v(?:\.[^\s>]{0,80})?\s{1,4}([^>]{1,60})>")  # bounded: "<v.<v.<v. ..." must fail fast
_TAG = re.compile(r"</?[a-zA-Z][^>]{0,80}>|\{\\[^}]{0,40}\}")
_STAMP = re.compile(r"^\s*\[?(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\]?\s*[-–:]?\s*")
_SPEAKER = re.compile(r"^(?P<speaker>[^\W\d_][^:\n]{0,39}):\s+(?P<text>\S.*)$")
# labels that look like "Name: text" but are not people
_NOT_SPEAKERS = frozenset({"note", "notes", "action", "action item", "action items", "todo", "to-do", "decision",
                           "decisions", "agenda", "summary", "topic", "question", "answer", "q", "a", "re", "fwd",
                           "subject", "date", "time", "http", "https", "next steps", "next step", "update"})


def _seconds(hours, minutes, seconds, millis="0") -> float:
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int((millis or "0").ljust(3, "0")[:3]) / 1000


def _clean(text: str) -> str:
    return " ".join(_TAG.sub(" ", text).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                    .replace("&nbsp;", " ").split())


def split_speaker(line: str) -> tuple[str, str]:
    """``("Rahim", "text")`` for ``Rahim: text``; ``("", line)`` when the label is not a person."""
    match = _SPEAKER.match(line)
    if not match:
        return "", line
    speaker = " ".join(match.group("speaker").split())
    if speaker.casefold() in _NOT_SPEAKERS or len(speaker.split()) > 4 or re.search(r"[.!?,;/\\]", speaker):
        return "", line
    return speaker, match.group("text").strip()


def _cues(text: str) -> list[dict[str, Any]]:
    """WebVTT and SubRip share one shape: blocks with a timing line, then text lines."""
    segments: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line for line in block.split("\n") if line.strip()]
        at = next((i for i, line in enumerate(lines[:2]) if _TIMING.match(line)), None)
        if at is None:
            continue  # WEBVTT header, NOTE, STYLE, REGION, stray text
        timing = _TIMING.match(lines[at])
        body = " ".join(lines[at + 1:])
        voice = _VOICE.search(body)
        speaker, spoken = (" ".join(voice.group(1).split()), _clean(body)) if voice else split_speaker(_clean(body))
        if spoken:
            segments.append({"t": round(_seconds(*timing.groups()[:4]), 3),
                             "end": round(_seconds(*timing.groups()[4:]), 3), "speaker": speaker[:60], "text": spoken})
    return segments


def _lines(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for raw in text.split("\n"):
        line = " ".join(raw.split())
        if not line or set(line) <= set("-=_*#"):
            continue
        stamp = _STAMP.match(line)
        when = None
        if stamp and stamp.end() < len(line):
            when, line = _seconds(*stamp.groups()), line[stamp.end():]
        speaker, spoken = split_speaker(line.lstrip("#*- ").strip() or line)
        if spoken:
            segments.append({"t": when, "end": None, "speaker": speaker[:60], "text": spoken})
    return segments


def parse(text: str, suffix: str = ".txt") -> list[dict[str, Any]]:
    """Segments in file order. Unknown structure degrades to one segment per line."""
    body = str(text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if suffix.casefold() in (".vtt", ".srt") or body.lstrip().startswith("WEBVTT"):
        return _cues(body)
    # a caption file saved as .txt: use the cues when there are any
    return (_cues(body) if any(_TIMING.match(line) for line in body.split("\n")[:200]) else []) or _lines(body)
