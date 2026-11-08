"""Explicit voice adapters: wake word, STT, TTS, barge-in and safe approval."""

from __future__ import annotations

import io
import math
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .common import AssistantError, FreshApproval, atomic_json, clean_text, read_json

AUDIO_BYTES = 25_000_000
APPROVE_WORDS = ("হ্যাঁ চালাও", "হ্যা চালাও", "অনুমোদন", "yes run", "yes approve", "approve", "confirm")
CANCEL_WORDS = ("বাতিল", "না বাতিল", "থামো", "cancel", "stop", "deny")
SENTENCE_END = re.compile(r"[.!?।](?:[\"'”’)\]]*)\s+")


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    confidence: float | None = None
    speaker: dict[str, Any] | None = None


@dataclass(frozen=True)
class AudioClip:
    data: bytes
    format: str = "wav"

    def __post_init__(self) -> None:
        if not self.data or len(self.data) > AUDIO_BYTES or self.format not in {"wav", "mp3", "ogg"}:
            raise AssistantError("Invalid or oversized synthesized audio")


def sentence_stream(chunks: Iterable[str], *, max_chars: int = 500) -> Iterator[str]:
    """Speak complete sentences before the full LLM answer has finished."""
    buffer = ""
    for chunk in chunks:
        if not isinstance(chunk, str):
            raise AssistantError("LLM stream must produce text")
        buffer += chunk
        while True:
            match = SENTENCE_END.search(buffer)
            if match is None:
                break
            sentence = buffer[:match.end()].strip()
            buffer = buffer[match.end():]
            if sentence:
                yield sentence
        while len(buffer) > max_chars:
            cut = buffer.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            yield buffer[:cut].strip()
            buffer = buffer[cut:].lstrip()
    if buffer.strip():
        yield buffer.strip()


def _audio_path(path: Path | str) -> Path:
    target = Path(path).expanduser().resolve()
    if (not target.is_file() or target.is_symlink() or target.stat().st_size > AUDIO_BYTES
            or target.suffix.casefold() not in {".wav", ".mp3", ".flac", ".ogg", ".m4a"}):
        raise AssistantError("Audio input must be a bounded regular WAV/MP3/FLAC/OGG/M4A file")
    return target


class FasterWhisperSTT:
    """Multilingual faster-whisper with ``language=None`` auto-detection."""

    def __init__(self, model_path: Path | str, *, device: str = "auto", compute_type: str = "auto"):
        if not Path(model_path).expanduser().exists():
            raise AssistantError("Use an existing local faster-whisper model directory; no implicit download")
        self.model_path = str(Path(model_path).expanduser().resolve())
        self.device, self.compute_type = device, compute_type
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise AssistantError('Install voice support: pip install -e ".[voice]"') from exc
            self._model = WhisperModel(self.model_path, device=self.device, compute_type=self.compute_type,
                                       local_files_only=True)
        return self._model

    def transcribe(self, audio: Path | str) -> Transcript:
        source = _audio_path(audio)
        try:
            segments, info = self._load().transcribe(str(source), language=None, vad_filter=True, beam_size=1)
            text = " ".join(str(segment.text).strip() for segment in segments).strip()
        except Exception as exc:
            raise AssistantError(f"Speech transcription failed ({type(exc).__name__})") from exc
        if not text or len(text) > 20_000:
            raise AssistantError("No bounded speech transcript was produced")
        probability = getattr(info, "language_probability", None)
        return Transcript(text=text, language=str(getattr(info, "language", "unknown")),
                          confidence=float(probability) if isinstance(probability, (float, int)) else None)


class OpenWakeWord:
    frame_length = 1280  # 80 ms at 16 kHz

    def __init__(self, model_path: Path | str, *, threshold: float = 0.6):
        self.model_path = _model_file(model_path, {".onnx", ".tflite"})
        self.threshold = float(threshold)
        if not 0 < self.threshold < 1:
            raise AssistantError("Wake-word threshold must be between zero and one")
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise AssistantError('Install voice support: pip install -e ".[voice-wake]"') from exc
        self.model = Model(wakeword_model_paths=[str(self.model_path)], inference_framework="onnx")

    def detect(self, frame: bytes) -> bool:
        try:
            import numpy as np

            pcm = np.frombuffer(frame, dtype=np.int16)
            if not pcm.size:
                return False
            predictions = self.model.predict(pcm)
            return any(float(score) >= self.threshold for score in predictions.values())
        except Exception as exc:
            raise AssistantError(f"Wake-word inference failed ({type(exc).__name__})") from exc


class PorcupineWakeWord:
    def __init__(self, model_path: Path | str, access_key: str, *, sensitivity: float = 0.6):
        self.model_path = _model_file(model_path, {".ppn"})
        if not access_key.strip():
            raise AssistantError("Porcupine requires an access key")
        try:
            import pvporcupine
        except ImportError as exc:
            raise AssistantError('Install voice support: pip install -e ".[voice-wake]"') from exc
        self.engine = pvporcupine.create(access_key=access_key.strip(), keyword_paths=[str(self.model_path)],
                                         sensitivities=[float(sensitivity)])
        self.frame_length = int(self.engine.frame_length)

    def detect(self, frame: bytes) -> bool:
        import array

        pcm = array.array("h")
        pcm.frombytes(frame)
        if len(pcm) != self.frame_length:
            return False
        return int(self.engine.process(pcm)) >= 0

    def close(self) -> None:
        self.engine.delete()


def _model_file(path: Path | str, suffixes: set[str]) -> Path:
    target = Path(path).expanduser().resolve()
    if not target.is_file() or target.is_symlink() or target.suffix.casefold() not in suffixes or target.stat().st_size > 100_000_000:
        raise AssistantError(f"A local regular {'/'.join(sorted(suffixes))} model file is required")
    return target


def wake_detector(engine: str, model_path: Path | str, **options):
    if engine == "openwakeword":
        return OpenWakeWord(model_path, threshold=float(options.get("threshold", 0.6)))
    if engine == "porcupine":
        return PorcupineWakeWord(model_path, str(options.get("access_key", "")),
                                 sensitivity=float(options.get("sensitivity", 0.6)))
    raise AssistantError("Wake engine must be openwakeword or porcupine")


def microphone_frames(frame_length: int, *, stop: threading.Event | None = None) -> Iterator[bytes]:
    """Yield microphone frames only inside an explicit ``listen_once`` call."""
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise AssistantError('Install microphone support: pip install -e ".[voice]"') from exc
    stop = stop or threading.Event()
    with sd.RawInputStream(samplerate=16_000, channels=1, dtype="int16", blocksize=frame_length) as stream:
        while not stop.is_set():
            data, overflowed = stream.read(frame_length)
            if overflowed:
                continue
            yield bytes(data)


def record_after_wake(frames: Iterable[bytes], destination: Path, *, max_seconds: float = 30,
                      silence_seconds: float = 0.8, frame_length: int = 1280,
                      amplitude_threshold: int = 450) -> bool:
    """Collect one bounded utterance from an already-running frame iterator."""
    import array

    started = time.monotonic()
    silent_for = 0.0
    collected: list[bytes] = []
    frame_seconds = frame_length / 16_000
    for frame in frames:
        collected.append(frame)
        pcm = array.array("h")
        pcm.frombytes(frame)
        rms = math.sqrt(sum(sample * sample for sample in pcm) / max(1, len(pcm)))
        silent_for = silent_for + frame_seconds if rms < amplitude_threshold else 0.0
        elapsed = time.monotonic() - started
        if len(collected) > 4 and silent_for >= silence_seconds:
            break
        if elapsed >= max_seconds:
            break
    if not collected:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"".join(collected))
    return True


def listen_once(detector, stt: FasterWhisperSTT, *, wake_timeout: float = 60,
                utterance_seconds: float = 30, speaker: SpeakerVerifier | None = None) -> Transcript:
    """Wait for Hey Nexus and transcribe/verify the same captured utterance."""
    stop = threading.Event()
    frames = iter(microphone_frames(detector.frame_length, stop=stop))
    deadline = time.monotonic() + wake_timeout
    try:
        while time.monotonic() < deadline:
            if detector.detect(next(frames)):
                break
        else:
            raise AssistantError("Wake word was not detected before the timeout")
        with tempfile.TemporaryDirectory(prefix="nexus-voice-") as temporary:
            audio = Path(temporary) / "utterance.wav"
            if not record_after_wake(frames, audio, max_seconds=utterance_seconds,
                                     frame_length=detector.frame_length):
                raise AssistantError("No speech followed the wake word")
            transcript = stt.transcribe(audio)
            verification = speaker.verify(audio) if speaker else None
            return Transcript(transcript.text, transcript.language, transcript.confidence, verification)
    finally:
        stop.set()


def listen_utterance(stt: FasterWhisperSTT, *, speaker: SpeakerVerifier | None = None,
                     utterance_seconds: float = 12) -> Transcript:
    """Explicit push-to-talk capture for a challenge; never waits for a wake word."""
    stop = threading.Event()
    frames = iter(microphone_frames(1280, stop=stop))
    try:
        with tempfile.TemporaryDirectory(prefix="nexus-voice-") as temporary:
            audio = Path(temporary) / "decision.wav"
            if not record_after_wake(frames, audio, max_seconds=utterance_seconds, frame_length=1280):
                raise AssistantError("No voice decision was captured")
            transcript = stt.transcribe(audio)
            verification = speaker.verify(audio) if speaker else None
            return Transcript(transcript.text, transcript.language, transcript.confidence, verification)
    finally:
        stop.set()


class SpeakerVerifier:
    """Resemblyzer embeddings with an explicit enrolment and cosine threshold."""

    def __init__(self, workspace: Path | str, *, threshold: float = 0.75):
        from .common import assistant_dir

        self.path = assistant_dir(workspace) / "speaker.json"
        self.threshold = float(threshold)
        if not 0.5 <= self.threshold <= 0.99:
            raise AssistantError("Speaker threshold must be between 0.50 and 0.99")
        self._encoder = None

    def _load(self):
        if self._encoder is None:
            try:
                from resemblyzer import VoiceEncoder
            except ImportError as exc:
                raise AssistantError('Speaker verification requires pip install -e ".[voice-speaker]"') from exc
            self._encoder = VoiceEncoder()
        return self._encoder

    def _embedding(self, audio: Path | str):
        try:
            import numpy as np
            from resemblyzer import preprocess_wav

            vector = np.asarray(self._load().embed_utterance(preprocess_wav(str(_audio_path(audio)))), dtype=float)
            if vector.size < 16 or vector.size > 2048 or not np.isfinite(vector).all():
                raise AssistantError("Speaker encoder returned an invalid embedding")
            norm = float(np.linalg.norm(vector))
            if not norm:
                raise AssistantError("Speaker embedding has zero norm")
            return vector / norm
        except AssistantError:
            raise
        except Exception as exc:
            raise AssistantError(f"Speaker embedding failed ({type(exc).__name__})") from exc

    def enroll(self, samples: list[Path | str]) -> dict[str, Any]:
        if not 1 <= len(samples) <= 5:
            raise AssistantError("Enrol with one to five voice samples")
        import numpy as np

        vectors = [self._embedding(path) for path in samples]
        if len({vector.shape for vector in vectors}) != 1:
            raise AssistantError("Voice samples produced incompatible embeddings")
        vector = np.mean(np.stack(vectors), axis=0)
        vector /= np.linalg.norm(vector)
        atomic_json(self.path, {"schema": 1, "engine": "resemblyzer", "created": time.time(),
                                "samples": len(samples), "embedding": vector.tolist()})
        return {"enrolled": True, "samples": len(samples), "dimensions": len(vector),
                "note": "Biometric template is workspace-local mode 0600; delete it to revoke voice identity."}

    def revoke(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False

    def verify(self, audio: Path | str) -> dict[str, Any]:
        stored = read_json(self.path, {})
        vector = stored.get("embedding")
        if stored.get("engine") != "resemblyzer" or not isinstance(vector, list) or not 16 <= len(vector) <= 2048:
            raise AssistantError("No valid enrolled speaker; voice approval stays disabled")
        import numpy as np

        enrolled = np.asarray(vector, dtype=float)
        sample = self._embedding(audio)
        if enrolled.shape != sample.shape or not np.isfinite(enrolled).all():
            raise AssistantError("Enrolled speaker template is invalid")
        score = float(np.dot(enrolled, sample) / (np.linalg.norm(enrolled) * np.linalg.norm(sample)))
        return {"matched": bool(score >= self.threshold), "score": score, "threshold": self.threshold}


class PiperTTS:
    def __init__(self, model_path: Path | str, *, executable: str = "piper"):
        self.model = _model_file(model_path, {".onnx"})
        self.executable = shutil.which(executable)
        if not self.executable:
            raise AssistantError("Piper executable is not installed")

    def synthesize(self, text: str) -> AudioClip:
        text = clean_text(text, 4000)
        with tempfile.TemporaryDirectory(prefix="nexus-piper-") as temporary:
            output = Path(temporary) / "speech.wav"
            try:
                result = subprocess.run([self.executable, "--model", str(self.model), "--output_file", str(output)],
                                        input=text.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        timeout=45, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AssistantError(f"Piper failed ({type(exc).__name__})") from exc
            if result.returncode or not output.is_file():
                raise AssistantError("Piper refused synthesis")
            return AudioClip(output.read_bytes(), "wav")


class KokoroTTS:
    def __init__(self, *, language: str = "a", voice: str = "af_heart"):
        self.language, self.voice = language, voice
        self._pipeline = None

    def synthesize(self, text: str) -> AudioClip:
        text = clean_text(text, 4000)
        try:
            import numpy as np
            if self._pipeline is None:
                from kokoro import KPipeline

                self._pipeline = KPipeline(lang_code=self.language)
            pieces = [audio for _graphemes, _phonemes, audio in self._pipeline(text, voice=self.voice)]
            if not pieces:
                raise AssistantError("Kokoro produced no audio")
            samples = np.concatenate([np.asarray(piece, dtype=np.float32) for piece in pieces])
            samples = np.clip(samples, -1, 1)
            pcm = (samples * 32767).astype("<i2").tobytes()
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24_000)
                output.writeframes(pcm)
            return AudioClip(buffer.getvalue(), "wav")
        except AssistantError:
            raise
        except Exception as exc:
            raise AssistantError(f"Kokoro synthesis failed ({type(exc).__name__})") from exc


class EdgeTTS:
    def __init__(self, *, voice: str = "bn-BD-NabanitaNeural"):
        self.voice = clean_text(voice, 120)

    def synthesize(self, text: str) -> AudioClip:
        text = clean_text(text, 4000)
        try:
            import asyncio

            import edge_tts
        except ImportError as exc:
            raise AssistantError("Edge TTS requires the voice extra and network access") from exc
        with tempfile.TemporaryDirectory(prefix="nexus-edge-") as temporary:
            output = Path(temporary) / "speech.mp3"
            try:
                asyncio.run(edge_tts.Communicate(text, self.voice).save(str(output)))
            except Exception as exc:
                raise AssistantError(f"Edge TTS failed ({type(exc).__name__})") from exc
            return AudioClip(output.read_bytes(), "mp3")


class ElevenLabsTTS:
    def __init__(self, api_key: str, *, voice_id: str, model: str = "eleven_multilingual_v2"):
        if not api_key.strip():
            raise AssistantError("ElevenLabs requires an API key from the native vault")
        self.api_key = api_key.strip()
        self.voice_id = clean_text(voice_id, 120)
        self.model = clean_text(model, 120)

    def synthesize(self, text: str) -> AudioClip:
        text = clean_text(text, 4000)
        try:
            response = httpx.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}",
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": self.api_key, "Accept": "audio/mpeg"},
                json={"text": text, "model_id": self.model}, timeout=45, trust_env=False,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AssistantError(f"ElevenLabs TTS failed ({type(exc).__name__})") from exc
        return AudioClip(response.content, "mp3")


class AudioPlayer:
    """Cancellable local playback; barge-in terminates only its owned process."""

    def play(self, clip: AudioClip, interrupt: threading.Event) -> bool:
        suffix = "." + clip.format
        with tempfile.TemporaryDirectory(prefix="nexus-playback-") as temporary:
            path = Path(temporary) / ("speech" + suffix)
            path.write_bytes(clip.data)
            executable = shutil.which("ffplay")
            argv = [executable, "-nodisp", "-autoexit", "-loglevel", "error", str(path)] if executable else []
            if not argv and clip.format == "wav" and shutil.which("aplay"):
                argv = [shutil.which("aplay"), "-q", str(path)]
            if not argv and sys_platform_darwin() and shutil.which("afplay"):
                argv = [shutil.which("afplay"), str(path)]
            if not argv:
                raise AssistantError("Audio playback needs ffplay, aplay (WAV), or afplay on macOS")
            try:
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
            except OSError as exc:
                raise AssistantError("Audio player could not start") from exc
            try:
                while process.poll() is None:
                    if interrupt.wait(0.05):
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        return True
                return False
            finally:
                if process.poll() is None:
                    process.kill()


def sys_platform_darwin() -> bool:
    import sys

    return sys.platform == "darwin"


class SpeechStreamer:
    def __init__(self, synthesizer, player: AudioPlayer | None = None):
        self.synthesizer = synthesizer
        self.player = player or AudioPlayer()
        self.interrupt = threading.Event()

    def stop(self) -> None:
        self.interrupt.set()

    def speak(self, llm_chunks: Iterable[str]) -> dict[str, Any]:
        self.interrupt.clear()
        spoken: list[str] = []
        interrupted = False
        for sentence in sentence_stream(llm_chunks):
            if self.interrupt.is_set():
                interrupted = True
                break
            clip = self.synthesizer.synthesize(sentence)
            interrupted = self.player.play(clip, self.interrupt)
            if interrupted:
                break
            spoken.append(sentence)
        return {"sentences": len(spoken), "spoken": spoken, "interrupted": interrupted,
                "remaining_text_retained": False}


class VoiceApproval:
    """Verified-speaker + exact challenge + one-use consume; no standing consent."""

    def __init__(self):
        self.fresh = FreshApproval()
        self._challenges: dict[str, dict[str, Any]] = {}

    def begin(self, action: str, payload: Any) -> dict[str, Any]:
        challenge = self.fresh.begin(action, payload)
        self._challenges[challenge["id"]] = challenge
        return {**challenge, "prompt": f"হ্যাঁ চালাও — {challenge['confirmation']} / yes approve — {challenge['confirmation']}"}

    def adopt(self, challenge: dict[str, Any]) -> dict[str, Any]:
        """Adopt a server-issued action challenge for immediate verified execution.

        ``decide(..., consume=False)`` only validates speech. The same trusted
        request must then consume/execute the adopted action; no grant is sent
        to the browser as authority.
        """
        if not isinstance(challenge, dict) or not challenge.get("id") or not challenge.get("confirmation"):
            raise AssistantError("A valid server action challenge is required")
        self._challenges[str(challenge["id"])] = challenge
        return challenge

    def decide(self, ident: str, transcript: str, verification: dict[str, Any], payload: Any,
               *, consume: bool = True) -> dict[str, Any]:
        challenge = self._challenges.get(ident)
        if challenge is None or not verification.get("matched"):
            return {"approved": False, "cancelled": False, "reason": "speaker or challenge verification failed"}
        text = " ".join(transcript.casefold().split())
        expected = challenge["confirmation"].casefold()
        if expected not in text:
            return {"approved": False, "cancelled": False, "reason": "the exact action challenge was not spoken"}
        cancel = any(word in text for word in CANCEL_WORDS)
        approve = any(word in text for word in APPROVE_WORDS)
        if cancel == approve:
            return {"approved": False, "cancelled": cancel, "reason": "speech was not an unambiguous approval/cancellation"}
        # Consuming on either decision prevents replay. Cancellation never executes the action.
        self._challenges.pop(ident, None)
        if not consume:
            return {"approved": bool(approve), "cancelled": bool(cancel),
                    "reason": "verified speech; the same trusted request must consume/execute now"}
        valid = self.fresh.consume(ident, challenge["confirmation"], payload)
        if not valid:
            return {"approved": False, "cancelled": False, "reason": "challenge expired or action changed"}
        return {"approved": bool(approve), "cancelled": bool(cancel), "reason": "verified one-use voice decision"}
