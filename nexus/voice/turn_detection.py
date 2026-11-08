"""Adaptive VAD + semantic end-of-turn detection so speakers are not cut off mid-thought."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .audio_io import SAMPLE_RATE
from .vad import BaseVAD, VADEvent, VADSegmenter

_TRAILING_FILLERS = frozenset({
    "um",
    "umm",
    "uh",
    "uhh",
    "er",
    "erm",
    "hmm",
    "hm",
    "ah",
    "ahh",
    "well",
    "like",
    "actually",
    "basically",
    "wait",
    # Bangla & romanized Bangla fillers
    "উম",
    "আহ",
    "হুম",
    "ইয়ে",
    "মানে",
})

_TRAILING_CONNECTORS = frozenset({
    "and",
    "or",
    "but",
    "so",
    "because",
    "since",
    "if",
    "when",
    "while",
    "although",
    "though",
    "unless",
    "until",
    "then",
    "with",
    "without",
    "for",
    "to",
    "of",
    "in",
    "on",
    "at",
    "from",
    "into",
    "about",
    "that",
    "which",
    "who",
    "where",
    "the",
    "a",
    "an",
    "my",
    "your",
    "their",
    "our",
    # Bangla connectors
    "এবং",
    "আর",
    "কিন্তু",
    "কারণ",
    "তবে",
    "যদি",
    "তাহলে",
    "নাকি",
    "অথবা",
    "আরো",
    "সাথে",
    "দিয়ে",
    "জন্য",
})

_COMPLETE_INTENTS = frozenset({
    "yes",
    "no",
    "ok",
    "okay",
    "stop",
    "cancel",
    "wait",
    "continue",
    "resume",
    "yes do it",
    "no cancel",
    "approve",
    "deny",
    "হ্যাঁ",
    "না",
    "থামো",
    "বাতিল",
    "দাঁড়াও",
    "হ্যাঁ করো",
    "না বাতিল",
})

_TRAILING_OPEN_PUNCT_RE = re.compile(r"(?:[,;:\-—]|\.\.\.|…|\(|\[|\"|'|`)\s*$")
_TERMINAL_PUNCT_RE = re.compile(r"[.!?।]\s*$")
_WORD_TOKEN_RE = re.compile(r"[\w\u0980-\u09FF']+")


@dataclass(frozen=True)
class TurnCheckResult:
    complete: bool
    reason: str
    recommended_silence_ms: int
    confidence: float = 0.8


class SemanticTurnDetector:
    """Inspect partial/candidate transcript text for semantic completeness."""

    def __init__(
        self,
        *,
        base_silence_ms: int = 700,
        incomplete_extension_ms: int = 900,
        max_adaptive_silence_ms: int = 2400,
    ) -> None:
        self.base_silence_ms = max(150, int(base_silence_ms))
        self.incomplete_extension_ms = max(100, int(incomplete_extension_ms))
        self.max_adaptive_silence_ms = max(self.base_silence_ms, int(max_adaptive_silence_ms))

    def check(self, text: str, *, baseline_ms: int | None = None) -> TurnCheckResult:
        baseline = self.base_silence_ms if baseline_ms is None else max(150, int(baseline_ms))
        extended = min(self.max_adaptive_silence_ms, baseline + self.incomplete_extension_ms)
        cleaned = " ".join((text or "").split()).strip()
        if not cleaned:
            return TurnCheckResult(False, "empty_transcript", extended, confidence=0.3)

        lowered = cleaned.lower().strip()
        # Explicit short complete intents (except standalone 'wait' if trailing after hesitation? No, 'wait' is a complete intent).
        norm_no_punct = lowered.rstrip(".!?।")
        if norm_no_punct in _COMPLETE_INTENTS:
            return TurnCheckResult(True, "complete_intent", baseline, confidence=0.95)

        if _TRAILING_OPEN_PUNCT_RE.search(cleaned):
            return TurnCheckResult(False, "trailing_open_punctuation", extended, confidence=0.9)

        # Unbalanced brackets or quotes indicate the sentence is mid-clause.
        for left, right in (("(", ")"), ("[", "]"), ("{", "}")):
            if cleaned.count(left) > cleaned.count(right):
                return TurnCheckResult(False, "unclosed_delimiter", extended, confidence=0.9)
        if cleaned.count('"') % 2 == 1 or cleaned.count("`") % 2 == 1:
            return TurnCheckResult(False, "unclosed_quote", extended, confidence=0.85)

        tokens = _WORD_TOKEN_RE.findall(norm_no_punct)
        if not tokens:
            return TurnCheckResult(False, "no_words", extended, confidence=0.3)
        last_word = tokens[-1]

        if last_word in _TRAILING_FILLERS:
            return TurnCheckResult(False, f"trailing_filler:{last_word}", extended, confidence=0.92)
        if last_word in _TRAILING_CONNECTORS:
            return TurnCheckResult(False, f"trailing_connector:{last_word}", extended, confidence=0.9)

        if _TERMINAL_PUNCT_RE.search(cleaned):
            return TurnCheckResult(True, "terminal_punctuation", max(200, int(baseline * 0.85)), confidence=0.95)

        # Single-word non-intent fragment is often an interrupted clause unless clearly imperative.
        if len(tokens) == 1 and last_word not in {"help", "status", "hi", "hello", "hey", "thanks"}:
            return TurnCheckResult(False, "single_fragment", min(self.max_adaptive_silence_ms, baseline + 500), 0.6)

        return TurnCheckResult(True, "complete_clause", baseline, confidence=0.8)


class AdaptiveSilenceTracker:
    """Tracks user intra-utterance pause cadence and adjusts end-of-turn silence threshold."""

    def __init__(
        self,
        *,
        base_silence_ms: int = 700,
        min_silence_ms: int = 450,
        max_silence_ms: int = 2400,
        alpha: float = 0.3,
    ) -> None:
        self.base_silence_ms = max(150, int(base_silence_ms))
        self.min_silence_ms = max(150, min(self.base_silence_ms, int(min_silence_ms)))
        self.max_silence_ms = max(self.base_silence_ms, int(max_silence_ms))
        self.alpha = max(0.05, min(0.9, float(alpha)))
        self._ema_pause_ms: float = float(self.base_silence_ms * 0.55)
        self._observations: int = 0

    @property
    def current_baseline_ms(self) -> int:
        # Baseline stays slightly above typical intra-utterance pauses so natural breaths are tolerated.
        if self._observations == 0:
            return self.base_silence_ms
        adapted = int(round(max(self.base_silence_ms * 0.75, self._ema_pause_ms * 1.45)))
        return max(self.min_silence_ms, min(int(self.max_silence_ms * 0.75), adapted))

    def observe_pause(self, pause_ms: float) -> None:
        """Record a brief pause inside an ongoing utterance where the speaker resumed."""
        clamped = max(60.0, min(float(self.max_silence_ms), float(pause_ms)))
        if self._observations == 0:
            self._ema_pause_ms = clamped
        else:
            self._ema_pause_ms = (1.0 - self.alpha) * self._ema_pause_ms + self.alpha * clamped
        self._observations += 1

    def silence_threshold_for(
        self,
        partial_text: str,
        detector: SemanticTurnDetector,
    ) -> TurnCheckResult:
        return detector.check(partial_text, baseline_ms=self.current_baseline_ms)


class TurnTakingSegmenter:
    """Two-stage segmenter combining VAD with semantic turn completion & adaptive silence."""

    def __init__(
        self,
        vad: BaseVAD,
        *,
        sample_rate: int = SAMPLE_RATE,
        threshold: float = 0.5,
        min_speech_ms: int = 250,
        base_silence_ms: int = 700,
        min_silence_ms: int = 450,
        max_silence_ms: int = 2400,
        incomplete_extension_ms: int = 900,
        speech_pad_ms: int = 200,
        max_utterance_seconds: float = 30.0,
        partial_transcriber: Callable[[bytes], str] | None = None,
        semantic_detector: SemanticTurnDetector | None = None,
        silence_tracker: AdaptiveSilenceTracker | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.vad = vad
        self.sample_rate = sample_rate
        self.threshold = float(threshold)
        self.min_speech_ms = max(0, int(min_speech_ms))
        self.max_utterance_seconds = max(0.5, float(max_utterance_seconds))
        self.partial_transcriber = partial_transcriber
        self.semantic = semantic_detector or SemanticTurnDetector(
            base_silence_ms=base_silence_ms,
            incomplete_extension_ms=incomplete_extension_ms,
            max_adaptive_silence_ms=max_silence_ms,
        )
        self.tracker = silence_tracker or AdaptiveSilenceTracker(
            base_silence_ms=base_silence_ms,
            min_silence_ms=min_silence_ms,
            max_silence_ms=max_silence_ms,
        )
        self.clock = clock
        # Underlying base segmenter runs with the adaptive cap as its hard ceiling while we check earlier!
        self._base = VADSegmenter(
            vad,
            sample_rate=sample_rate,
            threshold=threshold,
            min_speech_ms=min_speech_ms,
            min_silence_ms=max_silence_ms,
            speech_pad_ms=speech_pad_ms,
            max_utterance_seconds=max_utterance_seconds,
            clock=clock,
        )
        self.chunk_samples = self._base.chunk_samples
        self.chunk_bytes = self._base.chunk_bytes
        self.chunk_ms = self._base.chunk_ms
        self._in_speech = False
        self._silence_ms = 0.0
        self._had_pause_checked = False
        self._active_target_ms = self.tracker.current_baseline_ms
        self._utterance_chunks: list[bytes] = []
        self.last_turn_check: TurnCheckResult | None = None

    def reset(self) -> None:
        self._base.reset()
        self._in_speech = False
        self._silence_ms = 0.0
        self._had_pause_checked = False
        self._active_target_ms = self.tracker.current_baseline_ms
        self._utterance_chunks.clear()

    def feed(self, pcm: bytes) -> list[VADEvent]:
        events: list[VADEvent] = []
        if not pcm:
            return events
        # Process chunk-aligned through base segmenter, inspecting silence boundaries for semantic turn check.
        offset = 0
        while offset < len(pcm):
            step = pcm[offset : offset + self.chunk_bytes]
            offset += len(step)
            base_events = self._base.feed(step)
            for ev in base_events:
                if ev.kind == "speech_start":
                    self._in_speech = True
                    self._silence_ms = 0.0
                    self._had_pause_checked = False
                    self._active_target_ms = self.tracker.current_baseline_ms
                    self._utterance_chunks = [ev.pcm]
                    events.append(ev)
                elif ev.kind == "speech_frame":
                    self._utterance_chunks.append(ev.pcm)
                    if ev.probability >= self.threshold:
                        if self._silence_ms >= 80.0:
                            self.tracker.observe_pause(self._silence_ms)
                        self._silence_ms = 0.0
                        self._had_pause_checked = False
                        self._active_target_ms = self.tracker.current_baseline_ms
                        events.append(ev)
                    else:
                        self._silence_ms += self.chunk_ms
                        events.append(ev)
                        baseline = self.tracker.current_baseline_ms
                        if not self._had_pause_checked and self._silence_ms >= baseline:
                            self._had_pause_checked = True
                            if self.partial_transcriber is not None:
                                partial_text = ""
                                try:
                                    partial_text = self.partial_transcriber(b"".join(self._utterance_chunks))
                                except Exception:
                                    partial_text = ""
                                check = self.tracker.silence_threshold_for(partial_text, self.semantic)
                                self.last_turn_check = check
                                self._active_target_ms = check.recommended_silence_ms
                            else:
                                self._active_target_ms = baseline
                        if self._silence_ms >= self._active_target_ms:
                            full_pcm = b"".join(self._utterance_chunks)
                            events.append(VADEvent("speech_end", ev.probability, ev.timestamp, full_pcm))
                            self.reset()
                elif ev.kind == "speech_end":
                    if self._in_speech:
                        events.append(ev)
                        self.reset()
        return events

    def flush(self) -> VADEvent | None:
        if self._in_speech and self._utterance_chunks:
            full_pcm = b"".join(self._utterance_chunks)
            self.reset()
            return VADEvent("speech_end", 0.0, self.clock(), full_pcm)
        return self._base.flush()

    def collect_utterance(self, frames: Iterable[bytes]) -> bytes | None:
        self.reset()
        for frame in frames:
            for ev in self.feed(frame):
                if ev.kind == "speech_end":
                    return ev.pcm
        final = self.flush()
        return final.pcm if final else None


__all__ = [
    "AdaptiveSilenceTracker",
    "SemanticTurnDetector",
    "TurnCheckResult",
    "TurnTakingSegmenter",
]
