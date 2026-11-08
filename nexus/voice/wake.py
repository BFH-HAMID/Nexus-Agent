"""Wake-word detection (openWakeWord default 'hey nexus') and push-to-talk fallback."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audio_io import SAMPLE_RATE, pcm16_to_float32
from .config import VoiceConfig, VoiceDependencyError, VoiceError


@dataclass(frozen=True)
class WakeEvent:
    word: str
    engine: str
    score: float
    timestamp: float


class WakeDetector(ABC):
    frame_length: int = 1280  # 80 ms @ 16 kHz

    @abstractmethod
    def process_frame(self, pcm: bytes) -> WakeEvent | None:
        """Inspect one 16 kHz mono PCM16 frame and return WakeEvent when triggered."""

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


class OpenWakeWordDetector(WakeDetector):
    """openWakeWord wake-word detector with configurable word, sensitivity, and cooldown."""

    frame_length = 1280

    def __init__(
        self,
        *,
        word: str = "hey nexus",
        model_path: Path | str = "",
        sensitivity: float = 0.6,
        cooldown_seconds: float = 1.5,
        model: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.word = (word or "hey nexus").strip()
        self.sensitivity = float(sensitivity)
        if not 0.05 <= self.sensitivity <= 0.99:
            raise VoiceError("Wake sensitivity must be between 0.05 and 0.99")
        # Higher sensitivity -> lower required confidence score threshold.
        self.threshold = round(max(0.01, min(0.95, 1.0 - self.sensitivity)), 4)
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._clock = clock
        self._last_trigger: float = -1e9
        self._buffer = bytearray()
        self._model_path = str(model_path or "").strip()
        self._model = model

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise VoiceDependencyError('Wake word detection requires pip install -e ".[voice-wake]"') from exc
        kwargs: dict[str, Any] = {}
        if self._model_path:
            target = Path(self._model_path).expanduser().resolve()
            if not target.is_file() or target.is_symlink() or target.suffix.casefold() not in {".onnx", ".tflite"}:
                raise VoiceError("wake.model_path must be a local regular .onnx or .tflite file")
            kwargs["wakeword_model_paths"] = [str(target)]
            kwargs["inference_framework"] = "onnx" if target.suffix.casefold() == ".onnx" else "tflite"
        self._model = Model(**kwargs)
        return self._model

    def reset(self) -> None:
        self._buffer.clear()

    def _predict(self, chunk_bytes: bytes) -> dict[str, float]:
        model = self._ensure_model()
        try:
            import numpy as np

            arr = np.frombuffer(chunk_bytes, dtype=np.int16)
        except ImportError:
            arr = [int(v * 32767) for v in pcm16_to_float32(chunk_bytes)]
        raw = model.predict(arr)
        if not isinstance(raw, dict):
            return {}
        return {str(k): float(v) for k, v in raw.items()}

    def process_frame(self, pcm: bytes) -> WakeEvent | None:
        if not pcm:
            return None
        self._buffer.extend(pcm)
        frame_bytes = self.frame_length * 2
        triggered: WakeEvent | None = None
        while len(self._buffer) >= frame_bytes:
            chunk = bytes(self._buffer[:frame_bytes])
            del self._buffer[:frame_bytes]
            scores = self._predict(chunk)
            best_word = self.word
            best_score = 0.0
            for name, score in scores.items():
                if score > best_score:
                    best_score = score
                    best_word = name
            now = self._clock()
            if best_score >= self.threshold and (now - self._last_trigger) >= self.cooldown_seconds:
                self._last_trigger = now
                triggered = WakeEvent(
                    word=best_word or self.word,
                    engine="openwakeword",
                    score=best_score,
                    timestamp=now,
                )
        return triggered


class PushToTalkDetector(WakeDetector):
    """Push-to-talk hotkey/manual trigger fallback with cooldown."""

    frame_length = 512

    def __init__(
        self,
        *,
        hotkey: str = "ctrl+space",
        cooldown_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.hotkey = (hotkey or "ctrl+space").strip()
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._clock = clock
        self._last_trigger: float = -1e9
        self._armed = False
        self._listener = None

    def trigger(self) -> bool:
        now = self._clock()
        if (now - self._last_trigger) < self.cooldown_seconds:
            return False
        self._armed = True
        return True

    def start_hotkey_listener(self) -> None:
        """Start optional global hotkey listener via ``pynput`` only when explicitly requested."""
        try:
            from pynput import keyboard
        except ImportError as exc:
            raise VoiceDependencyError("Push-to-talk global hotkey requires pynput") from exc
        combo = "+".join(f"<{part}>" if len(part) > 1 else part for part in self.hotkey.lower().split("+"))
        hotkeys = keyboard.GlobalHotKeys({combo: self.trigger})
        hotkeys.start()
        self._listener = hotkeys

    def stop_hotkey_listener(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            finally:
                self._listener = None

    def close(self) -> None:
        self.stop_hotkey_listener()

    def reset(self) -> None:
        self._armed = False

    def process_frame(self, pcm: bytes) -> WakeEvent | None:
        if not self._armed:
            return None
        now = self._clock()
        self._armed = False
        if (now - self._last_trigger) < self.cooldown_seconds:
            return None
        self._last_trigger = now
        return WakeEvent(word=self.hotkey, engine="ptt", score=1.0, timestamp=now)


class DisabledWakeDetector(WakeDetector):
    """Immediately fires once per utterance session when wake-word gating is disabled."""

    frame_length = 512

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._fired = False

    def reset(self) -> None:
        self._fired = False

    def process_frame(self, pcm: bytes) -> WakeEvent | None:
        if self._fired or not pcm:
            return None
        self._fired = True
        return WakeEvent(word="direct", engine="disabled", score=1.0, timestamp=self._clock())


def create_wake_detector(config: VoiceConfig, *, clock: Callable[[], float] = time.monotonic) -> WakeDetector:
    wake_cfg = config.wake
    if not wake_cfg.enabled or wake_cfg.engine == "disabled":
        return DisabledWakeDetector(clock=clock)
    if wake_cfg.engine == "ptt":
        return PushToTalkDetector(
            hotkey=wake_cfg.push_to_talk_key,
            cooldown_seconds=wake_cfg.cooldown_seconds,
            clock=clock,
        )
    if wake_cfg.engine == "openwakeword":
        return OpenWakeWordDetector(
            word=wake_cfg.word,
            model_path=wake_cfg.model_path,
            sensitivity=wake_cfg.sensitivity,
            cooldown_seconds=wake_cfg.cooldown_seconds,
            clock=clock,
        )
    raise VoiceError(f"Unsupported wake engine: {wake_cfg.engine}")


__all__ = [
    "SAMPLE_RATE",
    "DisabledWakeDetector",
    "OpenWakeWordDetector",
    "PushToTalkDetector",
    "WakeDetector",
    "WakeEvent",
    "create_wake_detector",
]
