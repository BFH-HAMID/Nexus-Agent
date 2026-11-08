"""Full-duplex barge-in controller, sentence streaming buffer, and latency metrics."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .vad import BaseVAD

_CLAUSE_SPLIT_RE = re.compile(r"[.!?।](?:[\"'”’)\]]*)(?:\s+|$)|\n+")
_FAST_START_CLAUSE_RE = re.compile(r"[,;:](?:\s+)")


class BargeInAction(str, Enum):
    DISCARD = "discard"    # "stop", "cancel", "never mind"
    RESUME = "resume"      # "continue", "keep going", empty/noise false trigger
    NEW_TURN = "new_turn"  # substantive new command or question


_DISCARD_RE = re.compile(
    r"^(?:stop|cancel|shut\s+up|never\s*mind|enough|quit|abort|থামো|বাদ\s*দাও|বাতিল|থাক)(?:\s+please)?[.!?।]*$",
    re.I,
)
_RESUME_RE = re.compile(
    r"^(?:(?:sorry|please|ok|okay)\s+)?(?:continue|keep\s+going|go\s+on|go\s+ahead|resume|finish|"
    r"বলো|চালিয়ে\s*যাও|আবার\s*বলো|থাক\s*বলো)[.!?।]*$",
    re.I,
)


def classify_barge_in(text: str) -> BargeInAction:
    """Decide whether a barge-in utterance resumes, discards, or starts a new turn."""
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return BargeInAction.RESUME
    if _DISCARD_RE.match(cleaned):
        return BargeInAction.DISCARD
    if _RESUME_RE.match(cleaned):
        return BargeInAction.RESUME
    return BargeInAction.NEW_TURN


@dataclass
class TurnMetrics:
    stt_ms: float = 0.0
    llm_first_token_ms: float = 0.0
    tts_first_audio_ms: float = 0.0
    e2e_first_audio_ms: float = 0.0
    barge_in_cancel_ms: float | None = None
    total_turn_ms: float = 0.0
    interrupted: bool = False
    barge_in_action: str = ""
    acknowledged: bool = False
    language: str = "en"
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stt_ms": round(self.stt_ms, 2),
            "llm_first_token_ms": round(self.llm_first_token_ms, 2),
            "tts_first_audio_ms": round(self.tts_first_audio_ms, 2),
            "e2e_first_audio_ms": round(self.e2e_first_audio_ms, 2),
            "total_turn_ms": round(self.total_turn_ms, 2),
            "interrupted": self.interrupted,
            "acknowledged": self.acknowledged,
            "language": self.language,
        }
        if self.barge_in_cancel_ms is not None:
            payload["barge_in_cancel_ms"] = round(self.barge_in_cancel_ms, 2)
        if self.barge_in_action:
            payload["barge_in_action"] = self.barge_in_action
        if self.provider:
            payload["provider"] = self.provider
        return payload


class StreamingSentenceBuffer:
    """Incrementally flushes speakable sentences from streamed LLM tokens."""

    def __init__(self, *, fast_first_clause: bool = True, fast_clause_min_chars: int = 28, max_chars: int = 360) -> None:
        self.fast_first_clause = fast_first_clause
        self.fast_clause_min_chars = max(12, int(fast_clause_min_chars))
        self.max_chars = max(80, int(max_chars))
        self._buf = ""
        self._emitted_first = False

    def push(self, token: str) -> list[str]:
        if not token:
            return []
        self._buf += token
        out: list[str] = []
        while True:
            match = _CLAUSE_SPLIT_RE.search(self._buf)
            if match is not None:
                sent = self._buf[: match.end()].strip()
                self._buf = self._buf[match.end() :]
                if sent:
                    self._emitted_first = True
                    out.append(sent)
                continue
            if self.fast_first_clause and not self._emitted_first and len(self._buf) >= self.fast_clause_min_chars:
                clause = _FAST_START_CLAUSE_RE.search(self._buf)
                if clause is not None and clause.end() >= self.fast_clause_min_chars:
                    sent = self._buf[: clause.end()].strip()
                    self._buf = self._buf[clause.end() :]
                    if sent:
                        self._emitted_first = True
                        out.append(sent)
                    continue
            if len(self._buf) >= self.max_chars:
                cut = self._buf.rfind(" ", 0, self.max_chars)
                cut = cut if cut > 0 else self.max_chars
                piece = self._buf[:cut].strip()
                self._buf = self._buf[cut:].lstrip()
                if piece:
                    self._emitted_first = True
                    out.append(piece)
                continue
            break
        return out

    def flush(self) -> str:
        rest = self._buf.strip()
        self._buf = ""
        return rest


def iter_streaming_sentences(
    tokens: Iterable[str],
    *,
    cancel_event: threading.Event | None = None,
    fast_first_clause: bool = True,
    on_first_token: Callable[[], None] | None = None,
) -> Iterator[str]:
    """Stream LLM tokens into sentences while honouring immediate cancellation."""
    buf = StreamingSentenceBuffer(fast_first_clause=fast_first_clause)
    saw_token = False
    for token in tokens:
        if cancel_event is not None and cancel_event.is_set():
            return
        if not token:
            continue
        if not saw_token:
            saw_token = True
            if on_first_token is not None:
                on_first_token()
        for sent in buf.push(token):
            if cancel_event is not None and cancel_event.is_set():
                return
            yield sent
    if cancel_event is not None and cancel_event.is_set():
        return
    tail = buf.flush()
    if tail:
        yield tail


class FullDuplexMonitor:
    """Monitors incoming audio frames during TTS playback/LLM streaming to detect barge-in."""

    def __init__(
        self,
        vad: BaseVAD,
        *,
        sample_rate: int = 16_000,
        threshold: float = 0.55,
        min_speech_ms: int = 96,
        on_barge_in: Callable[[bytes], None] | None = None,
    ) -> None:
        self.vad = vad
        self.sample_rate = sample_rate
        self.threshold = float(threshold)
        self.min_speech_ms = max(32, int(min_speech_ms))
        self.on_barge_in = on_barge_in

        self.chunk_samples = vad.chunk_samples
        self.chunk_bytes = self.chunk_samples * 2
        self.chunk_ms = (self.chunk_samples / self.sample_rate) * 1000.0
        self._speech_ms = 0.0
        self._captured: list[bytes] = []
        self._remainder = bytearray()
        self.triggered = threading.Event()
        self.detected_at_perf: float | None = None

    def reset(self) -> None:
        self._speech_ms = 0.0
        self._captured.clear()
        self._remainder.clear()
        self.triggered.clear()
        self.detected_at_perf = None

    def feed_frame(self, pcm: bytes) -> bool:
        """Inspect incoming microphone PCM16 while speaking; returns True once barge-in triggers."""
        if not pcm or self.triggered.is_set():
            return self.triggered.is_set()
        self._remainder.extend(pcm)
        while len(self._remainder) >= self.chunk_bytes:
            chunk = bytes(self._remainder[: self.chunk_bytes])
            del self._remainder[: self.chunk_bytes]
            prob = self.vad.speech_probability(chunk)
            if prob >= self.threshold:
                self._speech_ms += self.chunk_ms
                self._captured.append(chunk)
                if self._speech_ms >= self.min_speech_ms:
                    self.detected_at_perf = time.perf_counter()
                    self.triggered.set()
                    if self.on_barge_in is not None:
                        self.on_barge_in(b"".join(self._captured))
                    return True
            else:
                self._speech_ms = 0.0
                self._captured.clear()
        return False

    def captured_prefix(self) -> bytes:
        return b"".join(self._captured)


@dataclass
class DuplexState:
    paused_sentences: list[str] = field(default_factory=list)
    paused_language: str = "en"
    full_response_text: str = ""


__all__ = [
    "BargeInAction",
    "DuplexState",
    "FullDuplexMonitor",
    "StreamingSentenceBuffer",
    "TurnMetrics",
    "classify_barge_in",
    "iter_streaming_sentences",
]
