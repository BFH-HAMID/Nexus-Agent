"""Operator-owned ``voice.yaml`` configuration loader, validation, and HUD voice state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MAX_CONFIG_BYTES = 64_000
FORBIDDEN_INLINE_SECRET_KEYS = frozenset({
    "api_key",
    "api_secret",
    "access_key",
    "secret",
    "token",
    "password",
    "elevenlabs_api_key",
    "openai_api_key",
    "gemini_api_key",
    "livekit_api_key",
    "livekit_api_secret",
    "porcupine_access_key",
})
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,79}$")


class VoiceError(ValueError):
    """Base error for voice configuration, hardware, provider, or verification failures."""


class VoiceDisabledError(VoiceError):
    """Raised when voice operations are requested while ``voice.yaml`` has ``enabled: false``."""


class VoiceDependencyError(VoiceError):
    """Raised when an optional voice extra dependency is not installed."""


@dataclass(frozen=True)
class WakeConfig:
    enabled: bool = True
    engine: str = "openwakeword"  # openwakeword | ptt | disabled
    word: str = "hey nexus"
    model_path: str = ""
    sensitivity: float = 0.6
    cooldown_seconds: float = 1.5
    push_to_talk_key: str = "ctrl+space"


@dataclass(frozen=True)
class VADConfig:
    engine: str = "silero"  # silero | energy
    threshold: float = 0.5
    min_speech_ms: int = 250
    min_silence_ms: int = 700
    speech_pad_ms: int = 200
    max_utterance_seconds: float = 30.0


@dataclass(frozen=True)
class STTConfig:
    provider: str = "faster-whisper"  # faster-whisper | cloud
    model_size: str = "small"
    device: str = "auto"
    compute_type: str = "auto"
    language: str = "auto"
    cloud_url: str = ""
    cloud_api_key_env: str = "OPENAI_API_KEY"


@dataclass(frozen=True)
class TTSConfig:
    provider: str = "piper"  # piper | kokoro | edge | elevenlabs | auto
    speaking_rate: float = 1.0
    piper_model: str = ""
    piper_bn_model: str = ""
    piper_bin: str = "piper"
    kokoro_voice: str = "af_heart"
    kokoro_language: str = "a"
    edge_voice_bn: str = "bn-BD-NabanitaNeural"
    edge_voice_en: str = "en-US-AriaNeural"
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_multilingual_v2"
    elevenlabs_api_key_env: str = "ELEVENLABS_API_KEY"


@dataclass(frozen=True)
class SpeakerConfig:
    enabled: bool = False
    require_for_dangerous: bool = True
    require_always: bool = False
    threshold: float = 0.75


@dataclass(frozen=True)
class VoiceHUDConfig:
    publish_events: bool = True


@dataclass(frozen=True)
class DuplexConfig:
    enabled: bool = True
    barge_in_threshold: float = 0.55
    barge_in_min_speech_ms: int = 96
    cancel_budget_ms: int = 200


@dataclass(frozen=True)
class TurnDetectionConfig:
    semantic_eot: bool = True
    adaptive_silence: bool = True
    min_silence_ms: int = 450
    max_silence_ms: int = 2400
    incomplete_extension_ms: int = 900


@dataclass(frozen=True)
class StreamingConfig:
    enabled: bool = True
    fast_first_clause: bool = True
    target_first_audio_ms: int = 1000


@dataclass(frozen=True)
class RealtimeVoiceConfig:
    enabled: bool = False
    provider: str = "local"  # local | gemini-live | livekit | pipecat
    model: str = "gemini-2.0-flash-live-001"
    api_key_env: str = "GEMINI_API_KEY"
    livekit_url_env: str = "LIVEKIT_URL"
    livekit_api_key_env: str = "LIVEKIT_API_KEY"
    livekit_api_secret_env: str = "LIVEKIT_API_SECRET"


@dataclass(frozen=True)
class AcknowledgementConfig:
    enabled: bool = True
    min_delay_ms: int = 450
    cooldown_seconds: float = 12.0
    max_per_minute: int = 4


@dataclass(frozen=True)
class ConversationConfig:
    max_turns: int = 10
    resolve_references: bool = True
    one_question_clarification: bool = True
    spoken_approvals: bool = True
    approval_timeout_seconds: float = 120.0


@dataclass(frozen=True)
class VoiceConfig:
    schema: int = 1
    enabled: bool = False
    sample_rate: int = 16_000
    channels: int = 1
    input_device: int | str | None = None
    output_device: int | str | None = None
    wake: WakeConfig = field(default_factory=WakeConfig)
    vad: VADConfig = field(default_factory=VADConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    speaker: SpeakerConfig = field(default_factory=SpeakerConfig)
    hud: VoiceHUDConfig = field(default_factory=VoiceHUDConfig)
    duplex: DuplexConfig = field(default_factory=DuplexConfig)
    turn_detection: TurnDetectionConfig = field(default_factory=TurnDetectionConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    realtime: RealtimeVoiceConfig = field(default_factory=RealtimeVoiceConfig)
    acknowledgements: AcknowledgementConfig = field(default_factory=AcknowledgementConfig)
    conversation: ConversationConfig = field(default_factory=ConversationConfig)


def _check_mapping(data: Any, allowed: set[str], section: str) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise VoiceError(f"voice.yaml {section} must be a mapping")
    bad_secret = FORBIDDEN_INLINE_SECRET_KEYS & {str(k) for k in data}
    if bad_secret:
        raise VoiceError(f"Inline secrets are forbidden in voice.yaml ({', '.join(sorted(bad_secret))}); use .env or keyring")
    unknown = set(data) - allowed
    if unknown:
        raise VoiceError(f"Unknown voice.yaml keys in {section}: {', '.join(sorted(unknown))}")
    return data


def _require_bool(val: Any, name: str) -> bool:
    if type(val) is not bool:
        raise VoiceError(f"voice.yaml '{name}' must be a boolean")
    return val


def _require_num(val: Any, name: str, low: float, high: float) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise VoiceError(f"voice.yaml '{name}' must be a number")
    num = float(val)
    if not low <= num <= high:
        raise VoiceError(f"voice.yaml '{name}' must be between {low} and {high}")
    return num


def _require_int(val: Any, name: str, low: int, high: int) -> int:
    if type(val) is not int or not low <= val <= high:
        raise VoiceError(f"voice.yaml '{name}' must be an integer between {low} and {high}")
    return val


def _require_str(val: Any, name: str, *, max_len: int = 300) -> str:
    if not isinstance(val, str) or len(val) > max_len:
        raise VoiceError(f"voice.yaml '{name}' must be a bounded string")
    return val.strip()


def _require_device(val: Any, name: str) -> int | str | None:
    if val is None or val == "":
        return None
    if type(val) is int and 0 <= val <= 4096:
        return val
    if isinstance(val, str) and 1 <= len(val.strip()) <= 200:
        return val.strip()
    raise VoiceError(f"voice.yaml '{name}' must be null, an integer device index, or a device name")


def parse_voice_config(raw: Any) -> VoiceConfig:
    top_keys = {
        "schema",
        "enabled",
        "sample_rate",
        "channels",
        "input_device",
        "output_device",
        "wake",
        "vad",
        "stt",
        "tts",
        "speaker",
        "hud",
        "duplex",
        "turn_detection",
        "streaming",
        "realtime",
        "acknowledgements",
        "conversation",
    }
    data = _check_mapping(raw, top_keys, "root")
    schema = data.get("schema", 1)
    if type(schema) is not int or schema != 1:
        raise VoiceError("voice.yaml schema must be 1")
    enabled = _require_bool(data.get("enabled", False), "enabled")
    sample_rate = _require_int(data.get("sample_rate", 16_000), "sample_rate", 8000, 48000)
    channels = _require_int(data.get("channels", 1), "channels", 1, 2)
    input_device = _require_device(data.get("input_device"), "input_device")
    output_device = _require_device(data.get("output_device"), "output_device")

    wake_raw = _check_mapping(
        data.get("wake"),
        {"enabled", "engine", "word", "model_path", "sensitivity", "cooldown_seconds", "push_to_talk_key"},
        "wake",
    )
    wake_engine = _require_str(wake_raw.get("engine", "openwakeword"), "wake.engine", max_len=40)
    if wake_engine not in {"openwakeword", "ptt", "disabled"}:
        raise VoiceError("voice.yaml 'wake.engine' must be openwakeword, ptt, or disabled")
    wake = WakeConfig(
        enabled=_require_bool(wake_raw.get("enabled", True), "wake.enabled"),
        engine=wake_engine,
        word=_require_str(wake_raw.get("word", "hey nexus"), "wake.word", max_len=80) or "hey nexus",
        model_path=_require_str(wake_raw.get("model_path", ""), "wake.model_path", max_len=500),
        sensitivity=_require_num(wake_raw.get("sensitivity", 0.6), "wake.sensitivity", 0.05, 0.99),
        cooldown_seconds=_require_num(wake_raw.get("cooldown_seconds", 1.5), "wake.cooldown_seconds", 0.0, 60.0),
        push_to_talk_key=_require_str(wake_raw.get("push_to_talk_key", "ctrl+space"), "wake.push_to_talk_key", max_len=60)
        or "ctrl+space",
    )

    vad_raw = _check_mapping(
        data.get("vad"),
        {"engine", "threshold", "min_speech_ms", "min_silence_ms", "speech_pad_ms", "max_utterance_seconds"},
        "vad",
    )
    vad_engine = _require_str(vad_raw.get("engine", "silero"), "vad.engine", max_len=30)
    if vad_engine not in {"silero", "energy"}:
        raise VoiceError("voice.yaml 'vad.engine' must be silero or energy")
    vad = VADConfig(
        engine=vad_engine,
        threshold=_require_num(vad_raw.get("threshold", 0.5), "vad.threshold", 0.01, 0.99),
        min_speech_ms=_require_int(vad_raw.get("min_speech_ms", 250), "vad.min_speech_ms", 0, 5000),
        min_silence_ms=_require_int(vad_raw.get("min_silence_ms", 700), "vad.min_silence_ms", 100, 10000),
        speech_pad_ms=_require_int(vad_raw.get("speech_pad_ms", 200), "vad.speech_pad_ms", 0, 2000),
        max_utterance_seconds=_require_num(vad_raw.get("max_utterance_seconds", 30.0), "vad.max_utterance_seconds", 1.0, 300.0),
    )

    stt_raw = _check_mapping(
        data.get("stt"),
        {"provider", "model_size", "device", "compute_type", "language", "cloud_url", "cloud_api_key_env"},
        "stt",
    )
    stt_provider = _require_str(stt_raw.get("provider", "faster-whisper"), "stt.provider", max_len=40)
    if stt_provider not in {"faster-whisper", "cloud"}:
        raise VoiceError("voice.yaml 'stt.provider' must be faster-whisper or cloud")
    cloud_key_env = _require_str(stt_raw.get("cloud_api_key_env", "OPENAI_API_KEY"), "stt.cloud_api_key_env", max_len=80)
    if cloud_key_env and not _ENV_NAME_RE.fullmatch(cloud_key_env):
        raise VoiceError("voice.yaml 'stt.cloud_api_key_env' must be an uppercase env variable name")
    stt = STTConfig(
        provider=stt_provider,
        model_size=_require_str(stt_raw.get("model_size", "small"), "stt.model_size", max_len=500) or "small",
        device=_require_str(stt_raw.get("device", "auto"), "stt.device", max_len=40) or "auto",
        compute_type=_require_str(stt_raw.get("compute_type", "auto"), "stt.compute_type", max_len=40) or "auto",
        language=_require_str(stt_raw.get("language", "auto"), "stt.language", max_len=24) or "auto",
        cloud_url=_require_str(stt_raw.get("cloud_url", ""), "stt.cloud_url", max_len=500),
        cloud_api_key_env=cloud_key_env or "OPENAI_API_KEY",
    )

    tts_raw = _check_mapping(
        data.get("tts"),
        {
            "provider",
            "speaking_rate",
            "piper_model",
            "piper_bn_model",
            "piper_bin",
            "kokoro_voice",
            "kokoro_language",
            "edge_voice_bn",
            "edge_voice_en",
            "elevenlabs_voice_id",
            "elevenlabs_model",
            "elevenlabs_api_key_env",
        },
        "tts",
    )
    tts_provider = _require_str(tts_raw.get("provider", "piper"), "tts.provider", max_len=40)
    if tts_provider not in {"piper", "kokoro", "edge", "elevenlabs", "auto"}:
        raise VoiceError("voice.yaml 'tts.provider' must be piper, kokoro, edge, elevenlabs, or auto")
    eleven_key_env = _require_str(tts_raw.get("elevenlabs_api_key_env", "ELEVENLABS_API_KEY"), "tts.elevenlabs_api_key_env", max_len=80)
    if eleven_key_env and not _ENV_NAME_RE.fullmatch(eleven_key_env):
        raise VoiceError("voice.yaml 'tts.elevenlabs_api_key_env' must be an uppercase env variable name")
    tts = TTSConfig(
        provider=tts_provider,
        speaking_rate=_require_num(tts_raw.get("speaking_rate", 1.0), "tts.speaking_rate", 0.5, 2.0),
        piper_model=_require_str(tts_raw.get("piper_model", ""), "tts.piper_model", max_len=500),
        piper_bn_model=_require_str(tts_raw.get("piper_bn_model", ""), "tts.piper_bn_model", max_len=500),
        piper_bin=_require_str(tts_raw.get("piper_bin", "piper"), "tts.piper_bin", max_len=200) or "piper",
        kokoro_voice=_require_str(tts_raw.get("kokoro_voice", "af_heart"), "tts.kokoro_voice", max_len=80) or "af_heart",
        kokoro_language=_require_str(tts_raw.get("kokoro_language", "a"), "tts.kokoro_language", max_len=16) or "a",
        edge_voice_bn=_require_str(tts_raw.get("edge_voice_bn", "bn-BD-NabanitaNeural"), "tts.edge_voice_bn", max_len=120)
        or "bn-BD-NabanitaNeural",
        edge_voice_en=_require_str(tts_raw.get("edge_voice_en", "en-US-AriaNeural"), "tts.edge_voice_en", max_len=120)
        or "en-US-AriaNeural",
        elevenlabs_voice_id=_require_str(tts_raw.get("elevenlabs_voice_id", ""), "tts.elevenlabs_voice_id", max_len=120),
        elevenlabs_model=_require_str(tts_raw.get("elevenlabs_model", "eleven_multilingual_v2"), "tts.elevenlabs_model", max_len=120)
        or "eleven_multilingual_v2",
        elevenlabs_api_key_env=eleven_key_env or "ELEVENLABS_API_KEY",
    )

    speaker_raw = _check_mapping(
        data.get("speaker"),
        {"enabled", "require_for_dangerous", "require_always", "threshold"},
        "speaker",
    )
    speaker = SpeakerConfig(
        enabled=_require_bool(speaker_raw.get("enabled", False), "speaker.enabled"),
        require_for_dangerous=_require_bool(speaker_raw.get("require_for_dangerous", True), "speaker.require_for_dangerous"),
        require_always=_require_bool(speaker_raw.get("require_always", False), "speaker.require_always"),
        threshold=_require_num(speaker_raw.get("threshold", 0.75), "speaker.threshold", 0.5, 0.99),
    )

    hud_raw = _check_mapping(data.get("hud"), {"publish_events"}, "hud")
    hud = VoiceHUDConfig(
        publish_events=_require_bool(hud_raw.get("publish_events", True), "hud.publish_events"),
    )

    duplex_raw = _check_mapping(
        data.get("duplex"),
        {"enabled", "barge_in_threshold", "barge_in_min_speech_ms", "cancel_budget_ms"},
        "duplex",
    )
    duplex = DuplexConfig(
        enabled=_require_bool(duplex_raw.get("enabled", True), "duplex.enabled"),
        barge_in_threshold=_require_num(
            duplex_raw.get("barge_in_threshold", 0.55), "duplex.barge_in_threshold", 0.1, 0.99
        ),
        barge_in_min_speech_ms=_require_int(
            duplex_raw.get("barge_in_min_speech_ms", 96), "duplex.barge_in_min_speech_ms", 32, 1000
        ),
        cancel_budget_ms=_require_int(
            duplex_raw.get("cancel_budget_ms", 200), "duplex.cancel_budget_ms", 50, 1000
        ),
    )

    td_raw = _check_mapping(
        data.get("turn_detection"),
        {"semantic_eot", "adaptive_silence", "min_silence_ms", "max_silence_ms", "incomplete_extension_ms"},
        "turn_detection",
    )
    turn_detection = TurnDetectionConfig(
        semantic_eot=_require_bool(td_raw.get("semantic_eot", True), "turn_detection.semantic_eot"),
        adaptive_silence=_require_bool(td_raw.get("adaptive_silence", True), "turn_detection.adaptive_silence"),
        min_silence_ms=_require_int(td_raw.get("min_silence_ms", 450), "turn_detection.min_silence_ms", 150, 3000),
        max_silence_ms=_require_int(td_raw.get("max_silence_ms", 2400), "turn_detection.max_silence_ms", 400, 8000),
        incomplete_extension_ms=_require_int(
            td_raw.get("incomplete_extension_ms", 900), "turn_detection.incomplete_extension_ms", 100, 4000
        ),
    )

    stream_raw = _check_mapping(
        data.get("streaming"),
        {"enabled", "fast_first_clause", "target_first_audio_ms"},
        "streaming",
    )
    streaming = StreamingConfig(
        enabled=_require_bool(stream_raw.get("enabled", True), "streaming.enabled"),
        fast_first_clause=_require_bool(stream_raw.get("fast_first_clause", True), "streaming.fast_first_clause"),
        target_first_audio_ms=_require_int(
            stream_raw.get("target_first_audio_ms", 1000), "streaming.target_first_audio_ms", 100, 10000
        ),
    )

    rt_raw = _check_mapping(
        data.get("realtime"),
        {
            "enabled",
            "provider",
            "model",
            "api_key_env",
            "livekit_url_env",
            "livekit_api_key_env",
            "livekit_api_secret_env",
        },
        "realtime",
    )
    rt_provider = _require_str(rt_raw.get("provider", "local"), "realtime.provider", max_len=32) or "local"
    if rt_provider not in {"local", "gemini-live", "livekit", "pipecat"}:
        raise VoiceError("voice.yaml 'realtime.provider' must be local, gemini-live, livekit, or pipecat")
    for env_key, default_env in (
        ("api_key_env", "GEMINI_API_KEY"),
        ("livekit_url_env", "LIVEKIT_URL"),
        ("livekit_api_key_env", "LIVEKIT_API_KEY"),
        ("livekit_api_secret_env", "LIVEKIT_API_SECRET"),
    ):
        val = _require_str(rt_raw.get(env_key, default_env), f"realtime.{env_key}", max_len=80) or default_env
        if not _ENV_NAME_RE.fullmatch(val):
            raise VoiceError(f"voice.yaml 'realtime.{env_key}' must be an uppercase env variable name")
    realtime = RealtimeVoiceConfig(
        enabled=_require_bool(rt_raw.get("enabled", False), "realtime.enabled"),
        provider=rt_provider,
        model=_require_str(rt_raw.get("model", "gemini-2.0-flash-live-001"), "realtime.model", max_len=120)
        or "gemini-2.0-flash-live-001",
        api_key_env=_require_str(rt_raw.get("api_key_env", "GEMINI_API_KEY"), "realtime.api_key_env", max_len=80)
        or "GEMINI_API_KEY",
        livekit_url_env=_require_str(rt_raw.get("livekit_url_env", "LIVEKIT_URL"), "realtime.livekit_url_env", max_len=80)
        or "LIVEKIT_URL",
        livekit_api_key_env=_require_str(
            rt_raw.get("livekit_api_key_env", "LIVEKIT_API_KEY"), "realtime.livekit_api_key_env", max_len=80
        )
        or "LIVEKIT_API_KEY",
        livekit_api_secret_env=_require_str(
            rt_raw.get("livekit_api_secret_env", "LIVEKIT_API_SECRET"), "realtime.livekit_api_secret_env", max_len=80
        )
        or "LIVEKIT_API_SECRET",
    )

    ack_raw = _check_mapping(
        data.get("acknowledgements"),
        {"enabled", "min_delay_ms", "cooldown_seconds", "max_per_minute"},
        "acknowledgements",
    )
    acknowledgements = AcknowledgementConfig(
        enabled=_require_bool(ack_raw.get("enabled", True), "acknowledgements.enabled"),
        min_delay_ms=_require_int(ack_raw.get("min_delay_ms", 450), "acknowledgements.min_delay_ms", 0, 5000),
        cooldown_seconds=_require_num(
            ack_raw.get("cooldown_seconds", 12.0), "acknowledgements.cooldown_seconds", 0.0, 300.0
        ),
        max_per_minute=_require_int(ack_raw.get("max_per_minute", 4), "acknowledgements.max_per_minute", 0, 30),
    )

    conv_raw = _check_mapping(
        data.get("conversation"),
        {
            "max_turns",
            "resolve_references",
            "one_question_clarification",
            "spoken_approvals",
            "approval_timeout_seconds",
        },
        "conversation",
    )
    conversation = ConversationConfig(
        max_turns=_require_int(conv_raw.get("max_turns", 10), "conversation.max_turns", 1, 50),
        resolve_references=_require_bool(conv_raw.get("resolve_references", True), "conversation.resolve_references"),
        one_question_clarification=_require_bool(
            conv_raw.get("one_question_clarification", True), "conversation.one_question_clarification"
        ),
        spoken_approvals=_require_bool(conv_raw.get("spoken_approvals", True), "conversation.spoken_approvals"),
        approval_timeout_seconds=_require_num(
            conv_raw.get("approval_timeout_seconds", 120.0), "conversation.approval_timeout_seconds", 5.0, 600.0
        ),
    )

    return VoiceConfig(
        schema=1,
        enabled=enabled,
        sample_rate=sample_rate,
        channels=channels,
        input_device=input_device,
        output_device=output_device,
        wake=wake,
        vad=vad,
        stt=stt,
        tts=tts,
        speaker=speaker,
        hud=hud,
        duplex=duplex,
        turn_detection=turn_detection,
        streaming=streaming,
        realtime=realtime,
        acknowledgements=acknowledgements,
        conversation=conversation,
    )


def load_voice_config(workspace: Path | str | None = None) -> VoiceConfig:
    """Load operator-owned ``voice.yaml`` from workspace (defaults to disabled when absent)."""
    root = Path(workspace or Path.cwd()).resolve()
    path = root / "voice.yaml"
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES)):
        raise VoiceError("voice.yaml must be a bounded regular operator-owned YAML file")
    if not path.exists():
        return VoiceConfig()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise VoiceError(f"Malformed voice.yaml ({type(exc).__name__})") from exc
    return parse_voice_config(raw if raw is not None else {})


def require_voice_enabled(workspace: Path | str | None = None) -> VoiceConfig:
    cfg = load_voice_config(workspace)
    if not cfg.enabled:
        raise VoiceDisabledError(
            "Voice core is disabled in voice.yaml (enabled: false). "
            "Set `enabled: true` in voice.yaml to enable voice control."
        )
    return cfg


def voice_state_dir(workspace: Path | str | None = None) -> Path:
    from ..config import nexus_home

    root = Path(workspace or Path.cwd()).resolve()
    ws_hash = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:32]
    return nexus_home().resolve() / "voice" / ws_hash


def hud_state_path(workspace: Path | str | None = None) -> Path:
    return voice_state_dir(workspace) / "state.json"


def write_hud_state(workspace: Path | str | None, payload: dict[str, Any]) -> Path:
    """Persist the latest voice state atomically for HUD consumers."""
    target = hud_state_path(workspace)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        raise VoiceError("Voice HUD state path must not be a symlink")
    data = json.dumps({"observed_at": time.time(), **payload}, ensure_ascii=False, sort_keys=True) + "\n"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def read_hud_state(workspace: Path | str | None = None) -> dict[str, Any]:
    """Read current voice HUD state safely without initializing audio hardware."""
    cfg_enabled = False
    cfg_error = ""
    try:
        cfg = load_voice_config(workspace)
        cfg_enabled = cfg.enabled
    except VoiceError as exc:
        cfg_error = str(exc)

    messages: list[dict[str, Any]] = []
    msg_path = voice_state_dir(workspace) / "hud_messages.json"
    if msg_path.exists() and not msg_path.is_symlink() and msg_path.is_file() and msg_path.stat().st_size <= 256_000:
        try:
            loaded_msgs = json.loads(msg_path.read_text(encoding="utf-8"))
            if isinstance(loaded_msgs, list):
                messages = loaded_msgs[-10:]
        except (OSError, json.JSONDecodeError):
            pass

    target = hud_state_path(workspace)
    if target.exists() and not target.is_symlink() and target.is_file() and target.stat().st_size <= 64_000:
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return {
                    "enabled": cfg_enabled,
                    **loaded,
                    **({"messages": messages} if messages else {}),
                    **({"config_error": cfg_error} if cfg_error else {}),
                }
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "enabled": cfg_enabled,
        "state": "idle" if cfg_enabled else "disabled",
        "observed_at": None,
        **({"messages": messages} if messages else {}),
        **({"config_error": cfg_error} if cfg_error else {}),
    }
