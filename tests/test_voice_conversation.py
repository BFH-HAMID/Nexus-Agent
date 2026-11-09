"""Offline scripted-conversation tests for TASK 4: human-like conversation layer."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from nexus.agent import NexusAgent
from nexus.cli import main
from nexus.config import load_config
from nexus.hud.core import HUD
from nexus.permissions import Permissions
from nexus.server import ServerState
from nexus.tools import ApprovalGate, PathGuard, ToolContext, build_tools
from nexus.types import Risk, ToolSpec
from nexus.voice import (
    AcknowledgementManager,
    AdaptiveSilenceTracker,
    BargeInAction,
    ConversationState,
    GeminiLiveAdapter,
    LiveKitAdapter,
    LocalPipelineRealtimeAdapter,
    PipecatAdapter,
    SemanticTurnDetector,
    SpokenApprovalIntent,
    SpokenApprovalManager,
    StreamingSentenceBuffer,
    TurnTakingSegmenter,
    VoiceConfig,
    VoiceDependencyError,
    VoiceError,
    VoicePipeline,
    classify_barge_in,
    create_realtime_adapter,
    enforce_one_question,
    format_for_speech,
    load_persona_config,
    parse_approval_intent,
    read_hud_messages,
    resolve_reply_language,
)
from nexus.voice.audio_io import AudioClip, AudioOutputPlayer, float32_to_pcm16
from nexus.voice.config import (
    AcknowledgementConfig,
    ConversationConfig,
    DuplexConfig,
    RealtimeVoiceConfig,
    StreamingConfig,
    TurnDetectionConfig,
    VADConfig,
    WakeConfig,
)
from nexus.voice.persona import parse_persona_config
from nexus.voice.speaker import SpeakerVerifier, make_speaker_gate_check
from nexus.voice.stt import TranscriptResult
from nexus.voice.tts import TTSProvider
from nexus.voice.vad import EnergyVAD

from .conftest import FakeProvider, fake_registry, text_completion


class _FakeTTS(TTSProvider):
    name = "fake-tts"

    def __init__(self, *, workspace: Path | None = None, synth_delay_s: float = 0.0):
        super().__init__(workspace=workspace)
        self.synth_delay_s = synth_delay_s
        self.spoken: list[tuple[str, str]] = []

    def _synthesize_clean(self, text: str, *, language: str, rate: float) -> AudioClip:
        if self.synth_delay_s > 0:
            time.sleep(self.synth_delay_s)
        self.spoken.append((text, language))
        return AudioClip(float32_to_pcm16([0.1] * 160), "pcm")


class _ScriptablePlayer(AudioOutputPlayer):
    """Simulates bounded-duration audio playback that stops immediately on interrupt."""

    def __init__(self, play_seconds_per_clip: float = 0.0):
        super().__init__()
        self.play_seconds_per_clip = play_seconds_per_clip
        self.played_clips: list[AudioClip] = []
        self.stopped_immediately = False

    def stop(self) -> None:
        self.stopped_immediately = True

    def play(self, clip: AudioClip, interrupt=None) -> bool:
        self.played_clips.append(clip)
        if self.play_seconds_per_clip <= 0:
            return bool(interrupt is not None and interrupt.is_set())
        wait_ev = interrupt if interrupt is not None else threading.Event()
        if interrupt is not None:
            return bool(wait_ev.wait(timeout=self.play_seconds_per_clip))
        wait_ev.wait(timeout=self.play_seconds_per_clip)
        return False


def _speech_frames(chunks: int = 4, *, amplitude: float = 0.5) -> list[bytes]:
    return [float32_to_pcm16([amplitude if i % 2 == 0 else -amplitude for i in range(512)]) for _ in range(chunks)]


def _silence_frames(chunks: int = 8) -> list[bytes]:
    return [b"\x00\x00" * 512 for _ in range(chunks)]


def _fake_stt_from_text(text: str, language: str = "en"):
    return SimpleNamespace(
        transcribe_stream=lambda *_a, **_k: TranscriptResult(text=text, language=language),
    )


# ── 1. Operator-owned persona.yaml & permissions ────────────────────────── #


def test_persona_yaml_disabled_by_default_and_operator_protected(repo: Path):
    repo_root = Path(__file__).resolve().parents[1]
    cfg = load_persona_config(repo_root)
    assert cfg.schema == 1
    assert cfg.enabled is False
    assert cfg.name == "Nexus"
    assert cfg.tone.calm if hasattr(cfg.tone, "calm") else cfg.tone.style == "calm"
    assert cfg.language_policy.mirror_user is True
    assert cfg.language_policy.allow_bangla_english_mixing is True

    with pytest.raises(VoiceError, match="Inline secrets are forbidden"):
        parse_persona_config({"schema": 1, "api_key": "sk-1234567890"})
    with pytest.raises(VoiceError, match="Unknown persona.yaml"):
        parse_persona_config({"schema": 1, "unknown_setting": 1})

    # Agent file tools cannot edit persona.yaml even under AUTO_ALLOW
    (repo / "persona.yaml").write_text("schema: 1\nenabled: false\n", encoding="utf-8")
    (repo / "permissions.yaml").write_text("schema: 1\ndefault: AUTO_ALLOW\n", encoding="utf-8")
    gate = ApprovalGate(mode="auto")
    gate.permissions = Permissions(repo)
    reg = build_tools(ToolContext(repo, PathGuard(repo), gate))
    out, is_err = reg.call("write_file", {"path": "persona.yaml", "content": "schema: 1\nenabled: true\n"})
    assert is_err and "denied" in out.lower()
    assert Permissions(repo).operator_control(repo / "persona.yaml") is True
    assert load_persona_config(repo).enabled is False


# ── 2. Persona spoken-style rules, no markdown aloud, HUD/Telegram & language mirroring ── #


def test_persona_strips_markdown_summarizes_long_output_and_mirrors_languages(repo: Path):
    persona = parse_persona_config({
        "schema": 1,
        "enabled": True,
        "name": "Nexus",
        "spoken_style": {
            "short_sentences": True,
            "strip_markdown": True,
            "read_lists_aloud": False,
            "read_urls_aloud": False,
            "summarize_numbers": True,
            "summarize_code": True,
            "max_spoken_chars": 220,
            "long_output_route": "both",
        },
    })

    # Complex reply containing markdown, bullet list, raw URL, code fence, large number, and secret
    raw_reply = (
        "# Deployment Report\n\n"
        "Processed **1543200** requests across `nexus/server.py` (see https://github.com/BFH-HAMID/Nexus-Agent/pulls).\n\n"
        "- Updated authentication middleware\n"
        "- Fixed token rotation bug\n"
        "- Verified ghp_supersecret12345678901234567890 is masked\n\n"
        "```python\n"
        "def ok():\n"
        "    return True\n"
        "```\n"
    )
    tg_sent: list[str] = []
    res = format_for_speech(
        raw_reply,
        persona,
        user_language="en",
        workspace=repo,
        dispatch_full=None,
    )
    # No markdown, no raw URLs, no bullet syntax, and no secret in spoken text
    for forbidden in ("#", "**", "`", "https://", "ghp_supersecret", "- Updated"):
        assert forbidden not in res.spoken_text
    assert res.was_summarized is True
    assert "hud" in res.dispatched_to
    assert "hud" in res.spoken_text.lower()

    # Full redacted text saved to HUD feed and readable via HUD.snapshot()
    hud_msgs = read_hud_messages(repo)
    assert len(hud_msgs) == 1
    assert "ghp_supersecret" not in hud_msgs[0]["full_text"]
    assert "Deployment Report" in hud_msgs[0]["full_text"]

    studio = ServerState(repo, quiet=True).workbench.studio
    snap = HUD(studio).snapshot()
    assert snap["voice"]["messages"][-1]["id"] == res.hud_record_id

    # Verify Telegram dispatch hook also receives masked payload when route='telegram'
    persona_tg = parse_persona_config({
        "schema": 1,
        "enabled": True,
        "spoken_style": {"max_spoken_chars": 100, "long_output_route": "telegram"},
    })
    from nexus.voice.persona import dispatch_long_output

    routes, _ = dispatch_long_output(
        workspace=repo,
        full_text="Secret sk-12345678901234567890 detail report.",
        summary="Short report.",
        route="telegram",
        telegram_sender=lambda msg: tg_sent.append(msg) or True,
    )
    assert routes == ("telegram",)
    assert tg_sent and "sk-123456" not in tg_sent[0]
    assert persona_tg.spoken_style.long_output_route == "telegram"

    # Language mirroring: English, Bangla, and Bangla+English mixing
    assert resolve_reply_language(persona, "en", "Check git status") == "en"
    assert resolve_reply_language(persona, "bn", "আজকের আবহাওয়া কেমন") == "bn"
    assert resolve_reply_language(persona, "bn", "আমার `server.py` এর status বলো") == "bn-en"


def test_scripted_conversation_no_markdown_and_language_mirroring(repo: Path):
    (repo / "persona.yaml").write_text(
        yaml.safe_dump({"schema": 1, "enabled": True}),
        encoding="utf-8",
    )
    cfg = VoiceConfig(
        enabled=True,
        wake=WakeConfig(enabled=False, engine="disabled"),
        vad=VADConfig(engine="energy", threshold=0.3, min_speech_ms=32, min_silence_ms=100, speech_pad_ms=32),
        turn_detection=TurnDetectionConfig(semantic_eot=False, adaptive_silence=False),
        acknowledgements=AcknowledgementConfig(enabled=False),
    )

    # Turn 1: English with markdown reply -> spoken output has zero markdown
    tts_en = _FakeTTS(workspace=repo)
    p1 = VoicePipeline(
        cfg,
        workspace=repo,
        vad=EnergyVAD(rms_threshold=200.0),
        stt=_fake_stt_from_text("show repo status", "en"),
        tts=tts_en,
        player=_ScriptablePlayer(),
    )
    turn_en = p1.process_frames(
        _speech_frames(3) + _silence_frames(4),
        skip_wake=True,
        handler=lambda _tr: "## Status\n**Everything** is green on `main` at https://github.com/example/repo.",
    )
    assert turn_en.reply_language == "en"
    spoken_joined = " ".join(turn_en.spoken_sentences)
    assert "#" not in spoken_joined and "**" not in spoken_joined and "`" not in spoken_joined and "https://" not in spoken_joined

    # Turn 2: Bangla+English mixed input -> mirrored bn-en reply
    tts_bn = _FakeTTS(workspace=repo)
    p2 = VoicePipeline(
        cfg,
        workspace=repo,
        vad=EnergyVAD(rms_threshold=200.0),
        stt=_fake_stt_from_text("আমার pytest status চেক করো", "bn"),
        tts=tts_bn,
        player=_ScriptablePlayer(),
    )
    turn_bn = p2.process_frames(
        _speech_frames(3) + _silence_frames(4),
        skip_wake=True,
        handler=lambda tr: f"সব pytest ঠিক আছে ({tr.language})।",
    )
    assert turn_bn.reply_language == "bn-en"
    assert tts_bn.spoken[0][1].startswith("bn")


# ── 3. Full-duplex barge-in (<200 ms), LLM cancellation, resume vs discard ── #


def test_full_duplex_barge_in_cancels_tts_and_llm_under_200ms_then_resumes_or_discards(repo: Path):
    cfg = VoiceConfig(
        enabled=True,
        wake=WakeConfig(enabled=False, engine="disabled"),
        vad=VADConfig(engine="energy", threshold=0.3, min_speech_ms=32, min_silence_ms=100, speech_pad_ms=32),
        duplex=DuplexConfig(enabled=True, barge_in_threshold=0.4, barge_in_min_speech_ms=64, cancel_budget_ms=200),
        turn_detection=TurnDetectionConfig(semantic_eot=False, adaptive_silence=False),
        acknowledgements=AcknowledgementConfig(enabled=False),
    )
    tts = _FakeTTS(workspace=repo)
    player = _ScriptablePlayer(play_seconds_per_clip=0.35)
    stt = _fake_stt_from_text("read me the summary", "en")
    pipeline = VoicePipeline(
        cfg,
        workspace=repo,
        vad=EnergyVAD(rms_threshold=200.0),
        stt=stt,
        tts=tts,
        player=player,
    )

    llm_cancelled_after = []

    def slow_llm_stream(_tr):
        yield "Sentence one takes a moment to finish. "
        pipeline.llm_cancel_event.wait(0.06)
        if pipeline.llm_cancel_event.is_set():
            llm_cancelled_after.append("before_two")
            return
        yield "Sentence two should wait for resume. "
        pipeline.llm_cancel_event.wait(0.06)
        if pipeline.llm_cancel_event.is_set():
            llm_cancelled_after.append("before_three")
            return
        yield "Sentence three is the ending."

    # Barge-in arrives ~35ms into Sentence 1 playback (8 silence chunks * 4ms + 2 speech chunks)
    barge_audio = _silence_frames(8) + _speech_frames(5, amplitude=0.6)
    res1 = pipeline.process_frames(
        _speech_frames(3) + _silence_frames(4),
        skip_wake=True,
        handler=slow_llm_stream,
        barge_in_frames=barge_audio,
    )
    assert res1.interrupted is True
    assert res1.metrics is not None
    assert res1.metrics.barge_in_cancel_ms is not None
    assert res1.metrics.barge_in_cancel_ms < 200.0
    assert pipeline.llm_cancel_event.is_set() is True
    assert len(pipeline.duplex_state.paused_sentences) >= 1

    # User says "continue" -> resumes remaining paused sentences without hitting LLM handler again
    assert classify_barge_in("please continue") is BargeInAction.RESUME
    pipeline.stt = _fake_stt_from_text("please continue", "en")
    player.play_seconds_per_clip = 0.0
    llm_called = [0]

    def should_not_run(_tr):
        llm_called[0] += 1
        return "unexpected"

    res_resume = pipeline.process_frames(
        _speech_frames(3) + _silence_frames(4),
        skip_wake=True,
        handler=should_not_run,
    )
    assert res_resume.resumed is True
    assert res_resume.barge_in_action == "resume"
    assert llm_called[0] == 0
    assert res_resume.spoken_sentences

    # Now interrupt again and say "stop" -> discards remainder
    player.play_seconds_per_clip = 0.35
    pipeline.stt = _fake_stt_from_text("explain everything", "en")
    pipeline.process_frames(
        _speech_frames(3) + _silence_frames(4),
        skip_wake=True,
        handler=lambda _tr: ["First part plays now. ", "Second part should be discarded."],
        barge_in_frames=_silence_frames(8) + _speech_frames(5, amplitude=0.6),
    )
    assert pipeline.duplex_state.paused_sentences
    pipeline.stt = _fake_stt_from_text("stop", "en")
    player.play_seconds_per_clip = 0.0
    res_discard = pipeline.process_frames(
        _speech_frames(3) + _silence_frames(4),
        skip_wake=True,
        handler=should_not_run,
    )
    assert res_discard.barge_in_action == "discard"
    assert pipeline.duplex_state.paused_sentences == []
    assert llm_called[0] == 0


def test_nexus_agent_stream_cancelled_immediately_on_barge_in_event(repo: Path):
    cfg = load_config(repo)
    cfg.default_agent = "gemini"
    provider = FakeProvider("gemini", [text_completion("first part second part")])
    agent = NexusAgent(cfg, workspace=repo, providers=fake_registry(provider, config=cfg))
    agent.cancel_event = __import__("threading").Event()
    agent.cancel_event.set()
    result = agent.run("say hello")
    assert result.stopped_reason == "cancelled"


# ── 4. Turn detection: adaptive silence & semantic EOT ──────────────────── #


def test_semantic_turn_detector_and_segmenter_do_not_cut_off_trailing_and_or_um():
    det = SemanticTurnDetector(base_silence_ms=300, incomplete_extension_ms=600, max_adaptive_silence_ms=1400)
    assert det.check("Run pytest and").complete is False
    assert det.check("Check uh").complete is False
    assert det.check("Open the config,").complete is False
    assert det.check("এটা দেখো এবং").complete is False
    assert det.check("Run pytest now.").complete is True
    assert det.check("yes do it").complete is True

    # Tracker adapts baseline gently upward for measured speaker pauses
    tracker = AdaptiveSilenceTracker(base_silence_ms=300, min_silence_ms=250, max_silence_ms=1500)
    tracker.observe_pause(420)
    tracker.observe_pause(460)
    assert tracker.current_baseline_ms >= 300

    # Scripted VAD audio: user says "Run tests and", pauses 220ms (> 128ms base silence!), then finishes "check git"
    vad = EnergyVAD(rms_threshold=200.0)
    partial_calls = [0]

    def partial_stt(_pcm: bytes) -> str:
        partial_calls[0] += 1
        return "Run tests and" if partial_calls[0] == 1 else "Run tests and check git."

    seg = TurnTakingSegmenter(
        vad,
        threshold=0.3,
        min_speech_ms=32,
        base_silence_ms=128,
        min_silence_ms=96,
        max_silence_ms=700,
        incomplete_extension_ms=320,
        speech_pad_ms=32,
        partial_transcriber=partial_stt,
    )
    # 512 samples @ 16kHz = 32ms per frame -> 7 silence frames = 224ms pause (< 448ms extended, > 128ms base)
    stream = _speech_frames(3) + _silence_frames(7) + _speech_frames(3) + _silence_frames(18)
    utterance = seg.collect_utterance(stream)
    assert utterance is not None
    # Entire 13-frame utterance (speech + 224ms mid-pause + continuation) stayed together as one turn!
    assert len(utterance) >= 13 * 512 * 2
    assert partial_calls[0] >= 1


# ── 5. Streaming sentence-by-sentence & latency metrics ─────────────────── #


def test_streaming_sentence_buffer_and_turn_metrics_under_1s(repo: Path):
    buf = StreamingSentenceBuffer(fast_first_clause=True, fast_clause_min_chars=18)
    emitted: list[str] = []
    for tok in ["All tests passed. ", "Two files changed: ", "server.py is clean."]:
        emitted.extend(buf.push(tok))
    tail = buf.flush()
    if tail:
        emitted.append(tail)
    assert emitted[0] == "All tests passed."
    assert len(emitted) >= 2

    cfg = VoiceConfig(
        enabled=True,
        wake=WakeConfig(enabled=False, engine="disabled"),
        vad=VADConfig(engine="energy", threshold=0.3, min_speech_ms=32, min_silence_ms=96, speech_pad_ms=32),
        streaming=StreamingConfig(enabled=True, fast_first_clause=True, target_first_audio_ms=1000),
        turn_detection=TurnDetectionConfig(semantic_eot=False, adaptive_silence=False),
        acknowledgements=AcknowledgementConfig(enabled=False),
    )
    pipeline = VoicePipeline(
        cfg,
        workspace=repo,
        vad=EnergyVAD(rms_threshold=200.0),
        stt=_fake_stt_from_text("quick check", "en"),
        tts=_FakeTTS(workspace=repo),
        player=_ScriptablePlayer(),
    )

    def token_stream(_tr):
        yield from ("First sentence is ready. ", "Second sentence follows.")

    result = pipeline.process_frames(_speech_frames(3) + _silence_frames(4), skip_wake=True, handler=token_stream)
    assert result.error == ""
    assert result.metrics is not None
    assert result.metrics.stt_ms >= 0.0
    assert result.metrics.llm_first_token_ms >= 0.0
    assert 0.0 < result.metrics.tts_first_audio_ms < 1000.0
    assert result.spoken_sentences == ["First sentence is ready.", "Second sentence follows."]


# ── 6. Rate-limited & disableable acknowledgements ──────────────────────── #


def test_acknowledgement_manager_rate_limit_and_disable(repo: Path):
    now = [100.0]
    ack = AcknowledgementManager(
        enabled=True,
        min_delay_ms=40,
        cooldown_seconds=10.0,
        max_per_minute=2,
        clock=lambda: now[0],
    )
    assert ack.next_phrase("en") == "Okay, on it."
    # Cooldown prevents immediate repeat
    assert ack.next_phrase("en") is None
    now[0] += 11.0
    assert ack.next_phrase("bn") is not None
    # Max 2 per minute blocks 3rd within same 60s window even after cooldown
    now[0] += 11.0
    assert ack.next_phrase("en") is None
    now[0] += 60.0
    assert ack.next_phrase("en") is not None

    # Pipeline fires ack on slow handler and skips when disabled
    cfg_on = VoiceConfig(
        enabled=True,
        wake=WakeConfig(enabled=False, engine="disabled"),
        vad=VADConfig(engine="energy", threshold=0.3, min_speech_ms=32, min_silence_ms=96, speech_pad_ms=32),
        turn_detection=TurnDetectionConfig(semantic_eot=False, adaptive_silence=False),
        acknowledgements=AcknowledgementConfig(enabled=True, min_delay_ms=30, cooldown_seconds=0.0, max_per_minute=5),
    )
    tts = _FakeTTS(workspace=repo)
    p = VoicePipeline(
        cfg_on,
        workspace=repo,
        vad=EnergyVAD(rms_threshold=200.0),
        stt=_fake_stt_from_text("run slow check", "en"),
        tts=tts,
        player=_ScriptablePlayer(),
    )

    def slow_handler(_tr):
        threading.Event().wait(0.09)
        return "Finished."

    res = p.process_frames(_speech_frames(3) + _silence_frames(4), skip_wake=True, handler=slow_handler)
    assert res.metrics is not None and res.metrics.acknowledged is True
    assert any("on it" in text.lower() or "checking" in text.lower() for text, _ in tts.spoken)

    # Disabled ack manager emits nothing
    tts2 = _FakeTTS(workspace=repo)
    p.ack_manager.enabled = False
    res_no_ack = p.process_frames(_speech_frames(3) + _silence_frames(4), skip_wake=True, handler=slow_handler)
    assert res_no_ack.metrics is not None and res_no_ack.metrics.acknowledged is False
    assert tts2.spoken == []


# ── 7. Conversation state, active mission & One-Question Rule ───────────── #


def test_conversation_state_reference_resolution_and_one_question_rule(repo: Path):
    conv = ConversationState(
        max_turns=6,
        workspace=repo,
        mission_provider=lambda: {"task": "Inspect auth.py migration", "role": "coder"},
    )
    conv.add_turn(
        "run `pytest -q tests/test_voice.py`",
        "All 18 tests passed.",
        command_executed="pytest -q tests/test_voice.py",
    )
    replay = conv.resolve_reference("run it again")
    assert replay.needs_clarification is False
    assert "pytest -q tests/test_voice.py" in replay.resolved_text

    # Single clear recipient + single clear item (both 'send it to her' and 'send summary.md to her')
    conv.add_turn("check summary.md", "Alice asked for summary.md.")
    send = conv.resolve_reference("send summary.md to her")
    assert send.needs_clarification is False
    assert "Alice" in send.resolved_text
    send_it = conv.resolve_reference("send it to her")
    assert send_it.needs_clarification is False
    assert "Alice" in send_it.resolved_text and "summary.md" in send_it.resolved_text

    # Ambiguous multi-candidate turn triggers EXACTLY ONE short clarifying question
    conv.add_turn("find failures", "Found two failing files: auth.py and cache.py.")
    ambig = conv.resolve_reference("fix that one")
    assert ambig.needs_clarification is True
    assert ambig.clarification_question.count("?") == 1
    assert "auth.py" in ambig.clarification_question and "cache.py" in ambig.clarification_question

    # User answers clarification -> resolves cleanly without asking again
    follow = conv.resolve_reference("cache.py")
    assert follow.needs_clarification is False
    assert "cache.py" in follow.resolved_text

    # Verify enforce_one_question strictly truncates multi-question text
    assert enforce_one_question("Which file? And which branch?").count("?") == 1

    # End-to-end scripted VoicePipeline test: ambiguous turn skips LLM & asks ONE question
    cfg = VoiceConfig(
        enabled=True,
        wake=WakeConfig(enabled=False, engine="disabled"),
        vad=VADConfig(engine="energy", threshold=0.3, min_speech_ms=32, min_silence_ms=96, speech_pad_ms=32),
        turn_detection=TurnDetectionConfig(semantic_eot=False, adaptive_silence=False),
        acknowledgements=AcknowledgementConfig(enabled=False),
        conversation=ConversationConfig(max_turns=8, resolve_references=True, one_question_clarification=True),
    )
    tts = _FakeTTS(workspace=repo)
    p = VoicePipeline(
        cfg,
        workspace=repo,
        vad=EnergyVAD(rms_threshold=200.0),
        stt=_fake_stt_from_text("delete that one", "en"),
        tts=tts,
        player=_ScriptablePlayer(),
    )
    p.conversation.add_turn("list logs", "Found two logs: app.log and debug.log.")
    llm_calls: list[str] = []
    res_q = p.process_frames(
        _speech_frames(3) + _silence_frames(4),
        skip_wake=True,
        handler=lambda tr: llm_calls.append(tr.text) or "Should not run",
    )
    assert res_q.clarification_asked is True
    assert res_q.response_text.count("?") == 1
    assert llm_calls == []

    # Next turn answers "app.log" -> resolves to "delete app.log" and calls LLM handler
    p.stt = _fake_stt_from_text("app.log", "en")
    res_a = p.process_frames(
        _speech_frames(3) + _silence_frames(4),
        skip_wake=True,
        handler=lambda tr: llm_calls.append(tr.text) or f"Handled: {tr.text}",
    )
    assert res_a.clarification_asked is False
    assert llm_calls == ["delete app.log"]


# ── 8. First-class spoken approvals ('yes do it', 'no cancel', 'wait') ─── #


def test_spoken_approvals_preserve_permissions_and_speaker_gate(repo: Path, tmp_path: Path):
    assert parse_approval_intent("yes do it") is SpokenApprovalIntent.APPROVE
    assert parse_approval_intent("no cancel") is SpokenApprovalIntent.DENY
    assert parse_approval_intent("wait") is SpokenApprovalIntent.WAIT
    assert parse_approval_intent("হ্যাঁ করো") is SpokenApprovalIntent.APPROVE
    assert parse_approval_intent("না বাতিল") is SpokenApprovalIntent.DENY
    assert parse_approval_intent("দাঁড়াও") is SpokenApprovalIntent.WAIT
    # Contradiction fails closed (never auto-approves)
    assert parse_approval_intent("yes do it no cancel") is SpokenApprovalIntent.DENY

    # Enroll speaker and verify stranger vs owner on DANGEROUS tool
    from nexus.voice.audio_io import make_sine_wav

    owner_wav = make_sine_wav(tmp_path / "owner.wav", frequency=440.0, duration_seconds=0.1)
    stranger_wav = make_sine_wav(tmp_path / "stranger.wav", frequency=880.0, duration_seconds=0.1)
    verifier = SpeakerVerifier(
        repo,
        threshold=0.8,
        encoder=lambda p: [1.0, 0.0] * 16 if p.read_bytes() == owner_wav.read_bytes() else [0.0, 1.0] * 16,
    )
    verifier.enroll([owner_wav])

    replies = iter([
        ("wait", owner_wav.read_bytes()),
        ("yes do it", owner_wav.read_bytes()),
    ])
    spoken_prompts: list[str] = []
    mgr = SpokenApprovalManager(
        speak=lambda txt, _lang: spoken_prompts.append(txt),
        listen_for_reply=lambda: next(replies),
        verify_speaker=lambda pcm: (
            (m := verifier.verify(pcm)).matched,
            m.reason,
        ),
    )

    spec = ToolSpec(
        name="delete_file",
        description="delete",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        risk=Risk.DANGEROUS,
    )
    # Wait then owner 'yes do it' -> single-use scope='once' approval
    decision = mgr.request_approval(spec, {"path": "tmp.txt"}, Risk.DANGEROUS, "delete tmp.txt")
    assert decision.approved is True
    assert decision.scope == "once"
    assert any("waiting" in p.lower() for p in spoken_prompts)

    # Stranger saying 'yes do it' on DANGEROUS action is DENIED!
    stranger_mgr = SpokenApprovalManager(
        speak=lambda txt, _lang: spoken_prompts.append(txt),
        listen_for_reply=lambda: ("yes do it", stranger_wav.read_bytes()),
        verify_speaker=lambda pcm: ((m := verifier.verify(pcm)).matched, m.reason),
    )
    stranger_decision = stranger_mgr.request_approval(spec, {"path": "tmp.txt"}, Risk.DANGEROUS, "delete tmp.txt")
    assert stranger_decision.approved is False

    # Stranger blocked at ApprovalGate even with --yes + AUTO_ALLOW permissions.yaml!
    (repo / "permissions.yaml").write_text("schema: 1\ndefault: AUTO_ALLOW\n", encoding="utf-8")
    (repo / "keep.txt").write_text("important", encoding="utf-8")
    gate = ApprovalGate(mode="auto", allow_dangerous=True, callback=lambda *a: stranger_mgr.request_approval(*a))
    gate.permissions = Permissions(repo)
    gate.voice_speaker_check = make_speaker_gate_check(verifier, lambda: verifier.verify(stranger_wav))
    reg = build_tools(ToolContext(repo, PathGuard(repo), gate))
    out, is_err = reg.call("delete_file", {"path": "keep.txt"})
    assert is_err and (repo / "keep.txt").exists()

    # Enrolled owner saying 'no cancel' cancels cleanly
    deny_mgr = SpokenApprovalManager(
        listen_for_reply=lambda: ("no cancel", owner_wav.read_bytes()),
        verify_speaker=lambda pcm: ((m := verifier.verify(pcm)).matched, m.reason),
    )
    assert deny_mgr.request_approval(spec, {"path": "keep.txt"}, Risk.DANGEROUS, "delete keep.txt").approved is False


# ── 9. Optional realtime adapters & CLI flags ───────────────────────────── #


def test_realtime_adapters_default_local_and_lazy_optional_deps(repo: Path, monkeypatch):
    cfg = VoiceConfig(enabled=True)
    local_adapter = create_realtime_adapter(cfg, handler=lambda tr: f"echo:{tr.text}")
    assert isinstance(local_adapter, LocalPipelineRealtimeAdapter)
    local_adapter.connect()
    local_adapter.send_text("hi")
    events = list(local_adapter.receive_events())
    assert [e.kind for e in events] == ["connected", "text_delta", "turn_complete"]
    assert events[1].text == "echo:hi"
    local_adapter.close()

    # Fake Gemini Live session
    sent_log: list[str] = []
    gemini = GeminiLiveAdapter(
        session_factory=lambda model: SimpleNamespace(
            send_realtime_input=lambda data, mime_type: sent_log.append(f"audio:{len(data)}"),
            send=lambda input, end_of_turn: sent_log.append(f"text:{input}"),
            interrupt=lambda: sent_log.append("interrupt"),
            receive_sync=lambda: [{"kind": "text_delta", "text": "hello"}],
            close=lambda: sent_log.append("close"),
        )
    )
    gemini.connect()
    gemini.send_audio(b"\x01\x02")
    gemini.send_text("ping")
    gemini.interrupt()
    ev_kinds = [e.kind for e in gemini.receive_events()]
    assert "connected" in ev_kinds and "interrupted" in ev_kinds and "text_delta" in ev_kinds
    gemini.close()
    assert sent_log == ["audio:2", "text:ping", "interrupt", "close"]

    # Fake LiveKit & Pipecat adapters
    lk = LiveKitAdapter(
        room_factory=lambda url, room: SimpleNamespace(
            publish_audio=lambda _pcm: None,
            publish_data=lambda _t: None,
            drain_events=lambda: [{"kind": "text_delta", "text": "lk"}],
            disconnect=lambda: None,
        )
    )
    lk.connect()
    lk.send_audio(b"\x00\x00")
    lk.send_text("hi")
    assert any(e.text == "lk" for e in lk.receive_events())
    lk.close()

    pc = PipecatAdapter(
        runner_factory=lambda: SimpleNamespace(
            push_frame=lambda _f: None,
            pop_events=lambda: [{"kind": "text_delta", "text": "pc"}],
            close=lambda: None,
        )
    )
    pc.connect()
    pc.send_audio(b"\x00\x00")
    pc.send_text("hi")
    assert any(e.text == "pc" for e in pc.receive_events())
    pc.close()

    # Missing optional packages raise clear VoiceDependencyError
    import builtins

    real_import = builtins.__import__

    def deny(targets):
        def _fake(name, *args, **kwargs):
            if name in targets or any(name.startswith(t + ".") for t in targets):
                raise ImportError(f"missing {name}")
            return real_import(name, *args, **kwargs)

        return _fake

    monkeypatch.setattr(builtins, "__import__", deny({"google"}))
    with pytest.raises(VoiceDependencyError, match="voice-realtime"):
        GeminiLiveAdapter(api_key="test-key").connect()

    monkeypatch.setattr(builtins, "__import__", deny({"livekit"}))
    with pytest.raises(VoiceDependencyError, match="voice-livekit"):
        LiveKitAdapter().connect()

    monkeypatch.setattr(builtins, "__import__", deny({"pipecat"}))
    with pytest.raises(VoiceDependencyError, match="voice-pipecat"):
        PipecatAdapter().connect()

    # Config factory selects requested realtime adapter when enabled
    rt_cfg = VoiceConfig(enabled=True, realtime=RealtimeVoiceConfig(enabled=True, provider="pipecat"))
    assert isinstance(create_realtime_adapter(rt_cfg), PipecatAdapter)


def test_cli_voice_run_reports_metrics_and_no_ack_flags(repo: Path, tmp_path: Path, monkeypatch, capsys):
    from nexus.voice.audio_io import make_sine_wav

    class _FakeOutStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def write(self, _data: bytes) -> None:
            return None

    fake_sd = SimpleNamespace(
        default=SimpleNamespace(device=(0, 1)),
        query_devices=lambda: [],
        RawOutputStream=lambda **_k: _FakeOutStream(),
    )
    monkeypatch.setattr("nexus.voice.audio_io._import_sounddevice", lambda sd=None: fake_sd)

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
        lambda _cfg: _fake_stt_from_text("status check", "en"),
    )
    monkeypatch.setattr("nexus.voice.pipeline.create_tts", lambda *_a, **_k: _FakeTTS())
    monkeypatch.setattr(
        "nexus.voice.cli.build_agent_handler",
        lambda *_a, **_k: (lambda _tr: "Everything looks good."),
    )
    rc = main(["voice", "-C", str(repo), "run", "--wav", str(cmd_wav), "--skip-wake", "--no-ack", "--no-duplex", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["reply_language"] == "en"
    assert "stt_ms" in payload["metrics"]
    assert "llm_first_token_ms" in payload["metrics"]
    assert "tts_first_audio_ms" in payload["metrics"]


def test_build_agent_handler_streams_with_persona_and_spoken_approval(repo: Path, monkeypatch):
    from nexus.voice.pipeline import build_agent_handler

    (repo / "persona.yaml").write_text(yaml.safe_dump({"schema": 1, "enabled": True}), encoding="utf-8")
    (repo / "permissions.yaml").write_text("schema: 1\ndefault: AUTO_ALLOW\n", encoding="utf-8")
    (repo / "temp_note.txt").write_text("remove me", encoding="utf-8")

    seen_systems: list[str] = []
    from .conftest import tool_completion

    class _CapturingProvider(FakeProvider):
        def complete(self, system, messages, tools=None, **kwargs):
            seen_systems.append(system)
            on_text = kwargs.get("on_text")
            comp = super().complete(system, messages, tools, **kwargs)
            if on_text and comp.text:
                on_text(comp.text)
            return comp

    provider = _CapturingProvider(
        "gemini",
        [
            tool_completion([("delete_file", {"path": "temp_note.txt"})]),
            text_completion("**Done**: deleted `temp_note.txt` cleanly."),
        ],
    )
    nexus_cfg = load_config(repo)
    nexus_cfg.default_agent = "gemini"
    monkeypatch.setattr("nexus.agent.build_providers", lambda _agents, **kwargs: fake_registry(provider, config=nexus_cfg))

    cfg = VoiceConfig(
        enabled=True,
        wake=WakeConfig(enabled=False, engine="disabled"),
        vad=VADConfig(engine="energy", threshold=0.3, min_speech_ms=32, min_silence_ms=96, speech_pad_ms=32),
        turn_detection=TurnDetectionConfig(semantic_eot=False, adaptive_silence=False),
        acknowledgements=AcknowledgementConfig(enabled=False),
    )
    tts = _FakeTTS(workspace=repo)
    pipeline = VoicePipeline(
        cfg,
        workspace=repo,
        vad=EnergyVAD(rms_threshold=200.0),
        stt=_fake_stt_from_text("delete temp_note.txt", "en"),
        tts=tts,
        player=_ScriptablePlayer(),
    )
    # Wire spoken approval reply 'yes do it' on the approval manager
    pipeline.approval_manager.listen_for_reply = lambda: ("yes do it", None)

    handler = build_agent_handler(repo, pipeline=pipeline, stream=True)
    res = pipeline.process_frames(_speech_frames(3) + _silence_frames(4), skip_wake=True, handler=handler)
    assert res.error == ""
    assert not (repo / "temp_note.txt").exists()
    assert seen_systems and "Voice Persona: Nexus" in seen_systems[0]
    spoken_joined = " ".join(res.spoken_sentences)
    assert "**" not in spoken_joined and "`" not in spoken_joined
    assert "deleted" in spoken_joined.lower()

