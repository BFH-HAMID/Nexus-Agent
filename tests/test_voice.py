"""Offline tests for the modular voice core (nexus/voice/) and CLI `nexus voice`."""

from __future__ import annotations

import json
import os
import stat
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from nexus.cli import main
from nexus.hud.core import HUD
from nexus.permissions import Permissions
from nexus.server import ServerState
from nexus.tools import ApprovalDecision, ApprovalGate, PathGuard, ToolContext, build_tools
from nexus.types import Event, EventType
from nexus.voice import (
    VoiceConfig,
    VoiceDependencyError,
    VoiceDisabledError,
    VoiceError,
    VoicePipeline,
    VoiceState,
    load_voice_config,
    read_hud_state,
)
from nexus.voice.audio_io import (
    AudioClip,
    AudioInputStream,
    AudioOutputPlayer,
    float32_to_pcm16,
    list_devices,
    make_sine_wav,
    read_wav_pcm,
    resolve_device,
    test_microphone,
    write_wav,
)
from nexus.voice.config import TTSConfig, VADConfig, WakeConfig, parse_voice_config
from nexus.voice.pipeline import build_agent_handler
from nexus.voice.speaker import SpeakerVerifier, make_speaker_gate_check
from nexus.voice.stt import CloudSTT, FasterWhisperSTT, TranscriptResult
from nexus.voice.tts import (
    AutoTTS,
    EdgeTTS,
    ElevenLabsTTS,
    KokoroTTS,
    PiperTTS,
    PlaybackQueue,
    TTSProvider,
    iter_sentences,
    sanitize_tts_text,
)
from nexus.voice.vad import EnergyVAD, SileroVAD, VADSegmenter
from nexus.voice.wake import OpenWakeWordDetector, PushToTalkDetector

from .conftest import FakeProvider, fake_registry, text_completion, tool_completion


class _FakeRawInputStream:
    def __init__(self, frames: list[bytes], **_kwargs):
        self._frames = list(frames)
        self._idx = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _n: int):
        if self._idx >= len(self._frames):
            return b"\x00\x00" * 512, False
        chunk = self._frames[self._idx]
        self._idx += 1
        return chunk, False


class _FakeRawOutputStream:
    def __init__(self, sink: list[bytes], **_kwargs):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def write(self, data: bytes):
        self._sink.append(bytes(data))


class _FakeSoundDevice:
    def __init__(self, input_frames: list[bytes] | None = None):
        self.input_frames = list(input_frames or [])
        self.played_bytes: list[bytes] = []
        self.default = SimpleNamespace(device=(0, 1))

    def query_devices(self):
        return [
            {
                "index": 0,
                "name": "Fake USB Mic",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 16000.0,
                "hostapi": 0,
            },
            {
                "index": 1,
                "name": "Fake Desk Speaker",
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000.0,
                "hostapi": 0,
            },
        ]

    def query_hostapis(self):
        return [{"name": "ALSA"}]

    def RawInputStream(self, **kwargs):
        return _FakeRawInputStream(self.input_frames, **kwargs)

    def RawOutputStream(self, **kwargs):
        return _FakeRawOutputStream(self.played_bytes, **kwargs)


class _FakeTTS(TTSProvider):
    name = "fake-tts"

    def __init__(self, *, speaking_rate: float = 1.0, workspace: Path | None = None):
        super().__init__(speaking_rate=speaking_rate, workspace=workspace)
        self.spoken: list[tuple[str, str, float]] = []

    def _synthesize_clean(self, text: str, *, language: str, rate: float) -> AudioClip:
        self.spoken.append((text, language, rate))
        pcm = float32_to_pcm16([0.1] * 320)
        buf_wav = make_sine_wav_bytes(pcm)
        return AudioClip(buf_wav, "wav")


def make_sine_wav_bytes(pcm: bytes) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(pcm)
    return buf.getvalue()


# ── 1. Operator-owned voice.yaml & permissions ──────────────────────────── #


def test_default_repo_voice_yaml_is_disabled_and_operator_owned(repo: Path):
    cfg = load_voice_config(Path(__file__).resolve().parents[1])
    assert cfg.schema == 1
    assert cfg.enabled is False
    assert cfg.wake.word == "hey nexus"

    (repo / "voice.yaml").write_text("schema: 1\nenabled: false\n", encoding="utf-8")
    (repo / "permissions.yaml").write_text("schema: 1\ndefault: AUTO_ALLOW\n", encoding="utf-8")
    gate = ApprovalGate(mode="auto")
    gate.permissions = Permissions(repo)
    reg = build_tools(ToolContext(repo, PathGuard(repo), gate))
    out, is_err = reg.call("write_file", {"path": "voice.yaml", "content": "schema: 1\nenabled: true\n"})
    assert is_err
    assert load_voice_config(repo).enabled is False


def test_voice_yaml_rejects_inline_secrets_and_disabled_pipeline_refuses(repo: Path):
    with pytest.raises(VoiceError, match="Inline secrets are forbidden"):
        parse_voice_config({"schema": 1, "tts": {"api_key": "sk-12345678901234567890"}})
    with pytest.raises(VoiceError, match="Unknown voice.yaml"):
        parse_voice_config({"schema": 1, "unknown_field": True})

    pipeline = VoicePipeline(VoiceConfig(enabled=False), workspace=repo)
    with pytest.raises(VoiceDisabledError, match="disabled"):
        pipeline.process_frames([b"\x00\x00" * 512], handler=lambda _t: "ok")


# ── 2. Audio I/O ────────────────────────────────────────────────────────── #


def test_audio_io_devices_stream_and_playback_with_fake_sounddevice(tmp_path: Path):
    tone = float32_to_pcm16([0.4 if i % 2 == 0 else -0.4 for i in range(512)])
    fake_sd = _FakeSoundDevice(input_frames=[tone, tone, tone])

    devices = list_devices(sd_module=fake_sd)
    assert len(devices) == 2
    assert resolve_device("Fake USB Mic", kind="input", sd_module=fake_sd) == 0
    assert resolve_device(1, kind="output", sd_module=fake_sd) == 1

    wav_out = tmp_path / "mic.wav"
    report = test_microphone(duration_seconds=0.05, device=0, save_path=wav_out, sd_module=fake_sd)
    assert report["ok"] and report["signal_detected"] and wav_out.is_file()
    pcm, rate, channels = read_wav_pcm(wav_out)
    assert rate == 16_000 and channels == 1 and len(pcm) > 0

    with AudioInputStream(device=0, sd_module=fake_sd) as stream:
        first = next(stream.frames())
        assert len(first) == 1024

    player = AudioOutputPlayer(device=1, sd_module=fake_sd)
    clip = AudioClip(make_sine_wav_bytes(tone), "wav")
    assert player.play(clip) is False
    assert fake_sd.played_bytes

    stop = threading.Event()
    stop.set()
    assert player.play(clip, interrupt=stop) is True


def test_missing_sounddevice_raises_clear_dependency_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sounddevice":
            raise ImportError("no sounddevice")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(VoiceDependencyError, match="pip install"):
        list_devices()


# ── 3. Wake word & push-to-talk ─────────────────────────────────────────── #


def test_openwakeword_sensitivity_and_cooldown():
    clock_now = [10.0]

    class _FakeOWWModel:
        def __init__(self, score: float):
            self.score = score

        def predict(self, _pcm):
            return {"hey_nexus": self.score}

    low_sens = OpenWakeWordDetector(
        word="hey nexus",
        sensitivity=0.2,  # threshold = 0.8
        cooldown_seconds=2.0,
        model=_FakeOWWModel(0.5),
        clock=lambda: clock_now[0],
    )
    frame = b"\x01\x00" * 1280
    assert low_sens.process_frame(frame) is None

    high_sens = OpenWakeWordDetector(
        word="hey nexus",
        sensitivity=0.8,  # threshold = 0.2
        cooldown_seconds=2.0,
        model=_FakeOWWModel(0.5),
        clock=lambda: clock_now[0],
    )
    evt1 = high_sens.process_frame(frame)
    assert evt1 is not None and evt1.word == "hey_nexus"
    # Cooldown suppresses immediate re-trigger
    clock_now[0] = 10.5
    assert high_sens.process_frame(frame) is None
    # Triggers again after cooldown elapses
    clock_now[0] = 12.5
    assert high_sens.process_frame(frame) is not None


def test_push_to_talk_detector_trigger_and_cooldown():
    now = [0.0]
    ptt = PushToTalkDetector(hotkey="ctrl+space", cooldown_seconds=1.0, clock=lambda: now[0])
    assert ptt.process_frame(b"\x00" * 1024) is None
    assert ptt.trigger() is True
    evt = ptt.process_frame(b"\x00" * 1024)
    assert evt is not None and evt.engine == "ptt"
    now[0] = 0.3
    assert ptt.trigger() is False
    now[0] = 1.5
    assert ptt.trigger() is True


# ── 4. Silero VAD ───────────────────────────────────────────────────────── #


def test_silero_vad_chunking_speech_start_end_and_max_duration():
    probs = [0.1, 0.1, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1]
    idx = [0]

    def fake_silero(samples, sr):
        assert sr == 16_000 and len(samples) == 512
        val = probs[min(idx[0], len(probs) - 1)]
        idx[0] += 1
        return val

    vad = SileroVAD(model=fake_silero)
    seg = VADSegmenter(
        vad,
        threshold=0.5,
        min_speech_ms=64,    # 2 chunks
        min_silence_ms=96,   # 3 chunks
        speech_pad_ms=32,    # 1 chunk preroll
    )
    chunk = b"\x10\x00" * 512
    events = []
    for _ in probs:
        events.extend(seg.feed(chunk))
    kinds = [e.kind for e in events]
    assert kinds[0] == "speech_start"
    assert kinds[-1] == "speech_end"
    # Pre-roll + 2 speech chunks + 1 speech + 3 trailing silence = 7 chunks worth
    assert len(events[-1].pcm) == 7 * 1024

    # Max utterance cap stops runaway speech
    always_speech = SileroVAD(model=lambda _s, _sr: 0.95)
    capped = VADSegmenter(always_speech, min_speech_ms=32, min_silence_ms=5000, max_utterance_seconds=0.5)
    frames = [chunk] * 30
    collected = capped.collect_utterance(frames)
    assert collected is not None and len(collected) <= 17 * 1024


def test_energy_vad_distinguishes_tone_from_silence(tmp_path: Path):
    tone_wav = make_sine_wav(tmp_path / "tone.wav", duration_seconds=0.2, amplitude=0.5)
    pcm_tone, _, _ = read_wav_pcm(tone_wav)
    evad = EnergyVAD(rms_threshold=300.0)
    assert evad.speech_probability(pcm_tone[:1024]) > 0.8
    assert evad.speech_probability(b"\x00" * 1024) == 0.0


# ── 5. STT (Bangla, English, mixed, streaming partials, cloud mock) ─────── #


def test_faster_whisper_stt_auto_detects_bangla_english_mixed_and_streams_partials(tmp_path: Path):
    wav = make_sine_wav(tmp_path / "speech.wav", duration_seconds=0.2)

    class _FakeWhisper:
        def __init__(self, text: str, lang: str):
            self.text = text
            self.lang = lang
            self.seen_language_arg = "unset"

        def transcribe(self, _path: str, *, language=None, **_kwargs):
            self.seen_language_arg = language
            segs = [
                SimpleNamespace(text=part, start=0.0, end=0.5)
                for part in self.text.split("|")
            ]
            info = SimpleNamespace(language=self.lang, language_probability=0.94, duration=1.2)
            return segs, info

    partials: list[str] = []
    mixed_model = _FakeWhisper("আমার repo এর|status দেখাও", "bn")
    stt = FasterWhisperSTT(model_size="small", language="auto", model=mixed_model)
    res = stt.transcribe(wav, on_partial=partials.append)
    assert mixed_model.seen_language_arg is None
    assert res.mixed is True and res.languages == ("bn", "en")
    assert res.text == "আমার repo এর status দেখাও"
    assert partials == ["আমার repo এর", "আমার repo এর status দেখাও"]

    # Stream frames incremental partials
    stream_partials: list[str] = []
    pcm, _, _ = read_wav_pcm(wav)
    # Pad to trigger incremental partial emission
    chunk = pcm + (b"\x01\x00" * 16000)
    res_stream = stt.transcribe_stream([chunk[:20000], chunk[20000:]], on_partial=stream_partials.append, partial_interval_bytes=16000)
    assert res_stream.text == "আমার repo এর status দেখাও"
    assert stream_partials


def test_cloud_stt_with_mock_http_and_env_key(tmp_path: Path, monkeypatch):
    wav = make_sine_wav(tmp_path / "cloud.wav", duration_seconds=0.1)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-cloud-secret-1234567890")
    calls = []

    class _FakeHTTP:
        def post(self, url, headers=None, data=None, files=None, timeout=None):
            calls.append((url, headers, data))
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"text": "list repository files", "language": "en"},
            )

    stt = CloudSTT(http_client=_FakeHTTP())
    res = stt.transcribe(wav)
    assert res.text == "list repository files"
    assert res.language == "en"
    assert calls and calls[0][1]["Authorization"].startswith("Bearer sk-test")


# ── 6. TTS, secret redaction, language voice, rate, & playback queue ────── #


def test_tts_redacts_secrets_before_synthesis(repo: Path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSySecretValueForTesting1234567890")
    raw = (
        "Your tokens are ghp_abcdefghijklmnopqrstuvwxyz1234567890, "
        "sk-proj-1234567890abcdefghijklmnop, and AIzaSySecretValueForTesting1234567890."
    )
    cleaned = sanitize_tts_text(raw, workspace=repo)
    assert "ghp_" not in cleaned
    assert "sk-proj-" not in cleaned
    assert "AIzaSySecretValueForTesting" not in cleaned
    assert "redacted secret" in cleaned

    tts = _FakeTTS(workspace=repo)
    tts.synthesize(raw)
    spoken_text, _lang, _rate = tts.spoken[0]
    assert "ghp_" not in spoken_text and "AIzaSySecretValue" not in spoken_text


def test_tts_providers_language_selection_rate_and_playback_queue(tmp_path: Path, monkeypatch):
    # Sentence splitting supports Bangla danda (।) and English punctuation
    sentences = list(iter_sentences(["প্রথম বাক্য। ", "Second sentence! ", "Third?"]))
    assert sentences == ["প্রথম বাক্য।", "Second sentence!", "Third?"]

    # EdgeTTS chooses bn-BD voice for Bangla and en-US for English + maps rate
    edge_calls = []

    def fake_communicate(text, voice, rate="+0%"):
        edge_calls.append((text, voice, rate))

        class _Comm:
            async def save(self, dest):
                Path(dest).write_bytes(b"ID3fake")

        return _Comm()

    edge = EdgeTTS(speaking_rate=1.25, communicate_factory=fake_communicate)
    clip_bn = edge.synthesize("হ্যালো নেক্সাস")
    clip_en = edge.synthesize("Hello Nexus")
    assert clip_bn.format == "mp3" and clip_en.format == "mp3"
    assert edge_calls[0][1] == "bn-BD-NabanitaNeural" and edge_calls[0][2] == "+25%"
    assert edge_calls[1][1] == "en-US-AriaNeural" and edge_calls[1][2] == "+25%"

    # PiperTTS passes length_scale = 1/rate
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"onnx")
    piper_calls = []

    def fake_piper(argv, input=b""):
        piper_calls.append((argv, input))
        return make_sine_wav_bytes(b"\x00\x00" * 160)

    piper = PiperTTS(model, speaking_rate=2.0, runner=fake_piper)
    piper_clip = piper.synthesize("Test rate")
    assert piper_clip.format == "wav"
    assert "--length_scale" in piper_calls[0][0]
    assert "0.5" in piper_calls[0][0]

    # KokoroTTS offline fake pipeline
    class _FakeKokoro:
        def __call__(self, _text, voice=None, speed=1.0):
            assert voice == "af_heart" and speed == 1.5
            yield ("g", "p", [0.0, 0.2, -0.2, 0.0])

    kokoro = KokoroTTS(speaking_rate=1.5, pipeline=_FakeKokoro())
    assert kokoro.synthesize("Hello").format == "wav"

    # ElevenLabsTTS fake HTTP
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test-key-1234567890")
    el = ElevenLabsTTS(
        voice_id="voice123",
        http_client=SimpleNamespace(
            post=lambda *_a, **_k: SimpleNamespace(raise_for_status=lambda: None, content=b"ID3eleven")
        ),
    )
    assert el.synthesize("Hello cloud").format == "mp3"

    # AutoTTS routes Bangla vs English
    auto = AutoTTS(bn_provider=edge, en_provider=piper)
    assert auto.select_voice("bn") == "bn-BD-NabanitaNeural"
    assert auto.select_voice("en") == "voice.onnx"

    # PlaybackQueue plays sentences and supports barge-in cancellation
    fake_tts = _FakeTTS()
    fake_sd = _FakeSoundDevice()
    q = PlaybackQueue(fake_tts, AudioOutputPlayer(sd_module=fake_sd))
    q.enqueue_stream(["One. ", "Two. ", "ghp_secret12345678901234567890."])
    assert q.depth == 3
    spoken = q.drain_sync()
    assert len(spoken) == 3
    assert "ghp_secret" not in spoken[2]

    q.enqueue("First.")
    q.enqueue("Second.")
    q.barge_in()
    assert q.depth == 0


# ── 7. Speaker verification & DANGEROUS action gating ───────────────────── #


def test_speaker_verifier_and_dangerous_gate_enforces_enrolled_voice(repo: Path, tmp_path: Path):
    owner_wav = make_sine_wav(tmp_path / "owner.wav", frequency=440.0, duration_seconds=0.1)
    stranger_wav = make_sine_wav(tmp_path / "stranger.wav", frequency=880.0, duration_seconds=0.1)

    def fake_encoder(path: Path):
        if "owner" in path.name or path.read_bytes() == owner_wav.read_bytes():
            return [1.0, 0.0] * 16
        return [0.0, 1.0] * 16

    verifier = SpeakerVerifier(repo, threshold=0.8, encoder=fake_encoder)
    # Fail closed when unenrolled
    assert verifier.verify(owner_wav).matched is False

    # Enroll owner
    enrolled = verifier.enroll([owner_wav])
    assert enrolled["enrolled"] is True
    assert oct(stat.S_IMODE(os.stat(verifier.path).st_mode)) == "0o600"
    assert verifier.verify(owner_wav).matched is True
    assert verifier.verify(stranger_wav).matched is False

    # Wire into ApprovalGate: stranger rejected on DANGEROUS even under AUTO_ALLOW + --yes!
    (repo / "permissions.yaml").write_text("schema: 1\ndefault: AUTO_ALLOW\n", encoding="utf-8")
    current_match = [verifier.verify(stranger_wav)]
    gate = ApprovalGate(mode="auto", allow_dangerous=True, callback=lambda *a: ApprovalDecision(True, scope="once"))
    gate.permissions = Permissions(repo)
    gate.voice_speaker_check = make_speaker_gate_check(verifier, lambda: current_match[0])
    reg = build_tools(ToolContext(repo, PathGuard(repo), gate))

    # Read-only still allowed for non-dangerous query
    out_read, err_read = reg.call("read_file", {"path": "README.md"})
    assert not err_read and "demo project" in out_read

    # DANGEROUS action rejected for stranger
    (repo / "target.txt").write_text("keep", encoding="utf-8")
    out_del, err_del = reg.call("delete_file", {"path": "target.txt"})
    assert err_del and "rejected" in out_del.lower() and (repo / "target.txt").exists()

    # Enrolled owner passes speaker gate and hits explicit fresh approval callback
    current_match[0] = verifier.verify(owner_wav)
    out_del_ok, err_del_ok = reg.call("delete_file", {"path": "target.txt"})
    assert not err_del_ok and not (repo / "target.txt").exists()

    # Revoke restores fail-closed protection
    assert verifier.revoke() is True
    assert verifier.verify(owner_wav).matched is False


# ── 8. Full pipeline (WAV -> wake -> VAD -> STT -> handler -> TTS + HUD) ── #


def test_full_pipeline_wav_produces_tts_and_publishes_hud_events(repo: Path, tmp_path: Path):
    # Create a WAV with 80ms wake chunk + 250ms speech + 400ms trailing silence
    wake_samples = [0.2 if i % 2 == 0 else -0.2 for i in range(1280)]
    speech_samples = [0.5 if i % 2 == 0 else -0.5 for i in range(4096)]
    silence_samples = [0.0] * 6400
    wav_path = tmp_path / "command.wav"
    write_wav(wav_path, float32_to_pcm16(wake_samples + speech_samples + silence_samples))

    # Wake model triggers on frame 0
    class _OneShotWakeModel:
        def __init__(self):
            self.calls = 0

        def predict(self, _pcm):
            self.calls += 1
            return {"hey_nexus": 0.95 if self.calls == 1 else 0.0}

    wake = OpenWakeWordDetector(word="hey nexus", sensitivity=0.6, model=_OneShotWakeModel())
    vad = EnergyVAD(rms_threshold=200.0)
    stt = FasterWhisperSTT(
        model_size="small",
        model=SimpleNamespace(
            transcribe=lambda _p, **_k: (
                [SimpleNamespace(text="check git status", start=0.0, end=0.5)],
                SimpleNamespace(language="en", language_probability=0.98, duration=0.5),
            )
        ),
    )
    tts = _FakeTTS(workspace=repo)
    fake_sd = _FakeSoundDevice()
    player = AudioOutputPlayer(sd_module=fake_sd)

    hud_events: list[Event] = []
    state_events = []
    cfg = VoiceConfig(
        enabled=True,
        wake=WakeConfig(enabled=True, engine="openwakeword", word="hey nexus"),
        vad=VADConfig(engine="energy", threshold=0.3, min_speech_ms=64, min_silence_ms=200, speech_pad_ms=32),
        tts=TTSConfig(provider="piper", speaking_rate=1.1),
    )
    pipeline = VoicePipeline(
        cfg,
        workspace=repo,
        wake_detector=wake,
        vad=vad,
        stt=stt,
        tts=tts,
        player=player,
        on_state_change=state_events.append,
        emit=hud_events.append,
    )

    result = pipeline.process_wav(
        wav_path,
        handler=lambda tr: [
            f"Heard: {tr.text}. ",
            "Secret token ghp_secret12345678901234567890 hidden.",
        ],
    )
    assert result.error == ""
    assert result.transcript is not None and result.transcript.text == "check git status"
    assert result.states == [
        VoiceState.IDLE.value,
        VoiceState.LISTENING.value,
        VoiceState.THINKING.value,
        VoiceState.SPEAKING.value,
        VoiceState.IDLE.value,
    ]
    assert len(tts.spoken) == 2
    assert tts.spoken[0][0] == "Heard: check git status."
    assert "ghp_secret" not in tts.spoken[1][0]
    assert "redacted secret" in tts.spoken[1][0]
    assert fake_sd.played_bytes

    # HUD EventType.VOICE_STATE + persisted HUD snapshot state both reflect pipeline activity
    assert [e.type for e in hud_events] == [EventType.VOICE_STATE] * 5
    assert [e.data["state"] for e in hud_events] == result.states
    studio = ServerState(repo, quiet=True).workbench.studio
    hud_snap = HUD(studio).snapshot()
    assert hud_snap["voice"]["state"] == "idle"
    assert read_hud_state(repo)["state"] == "idle"


def test_voice_agent_integration_blocks_dangerous_tool_for_unverified_voice(repo: Path, tmp_path: Path):
    (repo / "voice.yaml").write_text(
        yaml.safe_dump({"schema": 1, "enabled": True, "speaker": {"enabled": True, "require_for_dangerous": True}}),
        encoding="utf-8",
    )
    (repo / "permissions.yaml").write_text("schema: 1\ndefault: AUTO_ALLOW\n", encoding="utf-8")
    (repo / "secret.txt").write_text("do not delete", encoding="utf-8")

    owner_wav = make_sine_wav(tmp_path / "owner.wav", frequency=440.0, duration_seconds=0.1)
    stranger_wav = make_sine_wav(tmp_path / "stranger.wav", frequency=880.0, duration_seconds=0.1)
    verifier = SpeakerVerifier(
        repo,
        threshold=0.8,
        encoder=lambda p: [1.0, 0.0] * 16 if p.read_bytes() == owner_wav.read_bytes() else [0.0, 1.0] * 16,
    )
    verifier.enroll([owner_wav])

    provider = FakeProvider(
        "gemini",
        [
            tool_completion([("delete_file", {"path": "secret.txt"})]),
            text_completion("blocked by speaker gate"),
        ],
    )
    cfg = load_voice_config(repo)
    pipeline = VoicePipeline(cfg, workspace=repo, speaker_verifier=verifier)
    pipeline.last_speaker_match = verifier.verify(stranger_wav)

    from nexus.agent import NexusAgent
    from nexus.config import load_config

    nexus_cfg = load_config(repo)
    nexus_cfg.default_agent = "gemini"
    approvals_called: list[str] = []
    agent = NexusAgent(
        nexus_cfg,
        workspace=repo,
        providers=fake_registry(provider, config=nexus_cfg),
        approval_callback=lambda spec, *_a: approvals_called.append(spec.name) or ApprovalDecision(True),
        auto_yes=True,
    )
    agent.gate.voice_speaker_check = pipeline.make_approval_gate_check()
    agent.run("delete secret.txt")
    # Stranger never reaches approval callback and file remains intact
    assert (repo / "secret.txt").exists()
    assert approvals_called == []
    from nexus import audit as auditlog

    events = auditlog.tail(repo, limit=10)
    assert any(
        ev.get("what") == "permission.decision" and ev.get("target") == "delete_file" and ev.get("result") == "denied"
        for ev in events
    )

    # Enrolled owner passes speaker check and reaches explicit approval callback
    pipeline.last_speaker_match = verifier.verify(owner_wav)
    provider2 = FakeProvider(
        "gemini",
        [
            tool_completion([("delete_file", {"path": "secret.txt"})]),
            text_completion("deleted after owner voice + fresh approval"),
        ],
    )
    agent2 = NexusAgent(
        nexus_cfg,
        workspace=repo,
        providers=fake_registry(provider2, config=nexus_cfg),
        approval_callback=lambda spec, *_a: approvals_called.append(spec.name) or ApprovalDecision(True),
        auto_yes=True,
    )
    agent2.gate.voice_speaker_check = pipeline.make_approval_gate_check()
    agent2.run("delete secret.txt")
    assert not (repo / "secret.txt").exists()
    assert approvals_called == ["delete_file"]

    handler = build_agent_handler(repo, pipeline=pipeline, read_only=True)
    assert callable(handler)


def test_missing_optional_voice_dependencies_fail_with_clear_error(tmp_path: Path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def deny(targets):
        def _fake(name, *args, **kwargs):
            if name in targets or any(name.startswith(t + ".") for t in targets):
                raise ImportError(f"missing {name}")
            return real_import(name, *args, **kwargs)

        return _fake

    monkeypatch.setattr(builtins, "__import__", deny({"openwakeword"}))
    with pytest.raises(VoiceDependencyError, match="voice-wake"):
        OpenWakeWordDetector().process_frame(b"\x00\x00" * 1280)

    monkeypatch.setattr(builtins, "__import__", deny({"silero_vad"}))
    with pytest.raises(VoiceDependencyError, match="voice"):
        SileroVAD().speech_probability(b"\x00\x00" * 512)

    monkeypatch.setattr(builtins, "__import__", deny({"faster_whisper"}))
    wav = make_sine_wav(tmp_path / "dep.wav", duration_seconds=0.1)
    with pytest.raises(VoiceDependencyError, match="voice"):
        FasterWhisperSTT("small").transcribe(wav)

    monkeypatch.setattr(builtins, "__import__", deny({"resemblyzer"}))
    with pytest.raises(VoiceDependencyError, match="voice-speaker"):
        SpeakerVerifier(tmp_path).enroll([wav])


def test_full_offline_local_providers_never_touch_network(repo: Path, tmp_path: Path, monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network must not be called in offline mode")),
    )
    model_file = tmp_path / "local.onnx"
    model_file.write_bytes(b"onnx")
    piper = PiperTTS(
        model_file,
        workspace=repo,
        runner=lambda *_a, **_k: make_sine_wav_bytes(float32_to_pcm16([0.1] * 320)),
    )
    stt = FasterWhisperSTT(
        model_size="tiny",
        model=SimpleNamespace(
            transcribe=lambda _p, **_k: (
                [SimpleNamespace(text="offline test", start=0.0, end=0.2)],
                SimpleNamespace(language="en", language_probability=0.99, duration=0.2),
            )
        ),
    )
    vad = SileroVAD(model=lambda _s, _sr: 0.9)
    wake = OpenWakeWordDetector(
        model=SimpleNamespace(predict=lambda _pcm: {"hey_nexus": 0.99}),
    )
    cmd_wav = make_sine_wav(tmp_path / "offline.wav", duration_seconds=0.3, amplitude=0.4)
    cfg = VoiceConfig(
        enabled=True,
        wake=WakeConfig(enabled=True, engine="openwakeword"),
        vad=VADConfig(engine="silero", min_speech_ms=32, min_silence_ms=100, max_utterance_seconds=0.2),
    )
    fake_sd = _FakeSoundDevice()
    pipeline = VoicePipeline(
        cfg,
        workspace=repo,
        wake_detector=wake,
        vad=vad,
        stt=stt,
        tts=piper,
        player=AudioOutputPlayer(sd_module=fake_sd),
    )
    res = pipeline.process_wav(cmd_wav, handler=lambda tr: f"Offline reply for {tr.text}.")
    assert res.error == "" and res.spoken_sentences == ["Offline reply for offline test."]
    assert fake_sd.played_bytes


# ── 9. CLI: nexus voice run | test-mic | test-tts | enroll | devices ───── #


def test_cli_voice_commands_run_offline_with_fakes(repo: Path, tmp_path: Path, monkeypatch, capsys):
    fake_sd = _FakeSoundDevice(input_frames=[float32_to_pcm16([0.3] * 512)] * 5)
    monkeypatch.setattr("nexus.voice.audio_io._import_sounddevice", lambda sd=None: fake_sd)

    # 1. devices
    assert main(["voice", "-C", str(repo), "devices", "--json"]) == 0
    devs = json.loads(capsys.readouterr().out)
    assert devs[0]["name"] == "Fake USB Mic"

    # 2. test-mic
    assert main(["voice", "-C", str(repo), "test-mic", "--seconds", "0.1", "--json"]) == 0
    mic = json.loads(capsys.readouterr().out)
    assert mic["ok"] is True and mic["signal_detected"] is True

    # 3. test-tts (secret redacted)
    fake_tts = _FakeTTS(workspace=repo)
    monkeypatch.setattr("nexus.voice.cli.create_tts", lambda *_a, **_k: fake_tts)
    out_wav = tmp_path / "spoken.wav"
    assert main(["voice", "-C", str(repo), "test-tts", "Token is ghp_abcdef12345678901234567890", "-o", str(out_wav), "--json"]) == 0
    tts_res = json.loads(capsys.readouterr().out)
    assert tts_res["ok"] is True and tts_res["redacted"] is True and out_wav.is_file()
    assert "ghp_abcdef" not in fake_tts.spoken[0][0]

    # 4. enroll & revoke
    sample_wav = make_sine_wav(tmp_path / "sample.wav", duration_seconds=0.1)
    monkeypatch.setattr(
        "nexus.voice.speaker.SpeakerVerifier._embed",
        lambda _self, _audio: [1.0, 0.0] * 16,
    )
    assert main(["voice", "-C", str(repo), "enroll", str(sample_wav), "--confirm", "ENROLL MY VOICE", "--json"]) == 0
    enroll_res = json.loads(capsys.readouterr().out)
    assert enroll_res["enrolled"] is True

    # 5. run refuses when voice.yaml enabled=false
    (repo / "voice.yaml").write_text("schema: 1\nenabled: false\n", encoding="utf-8")
    assert main(["voice", "-C", str(repo), "run", "--json"]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["ok"] is False and "disabled" in refused["error"]

    # 6. run executes WAV command when voice.yaml enabled=true
    (repo / "voice.yaml").write_text(
        yaml.safe_dump({
            "schema": 1,
            "enabled": True,
            "wake": {"enabled": False, "engine": "disabled"},
            "vad": {"engine": "energy", "threshold": 0.3, "min_speech_ms": 64, "min_silence_ms": 150},
        }),
        encoding="utf-8",
    )
    cmd_wav = make_sine_wav(tmp_path / "cmd.wav", duration_seconds=0.25, amplitude=0.5)
    monkeypatch.setattr(
        "nexus.voice.pipeline.create_stt",
        lambda _cfg: SimpleNamespace(
            transcribe_stream=lambda *_a, **_k: TranscriptResult(text="hello nexus", language="en")
        ),
    )
    monkeypatch.setattr("nexus.voice.pipeline.create_tts", lambda *_a, **_k: _FakeTTS())
    monkeypatch.setattr(
        "nexus.voice.cli.build_agent_handler",
        lambda *_a, **_k: (lambda tr: f"Done with {tr.text}."),
    )
    assert main(["voice", "-C", str(repo), "run", "--wav", str(cmd_wav), "--skip-wake", "--json"]) == 0
    run_res = json.loads(capsys.readouterr().out)
    assert run_res["ok"] is True
    assert run_res["transcript"] == "hello nexus"
    assert run_res["spoken_sentences"] == ["Done with hello nexus."]
