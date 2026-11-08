"""Text-to-speech providers, secret redaction before speech, language-aware routing, and playback queue."""

from __future__ import annotations

import asyncio
import io
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .audio_io import AudioClip, AudioOutputPlayer
from .config import VoiceConfig, VoiceDependencyError, VoiceError

_BENGALI_RE = re.compile(r"[\u0980-\u09FF]")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?।](?:[\"'”’)\]]*)(?:\s+|$)|\n+")
_EXTRA_SECRET_PATTERNS = (
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9._-]{20,}\b"),
    re.compile(r"(?i)\b(?:xi-api-key|elevenlabs[_-]?api[_-]?key|openai[_-]?api[_-]?key)\s*[:=]\s*[^\s\"']{8,}"),
)


def sanitize_tts_text(
    text: str,
    *,
    workspace: Path | str | None = None,
    extra_secrets: Iterable[str] = (),
) -> str:
    """Redact API keys, tokens, and known secrets BEFORE any TTS provider receives text."""
    from ..redaction import mask, secrets
    from ..studio import redact

    if not isinstance(text, str):
        raise VoiceError("TTS text must be a string")
    secret_list = list(secrets(workspace)) + [str(s) for s in extra_secrets if s]
    cleaned = mask(text, secret_list)
    cleaned = redact(cleaned)
    for pattern in _EXTRA_SECRET_PATTERNS:
        cleaned = pattern.sub("[REDACTED_SECRET]", cleaned)
    cleaned = cleaned.replace("[REDACTED_SECRET]", "redacted secret").replace("[REDACTED]", "redacted secret")
    return " ".join(cleaned.split()).strip()


def detect_text_language(text: str, hint: str | None = None) -> str:
    if hint:
        norm = hint.strip().lower()
        if norm.startswith("bn"):
            return "bn"
        if norm.startswith("en"):
            return "en"
    return "bn" if _BENGALI_RE.search(text or "") else "en"


def iter_sentences(chunks: Iterable[str], *, max_chars: int = 500) -> Iterator[str]:
    """Yield complete sentences incrementally as LLM text chunks stream in (English + Bangla '।')."""
    buffer = ""
    for chunk in chunks:
        if not isinstance(chunk, str):
            raise VoiceError("TTS text stream must yield strings")
        buffer += chunk
        while True:
            match = _SENTENCE_SPLIT_RE.search(buffer)
            if match is None:
                break
            sentence = buffer[: match.end()].strip()
            buffer = buffer[match.end() :]
            if sentence:
                yield sentence
        while len(buffer) > max_chars:
            cut = buffer.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            piece = buffer[:cut].strip()
            if piece:
                yield piece
            buffer = buffer[cut:].lstrip()
    if buffer.strip():
        yield buffer.strip()


def _clamp_rate(rate: float | None, default: float = 1.0) -> float:
    val = default if rate is None else float(rate)
    if not 0.5 <= val <= 2.0:
        raise VoiceError("TTS speaking_rate must be between 0.5 and 2.0")
    return val


def _resolve_secret(env_name: str) -> str:
    val = os.environ.get(env_name, "").strip()
    if val:
        return val
    try:
        import keyring

        from ..redaction import KEYRING_SERVICE

        stored = keyring.get_password(KEYRING_SERVICE, env_name)
        if stored and stored.strip():
            return stored.strip()
    except Exception:
        pass
    return ""


class TTSProvider(ABC):
    """Base TTS interface that enforces secret redaction prior to synthesis."""

    name: str = "tts"

    def __init__(
        self,
        *,
        speaking_rate: float = 1.0,
        workspace: Path | str | None = None,
        extra_secrets: Iterable[str] = (),
    ) -> None:
        self.speaking_rate = _clamp_rate(speaking_rate)
        self.workspace = Path(workspace).resolve() if workspace else None
        self.extra_secrets = tuple(str(s) for s in extra_secrets if s)

    def select_voice(self, language: str | None = None) -> str:
        return language or "default"

    def synthesize(
        self,
        text: str,
        *,
        language: str | None = None,
        rate: float | None = None,
    ) -> AudioClip:
        safe_text = sanitize_tts_text(text, workspace=self.workspace, extra_secrets=self.extra_secrets)
        if not safe_text:
            raise VoiceError("TTS text is empty after normalization")
        effective_lang = detect_text_language(safe_text, hint=language)
        effective_rate = _clamp_rate(rate, self.speaking_rate)
        return self._synthesize_clean(safe_text, language=effective_lang, rate=effective_rate)

    def synthesize_stream(
        self,
        chunks: Iterable[str],
        *,
        language: str | None = None,
        rate: float | None = None,
    ) -> Iterator[tuple[str, AudioClip]]:
        for sentence in iter_sentences(chunks):
            safe_sentence = sanitize_tts_text(sentence, workspace=self.workspace, extra_secrets=self.extra_secrets)
            if not safe_sentence:
                continue
            clip = self.synthesize(safe_sentence, language=language, rate=rate)
            yield safe_sentence, clip

    @abstractmethod
    def _synthesize_clean(self, text: str, *, language: str, rate: float) -> AudioClip:
        """Synthesize already-redacted text."""


class PiperTTS(TTSProvider):
    """Offline Piper TTS (supports English and optional Bangla .onnx voice models)."""

    name = "piper"

    def __init__(
        self,
        model_path: Path | str = "",
        *,
        bn_model_path: Path | str = "",
        executable: str = "piper",
        speaking_rate: float = 1.0,
        workspace: Path | str | None = None,
        extra_secrets: Iterable[str] = (),
        runner: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(speaking_rate=speaking_rate, workspace=workspace, extra_secrets=extra_secrets)
        self.model_path = str(model_path or "").strip()
        self.bn_model_path = str(bn_model_path or "").strip()
        self.executable = (executable or "piper").strip()
        self._runner = runner

    def _model_for(self, language: str | None) -> Path:
        raw = self.bn_model_path if (language == "bn" and self.bn_model_path) else self.model_path
        if not raw:
            raise VoiceError("Piper TTS requires a local .onnx model path (tts.piper_model)")
        target = Path(raw).expanduser().resolve()
        if not target.is_file() or target.is_symlink() or target.suffix.casefold() != ".onnx":
            raise VoiceError("Piper model must be a regular local .onnx file")
        return target

    def select_voice(self, language: str | None = None) -> str:
        raw = self.bn_model_path if (language == "bn" and self.bn_model_path) else self.model_path
        return Path(raw).name if raw else "piper"

    def _synthesize_clean(self, text: str, *, language: str, rate: float) -> AudioClip:
        model = self._model_for(language)
        exe = self.executable if self._runner is not None else shutil.which(self.executable)
        if not exe:
            raise VoiceDependencyError('Piper TTS executable not found; install piper-tts (`pip install -e ".[voice-offline]"`)')
        length_scale = round(1.0 / rate, 3)
        with tempfile.TemporaryDirectory(prefix="nexus-piper-") as tmpdir:
            output = Path(tmpdir) / "speech.wav"
            argv = [
                str(exe),
                "--model",
                str(model),
                "--output_file",
                str(output),
                "--length_scale",
                str(length_scale),
            ]
            if self._runner is not None:
                res = self._runner(argv, input=text.encode("utf-8"))
                if isinstance(res, AudioClip):
                    return res
                if isinstance(res, (bytes, bytearray)):
                    return AudioClip(bytes(res), "wav")
            try:
                proc = subprocess.run(
                    argv,
                    input=text.encode("utf-8"),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=45,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise VoiceError(f"Piper TTS failed ({type(exc).__name__})") from exc
            if proc.returncode or not output.is_file():
                raise VoiceError("Piper TTS refused synthesis")
            return AudioClip(output.read_bytes(), "wav")


class KokoroTTS(TTSProvider):
    """Offline Kokoro TTS provider."""

    name = "kokoro"

    def __init__(
        self,
        *,
        language: str = "a",
        voice: str = "af_heart",
        speaking_rate: float = 1.0,
        workspace: Path | str | None = None,
        extra_secrets: Iterable[str] = (),
        pipeline: Any = None,
    ) -> None:
        super().__init__(speaking_rate=speaking_rate, workspace=workspace, extra_secrets=extra_secrets)
        self.kokoro_language = (language or "a").strip()
        self.voice = (voice or "af_heart").strip()
        self._pipeline = pipeline

    def select_voice(self, language: str | None = None) -> str:
        return self.voice

    def _ensure_pipeline(self):
        if self._pipeline is not None:
            return self._pipeline
        try:
            from kokoro import KPipeline
        except ImportError as exc:
            raise VoiceDependencyError('Kokoro TTS requires pip install -e ".[voice-offline]"') from exc
        self._pipeline = KPipeline(lang_code=self.kokoro_language)
        return self._pipeline

    def _synthesize_clean(self, text: str, *, language: str, rate: float) -> AudioClip:
        pipeline = self._ensure_pipeline()
        try:
            import numpy as np

            pieces = [audio for _g, _p, audio in pipeline(text, voice=self.voice, speed=rate)]
            if not pieces:
                raise VoiceError("Kokoro TTS produced no audio")
            samples = np.concatenate([np.asarray(p, dtype=np.float32) for p in pieces])
            samples = np.clip(samples, -1.0, 1.0)
            pcm = (samples * 32767.0).astype("<i2").tobytes()
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError(f"Kokoro TTS synthesis failed ({type(exc).__name__})") from exc
        buf = io.BytesIO()
        with wave.open(buf, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24_000)
            handle.writeframes(pcm)
        return AudioClip(buf.getvalue(), "wav", sample_rate=24_000)


def _edge_rate_string(rate: float) -> str:
    pct = int(round((rate - 1.0) * 100.0))
    return f"+{pct}%" if pct >= 0 else f"{pct}%"


class EdgeTTS(TTSProvider):
    """Edge TTS with Bangla ``bn-BD`` and English language-aware voice selection."""

    name = "edge"

    def __init__(
        self,
        *,
        bn_voice: str = "bn-BD-NabanitaNeural",
        en_voice: str = "en-US-AriaNeural",
        speaking_rate: float = 1.0,
        workspace: Path | str | None = None,
        extra_secrets: Iterable[str] = (),
        communicate_factory: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(speaking_rate=speaking_rate, workspace=workspace, extra_secrets=extra_secrets)
        self.bn_voice = (bn_voice or "bn-BD-NabanitaNeural").strip()
        self.en_voice = (en_voice or "en-US-AriaNeural").strip()
        self._communicate_factory = communicate_factory

    def select_voice(self, language: str | None = None) -> str:
        return self.bn_voice if (language or "").lower().startswith("bn") else self.en_voice

    def _synthesize_clean(self, text: str, *, language: str, rate: float) -> AudioClip:
        voice = self.select_voice(language)
        rate_str = _edge_rate_string(rate)
        factory = self._communicate_factory
        if factory is None:
            try:
                import edge_tts

                factory = edge_tts.Communicate
            except ImportError as exc:
                raise VoiceDependencyError('Edge TTS requires pip install -e ".[voice]"') from exc

        async def _save(dest: Path) -> None:
            comm = factory(text, voice, rate=rate_str)
            await comm.save(str(dest))

        with tempfile.TemporaryDirectory(prefix="nexus-edge-") as tmpdir:
            output = Path(tmpdir) / "speech.mp3"
            try:
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(_save(output))
                else:
                    err: list[BaseException] = []

                    def _runner() -> None:
                        try:
                            asyncio.run(_save(output))
                        except BaseException as exc:  # pragma: no cover - thread forwarder
                            err.append(exc)

                    t = threading.Thread(target=_runner, daemon=True)
                    t.start()
                    t.join(timeout=45)
                    if err:
                        raise err[0]
            except VoiceError:
                raise
            except Exception as exc:
                raise VoiceError(f"Edge TTS failed ({type(exc).__name__})") from exc
            if not output.is_file():
                raise VoiceError("Edge TTS did not write audio output")
            return AudioClip(output.read_bytes(), "mp3")


class ElevenLabsTTS(TTSProvider):
    """Optional ElevenLabs cloud TTS using env/OS keyring credentials."""

    name = "elevenlabs"

    def __init__(
        self,
        *,
        voice_id: str,
        model: str = "eleven_multilingual_v2",
        api_key: str = "",
        api_key_env: str = "ELEVENLABS_API_KEY",
        speaking_rate: float = 1.0,
        workspace: Path | str | None = None,
        extra_secrets: Iterable[str] = (),
        http_client: Any = None,
    ) -> None:
        super().__init__(speaking_rate=speaking_rate, workspace=workspace, extra_secrets=extra_secrets)
        self.voice_id = (voice_id or "").strip()
        if not self.voice_id:
            raise VoiceError("ElevenLabs TTS requires tts.elevenlabs_voice_id")
        self.model = (model or "eleven_multilingual_v2").strip()
        self.api_key = (api_key or _resolve_secret(api_key_env)).strip()
        if not self.api_key:
            raise VoiceError(f"ElevenLabs TTS requires API key in {api_key_env} or OS keyring")
        self._client = http_client

    def select_voice(self, language: str | None = None) -> str:
        return self.voice_id

    def _synthesize_clean(self, text: str, *, language: str, rate: float) -> AudioClip:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}"
        headers = {"xi-api-key": self.api_key, "Accept": "audio/mpeg"}
        payload = {
            "text": text,
            "model_id": self.model,
            "voice_settings": {"speed": rate},
        }
        try:
            if self._client is not None:
                resp = self._client.post(
                    url,
                    params={"output_format": "mp3_44100_128"},
                    headers=headers,
                    json=payload,
                    timeout=45,
                )
            else:
                resp = httpx.post(
                    url,
                    params={"output_format": "mp3_44100_128"},
                    headers=headers,
                    json=payload,
                    timeout=45,
                    trust_env=False,
                )
            resp.raise_for_status()
            return AudioClip(resp.content, "mp3")
        except Exception as exc:
            raise VoiceError(f"ElevenLabs TTS failed ({type(exc).__name__})") from exc


class AutoTTS(TTSProvider):
    """Language-aware composite TTS routing Bangla ('bn') and English ('en') providers."""

    name = "auto"

    def __init__(
        self,
        *,
        bn_provider: TTSProvider,
        en_provider: TTSProvider,
        speaking_rate: float = 1.0,
        workspace: Path | str | None = None,
        extra_secrets: Iterable[str] = (),
    ) -> None:
        super().__init__(speaking_rate=speaking_rate, workspace=workspace, extra_secrets=extra_secrets)
        self.bn_provider = bn_provider
        self.en_provider = en_provider

    def select_voice(self, language: str | None = None) -> str:
        if (language or "").lower().startswith("bn"):
            return self.bn_provider.select_voice("bn")
        return self.en_provider.select_voice("en")

    def _synthesize_clean(self, text: str, *, language: str, rate: float) -> AudioClip:
        target = self.bn_provider if language.startswith("bn") else self.en_provider
        return target.synthesize(text, language=language, rate=rate)


@dataclass(frozen=True)
class QueuedSpeechItem:
    text: str
    language: str | None = None
    rate: float | None = None


class PlaybackQueue:
    """Thread-safe sentence playback queue supporting streaming TTS and immediate barge-in."""

    def __init__(
        self,
        provider: TTSProvider,
        player: AudioOutputPlayer | None = None,
        *,
        workspace: Path | str | None = None,
        extra_secrets: Iterable[str] = (),
        on_sentence_spoken: Callable[[str], None] | None = None,
        on_first_audio: Callable[[float], None] | None = None,
    ) -> None:
        self.provider = provider
        self.player = player or AudioOutputPlayer()
        self.workspace = Path(workspace).resolve() if workspace else provider.workspace
        self.extra_secrets = tuple(extra_secrets) or provider.extra_secrets
        self.on_sentence_spoken = on_sentence_spoken
        self.on_first_audio = on_first_audio
        self.interrupt = threading.Event()
        self._queue: queue.Queue[QueuedSpeechItem | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._active_item: QueuedSpeechItem | None = None
        self._pending: int = 0
        self._idle_event = threading.Event()
        self._idle_event.set()
        self.last_error: BaseException | None = None
        self.first_audio_perf: float | None = None
        self.spoken_sentences: list[str] = []

    def reset_turn(self) -> None:
        self.interrupt.clear()
        with self._lock:
            self.first_audio_perf = None
            self._active_item = None
            self.last_error = None
            if self._pending == 0 and self._queue.empty():
                self._idle_event.set()

    @property
    def is_speaking(self) -> bool:
        with self._lock:
            return self._active_item is not None or self._pending > 0 or not self._queue.empty()

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self.interrupt.clear()
            self._worker = threading.Thread(target=self._run, name="nexus-voice-playback", daemon=True)
            self._worker.start()

    def enqueue(self, text: str, *, language: str | None = None, rate: float | None = None) -> str:
        safe = sanitize_tts_text(text, workspace=self.workspace, extra_secrets=self.extra_secrets)
        if not safe:
            return ""
        with self._lock:
            self._pending += 1
            self._idle_event.clear()
        self._queue.put(QueuedSpeechItem(text=safe, language=language, rate=rate))
        return safe

    def enqueue_stream(
        self,
        chunks: Iterable[str],
        *,
        language: str | None = None,
        rate: float | None = None,
    ) -> list[str]:
        queued: list[str] = []
        for sentence in iter_sentences(chunks):
            if self.interrupt.is_set():
                break
            safe = self.enqueue(sentence, language=language, rate=rate)
            if safe:
                queued.append(safe)
        return queued

    def barge_in(self) -> list[QueuedSpeechItem]:
        """Stop active playback immediately (<200 ms) and return unplayed sentences."""
        self.interrupt.set()
        if hasattr(self.player, "stop"):
            try:
                self.player.stop()
            except Exception:
                pass
        dropped: list[QueuedSpeechItem] = []
        with self._lock:
            if self._active_item is not None:
                dropped.append(self._active_item)
                self._active_item = None
        while True:
            try:
                item = self._queue.get_nowait()
                self._queue.task_done()
                if item is None:
                    self._queue.put(None)
                    break
                with self._lock:
                    self._pending = max(0, self._pending - 1)
                dropped.append(item)
            except queue.Empty:
                break
        with self._lock:
            if self._active_item is None and self._pending == 0:
                self._idle_event.set()
        return dropped

    def _record_first_audio(self) -> None:
        with self._lock:
            if self.first_audio_perf is not None:
                return
            now = time.perf_counter()
            self.first_audio_perf = now
        if self.on_first_audio is not None:
            self.on_first_audio(now)

    def drain_sync(self, *, language: str | None = None, rate: float | None = None) -> list[str]:
        """Process currently queued sentences synchronously on the calling thread."""
        self.interrupt.clear()
        spoken: list[str] = []
        while not self._queue.empty():
            item = self._queue.get_nowait()
            try:
                if item is None or self.interrupt.is_set():
                    continue
                with self._lock:
                    self._active_item = item
                clip = self.provider.synthesize(item.text, language=item.language or language, rate=item.rate or rate)
                self._record_first_audio()
                interrupted = self.player.play(clip, self.interrupt)
                if interrupted:
                    break
                spoken.append(item.text)
                self.spoken_sentences.append(item.text)
                if self.on_sentence_spoken is not None:
                    self.on_sentence_spoken(item.text)
            finally:
                with self._lock:
                    if self._active_item is item:
                        self._active_item = None
                    if item is not None:
                        self._pending = max(0, self._pending - 1)
                self._queue.task_done()
        return spoken

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Wait until all queued items finish playing or timeout elapses."""
        deadline = time.monotonic() + max(0.05, float(timeout))
        while time.monotonic() < deadline:
            if self.interrupt.is_set():
                return True
            with self._lock:
                busy = self._active_item is not None or self._pending > 0 or not self._queue.empty()
            if not busy:
                return True
            self._idle_event.wait(timeout=0.01)
        return False

    def close(self, *, wait: bool = True, timeout: float = 30.0) -> None:
        self._queue.put(None)
        if wait and self._worker is not None:
            self._worker.join(timeout=timeout)
            self._worker = None

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                if self.interrupt.is_set():
                    continue
                with self._lock:
                    self._active_item = item
                clip = self.provider.synthesize(item.text, language=item.language, rate=item.rate)
                if self.interrupt.is_set():
                    continue
                self._record_first_audio()
                interrupted = self.player.play(clip, self.interrupt)
                if not interrupted:
                    self.spoken_sentences.append(item.text)
                    if self.on_sentence_spoken is not None:
                        self.on_sentence_spoken(item.text)
            except BaseException as exc:  # pragma: no cover - surfaced to pipeline
                self.last_error = exc
            finally:
                with self._lock:
                    if self._active_item is item:
                        self._active_item = None
                    if item is not None:
                        self._pending = max(0, self._pending - 1)
                    if self._active_item is None and self._pending == 0 and self._queue.empty():
                        self._idle_event.set()
                self._queue.task_done()


def create_tts(config: VoiceConfig, *, workspace: Path | str | None = None) -> TTSProvider:
    tts_cfg = config.tts
    ws = Path(workspace).resolve() if workspace else None
    if tts_cfg.provider == "piper":
        return PiperTTS(
            tts_cfg.piper_model,
            bn_model_path=tts_cfg.piper_bn_model,
            executable=tts_cfg.piper_bin,
            speaking_rate=tts_cfg.speaking_rate,
            workspace=ws,
        )
    if tts_cfg.provider == "kokoro":
        return KokoroTTS(
            language=tts_cfg.kokoro_language,
            voice=tts_cfg.kokoro_voice,
            speaking_rate=tts_cfg.speaking_rate,
            workspace=ws,
        )
    if tts_cfg.provider == "edge":
        return EdgeTTS(
            bn_voice=tts_cfg.edge_voice_bn,
            en_voice=tts_cfg.edge_voice_en,
            speaking_rate=tts_cfg.speaking_rate,
            workspace=ws,
        )
    if tts_cfg.provider == "elevenlabs":
        return ElevenLabsTTS(
            voice_id=tts_cfg.elevenlabs_voice_id,
            model=tts_cfg.elevenlabs_model,
            api_key_env=tts_cfg.elevenlabs_api_key_env,
            speaking_rate=tts_cfg.speaking_rate,
            workspace=ws,
        )
    if tts_cfg.provider == "auto":
        bn_provider: TTSProvider = (
            PiperTTS(
                tts_cfg.piper_bn_model,
                executable=tts_cfg.piper_bin,
                speaking_rate=tts_cfg.speaking_rate,
                workspace=ws,
            )
            if tts_cfg.piper_bn_model
            else EdgeTTS(
                bn_voice=tts_cfg.edge_voice_bn,
                en_voice=tts_cfg.edge_voice_en,
                speaking_rate=tts_cfg.speaking_rate,
                workspace=ws,
            )
        )
        en_provider: TTSProvider = (
            PiperTTS(
                tts_cfg.piper_model,
                executable=tts_cfg.piper_bin,
                speaking_rate=tts_cfg.speaking_rate,
                workspace=ws,
            )
            if tts_cfg.piper_model
            else KokoroTTS(
                language=tts_cfg.kokoro_language,
                voice=tts_cfg.kokoro_voice,
                speaking_rate=tts_cfg.speaking_rate,
                workspace=ws,
            )
        )
        return AutoTTS(
            bn_provider=bn_provider,
            en_provider=en_provider,
            speaking_rate=tts_cfg.speaking_rate,
            workspace=ws,
        )
    raise VoiceError(f"Unsupported TTS provider: {tts_cfg.provider}")


__all__ = [
    "AutoTTS",
    "EdgeTTS",
    "ElevenLabsTTS",
    "KokoroTTS",
    "PiperTTS",
    "PlaybackQueue",
    "QueuedSpeechItem",
    "TTSProvider",
    "create_tts",
    "detect_text_language",
    "iter_sentences",
    "sanitize_tts_text",
]
