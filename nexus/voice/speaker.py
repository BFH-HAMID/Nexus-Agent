"""Optional speaker verification (voice-speaker extra) and DANGEROUS action voice gating."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audio_io import SAMPLE_RATE, write_wav
from .config import VoiceDependencyError, VoiceError, voice_state_dir


@dataclass(frozen=True)
class SpeakerMatch:
    matched: bool
    score: float
    threshold: float
    enrolled: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "score": round(self.score, 4),
            "threshold": self.threshold,
            "enrolled": self.enrolled,
            "reason": self.reason,
        }


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        raise VoiceError("Speaker embeddings have incompatible dimensions")
    dot = sum(float(x) * float(y) for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(y) * float(y) for y in b))
    if not na or not nb or not math.isfinite(na) or not math.isfinite(nb):
        raise VoiceError("Speaker embedding has zero or invalid norm")
    return dot / (na * nb)


def _normalize(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    if not norm or not math.isfinite(norm):
        raise VoiceError("Speaker embedding has zero or invalid norm")
    return [float(x) / norm for x in vec]


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise VoiceError("Speaker template path must not be a symlink")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class SpeakerVerifier:
    """Resemblyzer-backed speaker enrollment and verification (fail-closed when unenrolled)."""

    def __init__(
        self,
        workspace: Path | str | None = None,
        *,
        threshold: float = 0.75,
        store_path: Path | str | None = None,
        encoder: Callable[[Path], Sequence[float]] | None = None,
    ) -> None:
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.threshold = float(threshold)
        if not 0.5 <= self.threshold <= 0.99:
            raise VoiceError("Speaker verification threshold must be between 0.50 and 0.99")
        self.path = Path(store_path).resolve() if store_path else (voice_state_dir(self.workspace) / "speaker.json")
        self._custom_encoder = encoder
        self._voice_encoder = None

    def _load_resemblyzer(self):
        if self._voice_encoder is None:
            try:
                from resemblyzer import VoiceEncoder
            except ImportError as exc:
                raise VoiceDependencyError('Speaker verification requires pip install -e ".[voice-speaker]"') from exc
            self._voice_encoder = VoiceEncoder()
        return self._voice_encoder

    def _embed_path(self, wav_path: Path) -> list[float]:
        if self._custom_encoder is not None:
            raw = [float(v) for v in self._custom_encoder(wav_path)]
            if not 16 <= len(raw) <= 2048:
                raise VoiceError("Speaker encoder returned invalid embedding size")
            return _normalize(raw)
        try:
            encoder = self._load_resemblyzer()
            from resemblyzer import preprocess_wav

            vec = encoder.embed_utterance(preprocess_wav(str(wav_path)))
            raw = [float(v) for v in vec]
        except ImportError as exc:
            raise VoiceDependencyError('Speaker verification requires pip install -e ".[voice-speaker]"') from exc
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError(f"Speaker embedding failed ({type(exc).__name__})") from exc
        if not 16 <= len(raw) <= 2048:
            raise VoiceError("Speaker encoder returned invalid embedding size")
        return _normalize(raw)

    def _embed(self, audio: Path | str | bytes) -> list[float]:
        if isinstance(audio, (bytes, bytearray)):
            pcm = bytes(audio)
            with tempfile.TemporaryDirectory(prefix="nexus-spk-") as tmpdir:
                wav_path = Path(tmpdir) / "sample.wav"
                if pcm[:4] == b"RIFF":
                    wav_path.write_bytes(pcm)
                else:
                    write_wav(wav_path, pcm, sample_rate=SAMPLE_RATE)
                return self._embed_path(wav_path)
        source = Path(audio).expanduser().resolve()
        if not source.is_file() or source.is_symlink() or source.suffix.casefold() not in {".wav", ".mp3", ".flac", ".ogg", ".m4a"}:
            raise VoiceError("Speaker sample must be a bounded regular audio file")
        return self._embed_path(source)

    def is_enrolled(self) -> bool:
        return self._read_template() is not None

    def _read_template(self) -> list[float] | None:
        if not self.path.exists() or self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_size > 256_000:
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        vec = data.get("embedding") if isinstance(data, dict) else None
        if not isinstance(vec, list) or not 16 <= len(vec) <= 2048:
            return None
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) for v in vec):
            return None
        return [float(v) for v in vec]

    def enroll(self, samples: Sequence[Path | str | bytes]) -> dict[str, Any]:
        if not 1 <= len(samples) <= 5:
            raise VoiceError("Enroll with 1 to 5 voice samples")
        embeddings = [self._embed(item) for item in samples]
        dims = {len(e) for e in embeddings}
        if len(dims) != 1:
            raise VoiceError("Speaker enrollment samples produced inconsistent dimensions")
        dim = next(iter(dims))
        mean_vec = [sum(e[i] for e in embeddings) / len(embeddings) for i in range(dim)]
        template = _normalize(mean_vec)
        payload = {
            "schema": 1,
            "engine": "resemblyzer" if self._custom_encoder is None else "custom",
            "created": time.time(),
            "samples": len(samples),
            "dimensions": dim,
            "embedding": template,
        }
        _atomic_write_json(self.path, payload)
        return {
            "enrolled": True,
            "samples": len(samples),
            "dimensions": dim,
            "path": str(self.path),
        }

    def verify(self, audio: Path | str | bytes) -> SpeakerMatch:
        enrolled_vec = self._read_template()
        if enrolled_vec is None:
            return SpeakerMatch(
                matched=False,
                score=0.0,
                threshold=self.threshold,
                enrolled=False,
                reason="No enrolled voiceprint; DANGEROUS actions stay blocked",
            )
        sample_vec = self._embed(audio)
        score = _cosine_similarity(enrolled_vec, sample_vec)
        matched = bool(score >= self.threshold)
        return SpeakerMatch(
            matched=matched,
            score=score,
            threshold=self.threshold,
            enrolled=True,
            reason="enrolled speaker verified" if matched else "speaker voiceprint did not match enrolled owner",
        )

    def revoke(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False

    def status(self) -> dict[str, Any]:
        vec = self._read_template()
        return {
            "enrolled": vec is not None,
            "dimensions": len(vec) if vec is not None else 0,
            "threshold": self.threshold,
            "path": str(self.path),
        }


def is_dangerous_action(spec: Any, args: dict[str, Any] | None = None) -> bool:
    """Return True if tool call is Risk.DANGEROUS or requires fresh dangerous/external approval."""
    from ..tools import fresh_confirmation_required
    from ..types import Risk

    if getattr(spec, "risk", None) is Risk.DANGEROUS:
        return True
    return bool(fresh_confirmation_required(spec, args or {}))


def make_speaker_gate_check(
    verifier: SpeakerVerifier,
    match_supplier: Callable[[], SpeakerMatch | None],
    *,
    require_for_dangerous: bool = True,
    require_always: bool = False,
) -> Callable[[Any, dict[str, Any]], tuple[bool, str]]:
    """Build an ApprovalGate ``voice_speaker_check`` predicate.

    DANGEROUS-level actions accept ONLY an enrolled, verified owner voice.
    """

    def check(spec: Any, args: dict[str, Any]) -> tuple[bool, str]:
        dangerous = is_dangerous_action(spec, args)
        if not require_always and not (require_for_dangerous and dangerous):
            return True, ""
        if not verifier.is_enrolled():
            return False, "Enrolled speaker verification required for DANGEROUS actions; run `nexus voice enroll`"
        match = match_supplier()
        if match is None or not match.enrolled or not match.matched:
            reason = match.reason if match and match.reason else "Voice speaker did not match enrolled owner"
            return False, f"DANGEROUS action rejected: {reason}"
        return True, ""

    return check


__all__ = [
    "SpeakerMatch",
    "SpeakerVerifier",
    "is_dangerous_action",
    "make_speaker_gate_check",
]
