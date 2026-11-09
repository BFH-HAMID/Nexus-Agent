"""Voice stop phrase ('Nexus, stop everything') matched locally before any LLM call."""

from __future__ import annotations

from types import SimpleNamespace

from nexus.computer_control import EmergencyStop
from nexus.voice.config import load_voice_config
from nexus.voice.pipeline import VoicePipeline, VoiceTurnResult
from nexus.voice.stt import TranscriptResult


class _FakeSTT:
    def __init__(self, text: str, language: str = "en"):
        self.text = text
        self.language = language

    def transcribe_stream(self, pcm, *, on_partial=None, language=None):
        return TranscriptResult(text=self.text, language=self.language)


class _FakeTTS:
    extra_secrets: tuple = ()

    def __init__(self):
        self.clips: list[str] = []

    def synthesize(self, text: str, *, language: str = "en", rate: float = 1.0):
        self.clips.append(text)
        return b"fake-pcm"


class _FakePlayer:
    def play(self, clip, interrupt_event=None):
        pass


class _FakeWake:
    def reset(self):
        pass

    def process_frame(self, frame):
        return None


class _FakeVAD:
    chunk_samples = 512

    def speech_probability(self, pcm: bytes) -> float:
        return 1.0  # always speech

    def reset(self):
        pass


def _make_pipeline(tmp_path, transcript_text: str, language: str = "en",
                   handler=None) -> VoicePipeline:
    """Build a VoicePipeline with VoiceConfig (not SimpleNamespace)."""
    # Load a minimal voice config from a temp YAML file.
    import yaml
    voice_yaml = tmp_path / "voice.yaml"
    voice_yaml.write_text(yaml.safe_dump({
        "schema": 1,
        "enabled": True,
        "wake": {"enabled": False, "engine": "disabled"},
        "stt": {"language": "auto"},
        "speaker": {"enabled": False, "require_always": False, "threshold": 0.6},
        "duplex": {"enabled": False, "barge_in_threshold": 0.5, "barge_in_min_speech_ms": 200},
        "streaming": {"fast_first_clause": True},
        "conversation": {
            "spoken_approvals": False,
            "resolve_references": False,
            "max_turns": 5,
            "approval_timeout_seconds": 30,
        },
        "acknowledgements": {"enabled": False, "min_delay_ms": 1000, "cooldown_seconds": 0, "max_per_minute": 0},
        "tts": {"speaking_rate": 1.0},
        "realtime": {"enabled": False, "provider": "local"},
    }))
    config = load_voice_config(tmp_path)

    stt = _FakeSTT(transcript_text, language)
    tts = _FakeTTS()
    pipeline = VoicePipeline(
        workspace=tmp_path,
        wake_detector=_FakeWake(),
        vad=_FakeVAD(),
        stt=stt,
        tts=tts,
        player=_FakePlayer(),
        config=config,
        persona=SimpleNamespace(
            enabled=False,
            language_policy=SimpleNamespace(
                mirror_user=True,
                allow_bangla_english_mixing=True,
                default_language="en",
            ),
            spoken_style=SimpleNamespace(max_spoken_chars=10_000),
        ),
        text_handler=handler or (lambda _t: "normal response"),
    )
    return pipeline, tts


def _run_voice(pipeline: VoicePipeline) -> VoiceTurnResult:
    # Send enough PCM16 data to pass min_speech_ms (250ms = ~8 chunks of 512 samples * 2 bytes).
    return pipeline.process_frames([b"\xff" * 16384], skip_wake=True)


def test_stop_phrase_latches_emergency_and_returns_without_llm(tmp_path):
    """'Nexus, stop everything' latches EmergencyStop and does not call LLM."""
    EmergencyStop().set(False)

    llm_called = []

    def handler(t):
        llm_called.append(t.text)
        return "I heard you."

    pipeline, _tts = _make_pipeline(tmp_path, "Nexus, stop everything", handler=handler)
    result = _run_voice(pipeline)

    assert EmergencyStop().stopped() is True
    assert result.emergency_stop is True
    assert llm_called == []  # LLM was NOT called
    assert "stopped" in (result.response_text or "").lower() or "বন্ধ" in (result.response_text or "")

    EmergencyStop().set(False)


def test_stop_phrase_stop_everything_only(tmp_path):
    """'stop everything' also matches."""
    EmergencyStop().set(False)
    pipeline, _tts = _make_pipeline(tmp_path, "stop everything")
    result = _run_voice(pipeline)
    assert EmergencyStop().stopped() is True
    assert result.emergency_stop is True
    EmergencyStop().set(False)


def test_stop_phrase_case_insensitive(tmp_path):
    EmergencyStop().set(False)
    pipeline, _tts = _make_pipeline(tmp_path, "NEXUS, STOP EVERYTHING")
    result = _run_voice(pipeline)
    assert EmergencyStop().stopped() is True
    assert result.emergency_stop is True
    EmergencyStop().set(False)


def test_stop_phrase_with_trailing_punctuation(tmp_path):
    EmergencyStop().set(False)
    pipeline, _tts = _make_pipeline(tmp_path, "Nexus, stop everything!")
    result = _run_voice(pipeline)
    assert EmergencyStop().stopped() is True
    assert result.emergency_stop is True
    EmergencyStop().set(False)


def test_non_stop_phrase_still_calls_llm(tmp_path):
    """A normal utterance reaches the handler and is NOT treated as stop phrase."""
    EmergencyStop().set(False)

    called = []

    def handler(t):
        called.append(t.text)
        return "I heard you."

    pipeline, _tts = _make_pipeline(tmp_path, "Hello, how are you?", handler=handler)
    result = _run_voice(pipeline)

    assert called == ["Hello, how are you?"]
    assert result.response_text == "I heard you."
    assert EmergencyStop().stopped() is False


def test_stop_phrase_bangla(tmp_path):
    EmergencyStop().set(False)
    pipeline, _tts = _make_pipeline(tmp_path, "Nexus, stop everything", language="bn")
    result = _run_voice(pipeline)
    assert EmergencyStop().stopped() is True
    assert result.emergency_stop is True
    # Bangla confirmation message
    assert "বন্ধ" in result.response_text
    EmergencyStop().set(False)


def test_voice_turn_result_has_emergency_stop_field():
    result = VoiceTurnResult()
    assert result.emergency_stop is False
