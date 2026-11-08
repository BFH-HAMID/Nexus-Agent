"""Speech-to-text providers: local faster-whisper (auto Bangla/English/mixed) and optional cloud STT."""

from __future__ import annotations

import os
import re
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .audio_io import SAMPLE_RATE, read_wav_pcm, write_wav
from .config import VoiceConfig, VoiceDependencyError, VoiceError

_BENGALI_RE = re.compile(r"[\u0980-\u09FF]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")


@dataclass(frozen=True)
class TranscriptSegment:
    text: str
    start: float = 0.0
    end: float = 0.0
    language: str = "unknown"
    is_final: bool = True
    confidence: float | None = None


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    language: str = "unknown"
    confidence: float | None = None
    duration_seconds: float = 0.0
    languages: tuple[str, ...] = ()
    mixed: bool = False
    segments: tuple[TranscriptSegment, ...] = field(default_factory=tuple)
    provider: str = ""


def detect_spoken_languages(text: str, reported_language: str | None = None) -> tuple[str, tuple[str, ...], bool]:
    """Detect Bangla, English, and mixed Bangla/English speech from transcript + model language."""
    has_bn = bool(_BENGALI_RE.search(text or ""))
    has_en = bool(_LATIN_WORD_RE.search(text or ""))
    langs: list[str] = []
    if has_bn:
        langs.append("bn")
    if has_en:
        langs.append("en")
    reported = (reported_language or "").strip().lower()
    if reported and reported not in {"unknown", "auto"} and reported not in langs and not (has_bn and has_en):
        langs.insert(0, reported)
    if has_bn and has_en:
        primary = reported if reported in {"bn", "en"} else "bn-en"
        return primary, tuple(langs), True
    if has_bn:
        return (reported if reported == "bn" else "bn"), tuple(langs or ["bn"]), False
    if has_en:
        return (reported if reported == "en" else "en"), tuple(langs or ["en"]), False
    primary = reported or "unknown"
    return primary, ((primary,) if primary != "unknown" else ()), False


def _ensure_wav_file(audio: Path | str | bytes, tmpdir: Path, *, sample_rate: int = SAMPLE_RATE) -> Path:
    if isinstance(audio, (bytes, bytearray)):
        pcm = bytes(audio)
        if pcm[:4] == b"RIFF":
            target = tmpdir / "input.wav"
            target.write_bytes(pcm)
            return target
        return write_wav(tmpdir / "input.wav", pcm, sample_rate=sample_rate)
    source = Path(audio).expanduser().resolve()
    if not source.is_file() or source.is_symlink() or source.suffix.casefold() not in {".wav", ".mp3", ".flac", ".ogg", ".m4a"}:
        raise VoiceError("Audio input must be a bounded regular WAV/MP3/FLAC/OGG/M4A file")
    if source.suffix.casefold() == ".wav":
        # Validate WAV header & bounds safely.
        read_wav_pcm(source)
    return source


class STTProvider(ABC):
    name: str = "stt"

    @abstractmethod
    def transcribe(self, audio: Path | str | bytes, *, language: str | None = None) -> TranscriptResult:
        """Transcribe WAV file path or raw 16 kHz mono PCM16 bytes."""

    def transcribe_stream(
        self,
        frames: Iterable[bytes],
        *,
        on_partial: Callable[[str], None] | None = None,
        language: str | None = None,
        partial_interval_bytes: int = 32_000,
    ) -> TranscriptResult:
        """Collect frames and emit streaming partial transcripts before returning final text."""
        collected = bytearray()
        since_partial = 0
        last_partial = ""
        for frame in frames:
            if not frame:
                continue
            collected.extend(frame)
            since_partial += len(frame)
            if on_partial is not None and since_partial >= partial_interval_bytes and len(collected) >= 6400:
                since_partial = 0
                try:
                    partial = self.transcribe(bytes(collected), language=language)
                    text = partial.text.strip()
                    if text and text != last_partial:
                        last_partial = text
                        on_partial(text)
                except VoiceError:
                    pass
        if not collected:
            raise VoiceError("No audio frames supplied for transcription")
        return self.transcribe(bytes(collected), language=language)


class FasterWhisperSTT(STTProvider):
    """Local faster-whisper STT with configurable model size and Bangla/English/mixed auto-detection."""

    name = "faster-whisper"

    def __init__(
        self,
        model_size: str = "small",
        *,
        device: str = "auto",
        compute_type: str = "auto",
        language: str = "auto",
        local_files_only: bool = False,
        model: Any = None,
    ) -> None:
        self.model_size = (model_size or "small").strip()
        self.device = (device or "auto").strip()
        self.compute_type = (compute_type or "auto").strip()
        self.language = (language or "auto").strip()
        self.local_files_only = local_files_only
        self._model = model

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VoiceDependencyError('Local STT requires voice support: pip install -e ".[voice]"') from exc
        target = self.model_size
        local_only = self.local_files_only
        candidate = Path(target).expanduser()
        if candidate.exists():
            target = str(candidate.resolve())
            local_only = True
        self._model = WhisperModel(
            target,
            device=self.device,
            compute_type=self.compute_type,
            local_files_only=local_only,
        )
        return self._model

    def transcribe(
        self,
        audio: Path | str | bytes,
        *,
        language: str | None = None,
        on_partial: Callable[[str], None] | None = None,
    ) -> TranscriptResult:
        effective_lang = language if language is not None else self.language
        whisper_lang = None if (not effective_lang or effective_lang.lower() == "auto") else effective_lang
        model = self._load()
        with tempfile.TemporaryDirectory(prefix="nexus-voice-stt-") as tmpdir:
            wav_path = _ensure_wav_file(audio, Path(tmpdir))
            try:
                raw_segments, info = model.transcribe(
                    str(wav_path),
                    language=whisper_lang,
                    vad_filter=True,
                    beam_size=1,
                )
                seg_objs: list[TranscriptSegment] = []
                pieces: list[str] = []
                reported = str(getattr(info, "language", "unknown") or "unknown")
                prob_raw = getattr(info, "language_probability", None)
                prob = float(prob_raw) if isinstance(prob_raw, (int, float)) else None
                duration = float(getattr(info, "duration", 0.0) or 0.0)
                for seg in raw_segments:
                    piece = str(getattr(seg, "text", "") or "").strip()
                    if not piece:
                        continue
                    pieces.append(piece)
                    seg_objs.append(
                        TranscriptSegment(
                            text=piece,
                            start=float(getattr(seg, "start", 0.0) or 0.0),
                            end=float(getattr(seg, "end", 0.0) or 0.0),
                            language=reported,
                            is_final=True,
                            confidence=prob,
                        )
                    )
                    if on_partial is not None:
                        on_partial(" ".join(pieces))
            except VoiceError:
                raise
            except Exception as exc:
                raise VoiceError(f"faster-whisper transcription failed ({type(exc).__name__})") from exc
        full_text = " ".join(pieces).strip()
        if not full_text:
            raise VoiceError("No speech transcript was produced")
        primary_lang, languages, mixed = detect_spoken_languages(full_text, reported)
        return TranscriptResult(
            text=full_text,
            language=primary_lang,
            confidence=prob,
            duration_seconds=duration,
            languages=languages,
            mixed=mixed,
            segments=tuple(seg_objs),
            provider=self.name,
        )

    def transcribe_stream(
        self,
        frames: Iterable[bytes],
        *,
        on_partial: Callable[[str], None] | None = None,
        language: str | None = None,
        partial_interval_bytes: int = 32_000,
    ) -> TranscriptResult:
        collected = bytearray()
        since_partial = 0
        last_partial = ""
        for frame in frames:
            if not frame:
                continue
            collected.extend(frame)
            since_partial += len(frame)
            if on_partial is not None and since_partial >= partial_interval_bytes and len(collected) >= 6400:
                since_partial = 0
                try:
                    res = self.transcribe(bytes(collected), language=language)
                    if res.text and res.text != last_partial:
                        last_partial = res.text
                        on_partial(res.text)
                except VoiceError:
                    pass
        if not collected:
            raise VoiceError("No audio frames supplied for streaming transcription")
        return self.transcribe(bytes(collected), language=language, on_partial=on_partial if not last_partial else None)


def _resolve_secret_from_env_or_keyring(env_name: str) -> str:
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


class CloudSTT(STTProvider):
    """Optional cloud STT (OpenAI-compatible /v1/audio/transcriptions endpoint)."""

    name = "cloud"

    def __init__(
        self,
        *,
        url: str = "https://api.openai.com/v1/audio/transcriptions",
        model: str = "whisper-1",
        api_key: str = "",
        api_key_env: str = "OPENAI_API_KEY",
        language: str = "auto",
        http_client: Any = None,
    ) -> None:
        self.url = (url or "https://api.openai.com/v1/audio/transcriptions").strip()
        if not self.url.startswith("https://") and not self.url.startswith("http://127.0.0.1"):
            raise VoiceError("Cloud STT URL must use https:// (or loopback http://127.0.0.1)")
        self.model = (model or "whisper-1").strip()
        self.api_key = (api_key or _resolve_secret_from_env_or_keyring(api_key_env)).strip()
        if not self.api_key:
            raise VoiceError(f"Cloud STT requires API key in {api_key_env} or OS keyring")
        self.language = (language or "auto").strip()
        self._client = http_client

    def transcribe(self, audio: Path | str | bytes, *, language: str | None = None) -> TranscriptResult:
        effective_lang = language if language is not None else self.language
        with tempfile.TemporaryDirectory(prefix="nexus-cloud-stt-") as tmpdir:
            wav_path = _ensure_wav_file(audio, Path(tmpdir))
            data: dict[str, Any] = {"model": self.model}
            if effective_lang and effective_lang.lower() != "auto":
                data["language"] = effective_lang
            headers = {"Authorization": f"Bearer {self.api_key}"}
            files = {"file": (wav_path.name, wav_path.read_bytes(), "audio/wav")}
            try:
                if self._client is not None:
                    resp = self._client.post(self.url, headers=headers, data=data, files=files, timeout=45)
                else:
                    resp = httpx.post(self.url, headers=headers, data=data, files=files, timeout=45, trust_env=False)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                raise VoiceError(f"Cloud STT request failed ({type(exc).__name__})") from exc
        text = str(payload.get("text") or "").strip()
        if not text:
            raise VoiceError("Cloud STT returned empty transcript")
        reported = str(payload.get("language") or effective_lang or "unknown")
        primary, languages, mixed = detect_spoken_languages(text, reported)
        return TranscriptResult(
            text=text,
            language=primary,
            languages=languages,
            mixed=mixed,
            segments=(TranscriptSegment(text=text, language=primary, is_final=True),),
            provider=self.name,
        )


def create_stt(config: VoiceConfig) -> STTProvider:
    stt_cfg = config.stt
    if stt_cfg.provider == "faster-whisper":
        return FasterWhisperSTT(
            model_size=stt_cfg.model_size,
            device=stt_cfg.device,
            compute_type=stt_cfg.compute_type,
            language=stt_cfg.language,
        )
    if stt_cfg.provider == "cloud":
        return CloudSTT(
            url=stt_cfg.cloud_url or "https://api.openai.com/v1/audio/transcriptions",
            api_key_env=stt_cfg.cloud_api_key_env,
            language=stt_cfg.language,
        )
    raise VoiceError(f"Unsupported STT provider: {stt_cfg.provider}")


__all__ = [
    "CloudSTT",
    "FasterWhisperSTT",
    "STTProvider",
    "TranscriptResult",
    "TranscriptSegment",
    "create_stt",
    "detect_spoken_languages",
]
