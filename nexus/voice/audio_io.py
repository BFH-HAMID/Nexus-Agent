"""Audio capture and playback over ``sounddevice`` using 16 kHz mono PCM16 frames."""

from __future__ import annotations

import array
import io
import math
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import VoiceDependencyError, VoiceError

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
MAX_AUDIO_BYTES = 25_000_000


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float
    hostapi: str = ""
    is_default_input: bool = False
    is_default_output: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "max_input_channels": self.max_input_channels,
            "max_output_channels": self.max_output_channels,
            "default_samplerate": self.default_samplerate,
            "hostapi": self.hostapi,
            "is_default_input": self.is_default_input,
            "is_default_output": self.is_default_output,
        }


@dataclass(frozen=True)
class AudioFrame:
    pcm: bytes
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    timestamp: float = 0.0


@dataclass(frozen=True)
class AudioClip:
    data: bytes
    format: str = "wav"
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS

    def __post_init__(self) -> None:
        if not self.data or len(self.data) > MAX_AUDIO_BYTES or self.format not in {"wav", "mp3", "ogg", "pcm"}:
            raise VoiceError("Invalid or oversized audio clip")


def _import_sounddevice(sd_module: Any = None):
    if sd_module is not None:
        return sd_module
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise VoiceDependencyError('Audio I/O requires voice support: pip install -e ".[voice]"') from exc
    return sd


def pcm16_to_float32(pcm: bytes) -> list[float]:
    if len(pcm) % 2:
        pcm = pcm[: len(pcm) - 1]
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return [sample / 32768.0 for sample in samples]


def float32_to_pcm16(samples: Sequence[float]) -> bytes:
    out = array.array("h")
    for val in samples:
        clamped = max(-1.0, min(1.0, float(val)))
        out.append(int(round(clamped * 32767.0)))
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def pcm_rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def pcm_peak(pcm: bytes) -> int:
    if len(pcm) < 2:
        return 0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder != "little":
        samples.byteswap()
    return max((abs(s) for s in samples), default=0)


def encode_wav(pcm: bytes, *, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buf.getvalue()


def write_wav(path: Path | str, pcm: bytes, *, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encode_wav(pcm, sample_rate=sample_rate, channels=channels))
    return target


def read_wav_pcm(path: Path | str) -> tuple[bytes, int, int]:
    """Read a bounded regular WAV file and normalize to 16 kHz mono PCM16 when possible."""
    target = Path(path).expanduser().resolve()
    if (
        not target.is_file()
        or target.is_symlink()
        or target.stat().st_size > MAX_AUDIO_BYTES
        or target.suffix.casefold() != ".wav"
    ):
        raise VoiceError("Audio file must be a bounded regular .wav file")
    try:
        with wave.open(str(target), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            raw = handle.readframes(handle.getnframes())
    except (wave.Error, OSError) as exc:
        raise VoiceError(f"Unreadable WAV file ({type(exc).__name__})") from exc
    if width != 2:
        raise VoiceError(f"WAV sample width must be 16-bit PCM (got {width * 8}-bit)")
    if channels not in (1, 2):
        raise VoiceError(f"Unsupported channel count: {channels}")
    if channels == 2:
        stereo = array.array("h")
        stereo.frombytes(raw[: len(raw) - (len(raw) % 4)])
        if sys.byteorder != "little":
            stereo.byteswap()
        mono = array.array("h", (int((stereo[i] + stereo[i + 1]) / 2) for i in range(0, len(stereo) - 1, 2)))
        if sys.byteorder != "little":
            mono.byteswap()
        raw = mono.tobytes()
        channels = 1
    if rate != SAMPLE_RATE:
        raw = resample_pcm16(raw, rate, SAMPLE_RATE)
        rate = SAMPLE_RATE
    return raw, rate, channels


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not pcm:
        return pcm
    if src_rate <= 0 or dst_rate <= 0:
        raise VoiceError("Sample rates must be positive")
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder != "little":
        samples.byteswap()
    n_src = len(samples)
    if n_src == 0:
        return b""
    n_dst = max(1, int(round(n_src * dst_rate / src_rate)))
    out = array.array("h")
    ratio = (n_src - 1) / max(1, n_dst - 1) if n_dst > 1 else 0.0
    for i in range(n_dst):
        pos = i * ratio
        lo = int(pos)
        hi = min(lo + 1, n_src - 1)
        frac = pos - lo
        val = samples[lo] * (1.0 - frac) + samples[hi] * frac
        out.append(max(-32768, min(32767, int(round(val)))))
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def iter_wav_frames(path: Path | str, *, frame_samples: int = 512) -> Iterator[bytes]:
    """Stream 16 kHz mono PCM16 frames from a WAV file (zero-padded final frame if partial)."""
    pcm, _rate, _ch = read_wav_pcm(path)
    frame_bytes = frame_samples * SAMPLE_WIDTH
    for idx in range(0, len(pcm), frame_bytes):
        chunk = pcm[idx : idx + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
        yield chunk


def list_devices(*, sd_module: Any = None) -> list[AudioDevice]:
    sd = _import_sounddevice(sd_module)
    try:
        devices = sd.query_devices()
        default_in, default_out = None, None
        defaults = getattr(getattr(sd, "default", None), "device", None)
        if isinstance(defaults, (list, tuple)) and len(defaults) >= 2:
            default_in, default_out = defaults[0], defaults[1]
        elif isinstance(defaults, int):
            default_in = default_out = defaults
        try:
            hostapis = sd.query_hostapis()
        except Exception:
            hostapis = []
    except Exception as exc:
        raise VoiceError(f"Failed to query audio devices ({type(exc).__name__})") from exc

    rows: list[AudioDevice] = []
    for idx, item in enumerate(devices):
        if not isinstance(item, dict):
            continue
        dev_index = int(item.get("index", idx))
        hostapi_idx = item.get("hostapi")
        hostapi_name = ""
        if isinstance(hostapi_idx, int) and isinstance(hostapis, list) and 0 <= hostapi_idx < len(hostapis):
            hostapi_name = str(hostapis[hostapi_idx].get("name", ""))
        rows.append(
            AudioDevice(
                index=dev_index,
                name=str(item.get("name", f"Device {dev_index}")),
                max_input_channels=int(item.get("max_input_channels", 0)),
                max_output_channels=int(item.get("max_output_channels", 0)),
                default_samplerate=float(item.get("default_samplerate", SAMPLE_RATE)),
                hostapi=hostapi_name,
                is_default_input=(dev_index == default_in),
                is_default_output=(dev_index == default_out),
            )
        )
    return rows


def resolve_device(device: int | str | None, *, kind: str = "input", sd_module: Any = None) -> int | None:
    if device is None or device == "":
        return None
    devices = list_devices(sd_module=sd_module)
    channel_attr = "max_input_channels" if kind == "input" else "max_output_channels"
    if isinstance(device, int):
        for dev in devices:
            if dev.index == device and getattr(dev, channel_attr) > 0:
                return dev.index
        raise VoiceError(f"Audio {kind} device index {device} is not available")
    needle = str(device).strip().casefold()
    for dev in devices:
        if getattr(dev, channel_attr) > 0 and needle == dev.name.casefold():
            return dev.index
    for dev in devices:
        if getattr(dev, channel_attr) > 0 and needle in dev.name.casefold():
            return dev.index
    raise VoiceError(f"Audio {kind} device '{device}' was not found")


class AudioInputStream:
    """Bounded 16 kHz mono PCM16 microphone stream via ``sounddevice``."""

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        frame_samples: int = 512,
        device: int | str | None = None,
        sd_module: Any = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_samples = frame_samples
        self.device = device
        self._sd_module = sd_module
        self.stop_event = stop_event or threading.Event()
        self._stream = None
        self._resolved_device: int | None = None

    def __enter__(self) -> AudioInputStream:
        sd = _import_sounddevice(self._sd_module)
        self._resolved_device = resolve_device(self.device, kind="input", sd_module=sd) if self.device is not None else None
        self._stream = sd.RawInputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            blocksize=self.frame_samples,
            device=self._resolved_device,
        )
        self._stream.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._stream is not None:
            self._stream.__exit__(exc_type, exc, tb)
            self._stream = None

    def frames(self) -> Iterator[bytes]:
        if self._stream is None:
            with self:
                yield from self._read_loop()
        else:
            yield from self._read_loop()

    def _read_loop(self) -> Iterator[bytes]:
        assert self._stream is not None
        while not self.stop_event.is_set():
            data, overflowed = self._stream.read(self.frame_samples)
            if overflowed:
                continue
            yield bytes(data)


class AudioOutputPlayer:
    """Speaker playback with ``sounddevice`` (WAV/PCM16) and external player fallback for MP3/OGG."""

    def __init__(self, *, device: int | str | None = None, sd_module: Any = None) -> None:
        self.device = device
        self._sd_module = sd_module
        self._lock = threading.Lock()
        self._active_process: subprocess.Popen[bytes] | None = None

    def play(self, clip: AudioClip, interrupt: threading.Event | None = None) -> bool:
        """Play clip; returns ``True`` if interrupted by barge-in."""
        if interrupt is not None and interrupt.is_set():
            return True
        if clip.format in {"wav", "pcm"} and (self._sd_module is not None or self._can_use_sounddevice()):
            return self._play_sounddevice(clip, interrupt)
        return self._play_subprocess(clip, interrupt)

    def _can_use_sounddevice(self) -> bool:
        try:
            _import_sounddevice(None)
            return True
        except VoiceDependencyError:
            return False

    def _play_sounddevice(self, clip: AudioClip, interrupt: threading.Event | None) -> bool:
        sd = _import_sounddevice(self._sd_module)
        dev = resolve_device(self.device, kind="output", sd_module=sd) if self.device is not None else None
        if clip.format == "wav":
            with wave.open(io.BytesIO(clip.data), "rb") as handle:
                rate = handle.getframerate()
                channels = handle.getnchannels()
                pcm = handle.readframes(handle.getnframes())
        else:
            rate = clip.sample_rate
            channels = clip.channels
            pcm = clip.data
        frame_bytes = max(2, channels * SAMPLE_WIDTH)
        chunk_bytes = frame_bytes * 512  # 32 ms chunks @ 16 kHz for <200 ms barge-in latency
        with sd.RawOutputStream(samplerate=rate, channels=channels, dtype="int16", device=dev) as stream:
            for idx in range(0, len(pcm), chunk_bytes):
                if interrupt is not None and interrupt.is_set():
                    return True
                stream.write(pcm[idx : idx + chunk_bytes])
        return bool(interrupt is not None and interrupt.is_set())

    def _play_subprocess(self, clip: AudioClip, interrupt: threading.Event | None) -> bool:
        suffix = "." + ("wav" if clip.format == "pcm" else clip.format)
        payload = encode_wav(clip.data, sample_rate=clip.sample_rate, channels=clip.channels) if clip.format == "pcm" else clip.data
        with tempfile.TemporaryDirectory(prefix="nexus-voice-play-") as tmpdir:
            path = Path(tmpdir) / f"speech{suffix}"
            path.write_bytes(payload)
            argv: list[str] = []
            ffplay = shutil.which("ffplay")
            if ffplay:
                argv = [ffplay, "-nodisp", "-autoexit", "-loglevel", "error", str(path)]
            elif suffix == ".wav" and shutil.which("aplay"):
                argv = [str(shutil.which("aplay")), "-q", str(path)]
            elif sys.platform == "darwin" and shutil.which("afplay"):
                argv = [str(shutil.which("afplay")), str(path)]
            if not argv:
                raise VoiceDependencyError(
                    'Playback requires sounddevice (pip install -e ".[voice]") or ffplay/aplay/afplay'
                )
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with self._lock:
                self._active_process = proc
            try:
                while proc.poll() is None:
                    if interrupt is not None and interrupt.wait(0.02):
                        proc.terminate()
                        try:
                            proc.wait(timeout=0.15)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        return True
                return False
            finally:
                with self._lock:
                    if self._active_process is proc:
                        self._active_process = None
                if proc.poll() is None:
                    proc.kill()


def test_microphone(
    *,
    duration_seconds: float = 2.0,
    device: int | str | None = None,
    sample_rate: int = SAMPLE_RATE,
    frame_samples: int = 512,
    save_path: Path | str | None = None,
    sd_module: Any = None,
) -> dict[str, Any]:
    """Capture a bounded mic sample and return signal diagnostics."""
    duration_seconds = max(0.1, min(30.0, float(duration_seconds)))
    target_frames = max(1, int(round(duration_seconds * sample_rate / frame_samples)))
    chunks: list[bytes] = []
    with AudioInputStream(
        sample_rate=sample_rate,
        channels=1,
        frame_samples=frame_samples,
        device=device,
        sd_module=sd_module,
    ) as stream:
        resolved = stream._resolved_device
        for idx, frame in enumerate(stream.frames()):
            chunks.append(frame)
            if idx + 1 >= target_frames:
                break
    pcm = b"".join(chunks)
    rms = pcm_rms(pcm)
    peak = pcm_peak(pcm)
    dbfs = round(20.0 * math.log10(max(1.0, rms) / 32768.0), 2) if pcm else -96.0
    saved = str(write_wav(save_path, pcm, sample_rate=sample_rate)) if save_path else ""
    return {
        "ok": bool(pcm),
        "device": resolved if resolved is not None else device,
        "sample_rate": sample_rate,
        "frames": len(chunks),
        "bytes": len(pcm),
        "duration_seconds": round(len(pcm) / (sample_rate * SAMPLE_WIDTH), 3),
        "rms": round(rms, 2),
        "peak": peak,
        "dbfs": dbfs,
        "signal_detected": rms >= 50.0,
        "saved_wav": saved,
    }


test_microphone.__test__ = False  # helper function; not a pytest test case


def make_sine_wav(
    path: Path | str,
    *,
    frequency: float = 440.0,
    duration_seconds: float = 0.5,
    amplitude: float = 0.3,
    sample_rate: int = SAMPLE_RATE,
) -> Path:
    """Helper to generate a synthetic 16 kHz mono PCM16 WAV for offline diagnostics/tests."""
    total = max(1, int(round(duration_seconds * sample_rate)))
    samples = [
        amplitude * math.sin(2.0 * math.pi * frequency * i / sample_rate)
        for i in range(total)
    ]
    return write_wav(path, float32_to_pcm16(samples), sample_rate=sample_rate)


__all__ = [
    "CHANNELS",
    "MAX_AUDIO_BYTES",
    "SAMPLE_RATE",
    "SAMPLE_WIDTH",
    "AudioClip",
    "AudioDevice",
    "AudioFrame",
    "AudioInputStream",
    "AudioOutputPlayer",
    "encode_wav",
    "float32_to_pcm16",
    "iter_wav_frames",
    "list_devices",
    "make_sine_wav",
    "pcm16_to_float32",
    "pcm_peak",
    "pcm_rms",
    "read_wav_pcm",
    "resample_pcm16",
    "resolve_device",
    "test_microphone",
    "write_wav",
]
