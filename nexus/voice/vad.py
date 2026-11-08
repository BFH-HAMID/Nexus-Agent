"""Silero VAD speech start/end detection and bounded utterance segmentation."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from .audio_io import SAMPLE_RATE, pcm16_to_float32, pcm_rms
from .config import VoiceConfig, VoiceDependencyError, VoiceError


@dataclass(frozen=True)
class VADEvent:
    kind: str  # "speech_start" | "speech_frame" | "speech_end"
    probability: float
    timestamp: float
    pcm: bytes = b""


class BaseVAD(ABC):
    chunk_samples: int = 512

    @abstractmethod
    def speech_probability(self, chunk_pcm: bytes) -> float:
        """Return speech probability in [0.0, 1.0] for one 16 kHz mono PCM16 chunk."""

    def reset(self) -> None:
        return None


class SileroVAD(BaseVAD):
    """Silero VAD wrapper with 512-sample chunking and lazy dependency loading."""

    chunk_samples = 512  # 32 ms @ 16 kHz

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        model: Callable[[Any, int], Any] | None = None,
    ) -> None:
        if sample_rate not in (8000, 16000):
            raise VoiceError("Silero VAD supports 8000 Hz or 16000 Hz audio")
        self.sample_rate = sample_rate
        self.chunk_samples = 512 if sample_rate == 16000 else 256
        self._model = model
        self._custom_model = model is not None

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        try:
            from silero_vad import load_silero_vad
        except ImportError as exc:
            raise VoiceDependencyError('Silero VAD requires pip install -e ".[voice]"') from exc
        self._model = load_silero_vad()
        return self._model

    def reset(self) -> None:
        if self._model is not None and hasattr(self._model, "reset_states"):
            self._model.reset_states()

    def speech_probability(self, chunk_pcm: bytes) -> float:
        model = self._ensure_model()
        samples = pcm16_to_float32(chunk_pcm)
        if not samples:
            return 0.0
        if self._custom_model:
            return max(0.0, min(1.0, float(model(samples, self.sample_rate))))
        try:
            import torch

            tensor = torch.tensor(samples, dtype=torch.float32)
            score = model(tensor, self.sample_rate)
            if hasattr(score, "item"):
                score = score.item()
            return max(0.0, min(1.0, float(score)))
        except ImportError as exc:
            raise VoiceDependencyError("Silero VAD runtime requires torch") from exc


class EnergyVAD(BaseVAD):
    """Deterministic RMS-energy VAD for explicit non-neural offline fallback."""

    chunk_samples = 512

    def __init__(self, *, rms_threshold: float = 450.0) -> None:
        self.rms_threshold = max(20.0, float(rms_threshold))

    def speech_probability(self, chunk_pcm: bytes) -> float:
        rms = pcm_rms(chunk_pcm)
        if rms <= 0:
            return 0.0
        return min(1.0, rms / (self.rms_threshold * 1.5))


class VADSegmenter:
    """Speech start/end state machine with pre-roll padding and silence timeout."""

    def __init__(
        self,
        vad: BaseVAD,
        *,
        sample_rate: int = SAMPLE_RATE,
        threshold: float = 0.5,
        min_speech_ms: int = 250,
        min_silence_ms: int = 700,
        speech_pad_ms: int = 200,
        max_utterance_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.vad = vad
        self.sample_rate = sample_rate
        self.threshold = float(threshold)
        self.min_speech_ms = max(0, int(min_speech_ms))
        self.min_silence_ms = max(50, int(min_silence_ms))
        self.speech_pad_ms = max(0, int(speech_pad_ms))
        self.max_utterance_seconds = max(0.5, float(max_utterance_seconds))
        self.clock = clock

        self.chunk_samples = vad.chunk_samples
        self.chunk_bytes = self.chunk_samples * 2
        self.chunk_ms = (self.chunk_samples / self.sample_rate) * 1000.0
        pad_chunks = max(0, int(round(self.speech_pad_ms / self.chunk_ms)))
        self._preroll: deque[bytes] = deque(maxlen=max(1, pad_chunks)) if pad_chunks else deque(maxlen=0)

        self._remainder = bytearray()
        self._in_speech = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._utterance_ms = 0.0
        self._collected: list[bytes] = []

    def reset(self) -> None:
        self.vad.reset()
        self._preroll.clear()
        self._remainder.clear()
        self._in_speech = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._utterance_ms = 0.0
        self._collected.clear()

    def feed(self, pcm: bytes) -> list[VADEvent]:
        """Consume arbitrary PCM16 bytes and emit speech start/frame/end events."""
        events: list[VADEvent] = []
        if not pcm:
            return events
        self._remainder.extend(pcm)
        while len(self._remainder) >= self.chunk_bytes:
            chunk = bytes(self._remainder[: self.chunk_bytes])
            del self._remainder[: self.chunk_bytes]
            prob = self.vad.speech_probability(chunk)
            now = self.clock()
            is_speech = prob >= self.threshold

            if not self._in_speech:
                if is_speech:
                    self._speech_ms += self.chunk_ms
                    self._collected.append(chunk)
                    if self._speech_ms >= self.min_speech_ms:
                        self._in_speech = True
                        self._silence_ms = 0.0
                        self._utterance_ms = self._speech_ms
                        prefix = b"".join(self._preroll) + b"".join(self._collected)
                        self._preroll.clear()
                        self._collected = [prefix]
                        events.append(VADEvent("speech_start", prob, now, prefix))
                else:
                    self._speech_ms = 0.0
                    self._collected.clear()
                    if self._preroll.maxlen:
                        self._preroll.append(chunk)
                continue

            self._collected.append(chunk)
            self._utterance_ms += self.chunk_ms
            events.append(VADEvent("speech_frame", prob, now, chunk))

            if is_speech:
                self._silence_ms = 0.0
            else:
                self._silence_ms += self.chunk_ms

            if self._silence_ms >= self.min_silence_ms or (self._utterance_ms / 1000.0) >= self.max_utterance_seconds:
                full_pcm = b"".join(self._collected)
                events.append(VADEvent("speech_end", prob, now, full_pcm))
                self.reset()
        return events

    def flush(self) -> VADEvent | None:
        """Finalize any active utterance when an audio stream ends."""
        if not self._in_speech and not self._collected:
            return None
        full_pcm = b"".join(self._collected)
        self.reset()
        if not full_pcm:
            return None
        return VADEvent("speech_end", 0.0, self.clock(), full_pcm)

    def collect_utterance(self, frames: Iterable[bytes]) -> bytes | None:
        """Read frames until speech starts and ends (or input exhausts); returns utterance PCM16."""
        self.reset()
        for frame in frames:
            for event in self.feed(frame):
                if event.kind == "speech_end":
                    return event.pcm
        final = self.flush()
        return final.pcm if final else None


def iter_vad_events(segmenter: VADSegmenter, frames: Iterable[bytes]) -> Iterator[VADEvent]:
    for frame in frames:
        yield from segmenter.feed(frame)
    final = segmenter.flush()
    if final is not None:
        yield final


def create_vad(config: VoiceConfig, *, model: Any = None) -> BaseVAD:
    if config.vad.engine == "energy":
        return EnergyVAD()
    if config.vad.engine == "silero":
        return SileroVAD(sample_rate=config.sample_rate, model=model)
    raise VoiceError(f"Unsupported VAD engine: {config.vad.engine}")


__all__ = [
    "BaseVAD",
    "EnergyVAD",
    "SileroVAD",
    "VADEvent",
    "VADSegmenter",
    "create_vad",
    "iter_vad_events",
]
