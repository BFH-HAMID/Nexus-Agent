"""Voice pipeline state machine: full duplex, adaptive EOT, streaming TTS, persona, conversation state & approvals."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..types import Event, EventHandler, EventType, Risk, ToolSpec
from .approvals import SpokenApprovalIntent, SpokenApprovalManager, build_voice_approval_callback
from .audio_io import AudioInputStream, AudioOutputPlayer, iter_wav_frames, read_wav_pcm
from .config import VoiceConfig, VoiceDisabledError, VoiceError, load_voice_config, write_hud_state
from .conversation import AcknowledgementManager, ConversationState
from .duplex import (
    BargeInAction,
    DuplexState,
    FullDuplexMonitor,
    StreamingSentenceBuffer,
    TurnMetrics,
    classify_barge_in,
)
from .persona import (
    PersonaConfig,
    build_persona_prompt,
    dispatch_long_output,
    format_for_speech,
    load_persona_config,
    resolve_reply_language,
)
from .realtime import RealtimeVoiceAdapter, create_realtime_adapter
from .speaker import SpeakerMatch, SpeakerVerifier, is_dangerous_action, make_speaker_gate_check
from .stt import STTProvider, TranscriptResult, create_stt
from .tts import PlaybackQueue, TTSProvider, create_tts, iter_sentences, sanitize_tts_text
from .turn_detection import AdaptiveSilenceTracker, SemanticTurnDetector, TurnTakingSegmenter
from .vad import BaseVAD, VADSegmenter, create_vad
from .wake import WakeDetector, WakeEvent, create_wake_detector


class VoiceState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


@dataclass(frozen=True)
class VoiceStateEvent:
    state: VoiceState
    previous: VoiceState
    timestamp: float
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "previous": self.previous.value,
            "timestamp": self.timestamp,
            **self.detail,
        }


@dataclass
class VoiceTurnResult:
    transcript: TranscriptResult | None = None
    response_text: str = ""
    spoken_sentences: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    speaker_match: SpeakerMatch | None = None
    wake_event: WakeEvent | None = None
    interrupted: bool = False
    error: str = ""
    metrics: TurnMetrics | None = None
    clarification_asked: bool = False
    hud_dispatched: bool = False
    barge_in_action: str = ""
    resumed: bool = False
    reply_language: str = "en"
    approval_intent: str = ""


TextHandler = Callable[[TranscriptResult], str | Iterable[str]]


class VoicePipeline:
    """Full-duplex, persona-aware voice pipeline with streaming TTS, conversation state, and spoken approvals."""

    def __init__(
        self,
        config: VoiceConfig | None = None,
        *,
        workspace: Path | str | None = None,
        persona: PersonaConfig | None = None,
        wake_detector: WakeDetector | None = None,
        vad: BaseVAD | None = None,
        stt: STTProvider | None = None,
        tts: TTSProvider | None = None,
        player: AudioOutputPlayer | None = None,
        speaker_verifier: SpeakerVerifier | None = None,
        text_handler: TextHandler | None = None,
        conversation: ConversationState | None = None,
        ack_manager: AcknowledgementManager | None = None,
        approval_manager: SpokenApprovalManager | None = None,
        realtime_adapter: RealtimeVoiceAdapter | None = None,
        partial_transcriber: Callable[[bytes], str] | None = None,
        on_state_change: Callable[[VoiceStateEvent], None] | None = None,
        on_partial_transcript: Callable[[str], None] | None = None,
        emit: EventHandler | None = None,
        chief_factory: Callable[[Path], Any] | None = None,
        proactive_factory: Callable[[Path], Any] | None = None,
        email_session: Any = None,
        contact_resolver: Callable[[str], dict[str, Any] | None] | None = None,
        long_term_memory: Any = None,
        keyring_module: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.config = config if config is not None else load_voice_config(self.workspace)
        self.persona = persona if persona is not None else load_persona_config(self.workspace)
        self.wake_detector = wake_detector
        self.vad = vad
        self.stt = stt
        self.tts = tts
        self.player = player or AudioOutputPlayer(device=self.config.output_device)
        self.speaker_verifier = speaker_verifier
        self.text_handler = text_handler
        self._keyring_module = keyring_module
        resolved_contact_cb = contact_resolver if contact_resolver is not None else self._default_contact_resolver
        self.conversation = conversation or ConversationState(
            max_turns=self.config.conversation.max_turns,
            workspace=self.workspace,
            contact_resolver=resolved_contact_cb,
        )
        if conversation is not None and conversation.contact_resolver is None:
            conversation.contact_resolver = resolved_contact_cb
        from ..personal_integrations import VoiceEmailSession

        self.email_session = email_session or VoiceEmailSession(
            self.workspace,
            keyring_module=keyring_module,
        )
        from ..memory import LongTermMemory

        self.long_term_memory = long_term_memory or LongTermMemory(
            self.workspace,
            keyring_module=keyring_module,
        )
        self.ack_manager = ack_manager or AcknowledgementManager(
            enabled=self.config.acknowledgements.enabled,
            min_delay_ms=self.config.acknowledgements.min_delay_ms,
            cooldown_seconds=self.config.acknowledgements.cooldown_seconds,
            max_per_minute=self.config.acknowledgements.max_per_minute,
        )
        self.approval_manager = approval_manager or SpokenApprovalManager(
            workspace=self.workspace,
            speak=lambda txt, lang: self._speak_immediate(txt, lang),
            verify_speaker=self._verify_audio_for_approval,
            timeout_seconds=self.config.conversation.approval_timeout_seconds,
        )
        self.realtime_adapter = realtime_adapter
        self.partial_transcriber = partial_transcriber
        self.on_state_change = on_state_change
        self.on_partial_transcript = on_partial_transcript
        self.emit = emit
        self.chief_factory = chief_factory
        self.proactive_factory = proactive_factory
        self.clock = clock

        self.state = VoiceState.IDLE
        self.state_history: list[VoiceStateEvent] = []
        self.last_speaker_match: SpeakerMatch | None = None
        self.last_metrics: TurnMetrics | None = None
        self.duplex_state = DuplexState()
        self.llm_cancel_event = threading.Event()
        self._lock = threading.RLock()
        self._playback_queue: PlaybackQueue | None = None
        self._interrupt_event = threading.Event()
        self._duplex_monitor: FullDuplexMonitor | None = None
        self._pending_barge_prefix: bytes = b""
        self._pending_chief_mission_id: str | None = None

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise VoiceDisabledError(
                "Voice pipeline is disabled by voice.yaml (enabled: false). "
                "Set `enabled: true` in voice.yaml to activate voice."
            )

    def _ensure_components(self) -> None:
        if self.wake_detector is None:
            self.wake_detector = create_wake_detector(self.config)
        if self.vad is None:
            self.vad = create_vad(self.config)
        if self.stt is None:
            self.stt = create_stt(self.config)
        if self.tts is None:
            self.tts = create_tts(self.config, workspace=self.workspace)
        if self.speaker_verifier is None:
            candidate = SpeakerVerifier(self.workspace, threshold=self.config.speaker.threshold)
            if self.config.speaker.enabled or candidate.is_enrolled():
                self.speaker_verifier = candidate
        if self.realtime_adapter is None and self.config.realtime.enabled and self.config.realtime.provider != "local":
            self.realtime_adapter = create_realtime_adapter(self.config, pipeline=self, handler=self.text_handler)

    def _default_contact_resolver(self, query: str) -> dict[str, Any] | None:
        from ..personal_integrations import IntegrationError, PersonalIntegrations, load_integrations_policy

        try:
            pol = load_integrations_policy(self.workspace)
        except ValueError:
            return None
        if not (pol.get("enabled") and pol["contacts"].get("enabled") and pol["contacts"].get("allow_read")):
            return None
        try:
            return PersonalIntegrations(self.workspace, keyring_module=self._keyring_module).resolve_contact(query)
        except IntegrationError:
            return None

    def _verify_audio_for_approval(self, audio_pcm: bytes | None) -> tuple[bool, str]:
        verifier = self.active_speaker_verifier()
        if verifier is None:
            if self.config.speaker.enabled:
                return False, "Speaker verification enabled without enrolled voiceprint"
            return True, "speaker verification not enabled"
        match = verifier.verify(audio_pcm) if audio_pcm else self.last_speaker_match
        if match is None or not match.enrolled or not match.matched:
            return False, match.reason if match and match.reason else "Voice did not match enrolled speaker"
        return True, "enrolled speaker verified"

    def _speak_immediate(self, text: str, language: str = "en") -> None:
        if self.tts is None or not text:
            return
        safe = sanitize_tts_text(text, workspace=self.workspace)
        if not safe:
            return
        from ..hud.presence import Presence
        hub = Presence(self.workspace)
        if hub.speaker() != 'local':
            hub.say(safe, language=language)
            return
        clip = self.tts.synthesize(safe, language=language, rate=self.config.tts.speaking_rate)
        if hub.speaker() != 'local':
            return  # Handoff during synthesis: do not play or retry on another device.
        self.player.play(clip, self._interrupt_event)

    def transition(self, new_state: VoiceState, **detail: Any) -> VoiceStateEvent:
        with self._lock:
            prev = self.state
            self.state = new_state
            event = VoiceStateEvent(
                state=new_state,
                previous=prev,
                timestamp=self.clock(),
                detail=dict(detail),
            )
            self.state_history.append(event)
        if self.on_state_change is not None:
            self.on_state_change(event)
        if self.emit is not None:
            self.emit(Event(EventType.VOICE_STATE, event.to_dict()))
        if self.config.hud.publish_events:
            try:
                write_hud_state(self.workspace, {"enabled": self.config.enabled, **event.to_dict()})
            except OSError:
                pass
        if self.config.enabled:
            from ..hud.presence import publish_voice
            publish_voice(self.workspace, new_state.value, transcript=str(detail.get('transcript') or ''))
        return event

    def interrupt_speech(self) -> list[str]:
        """Barge-in (<200 ms): cancel ongoing/queued TTS playback and active LLM token stream."""
        t0 = time.perf_counter()
        self._interrupt_event.set()
        self.llm_cancel_event.set()
        dropped_texts: list[str] = []
        if self._playback_queue is not None:
            dropped = self._playback_queue.barge_in()
            dropped_texts = [item.text for item in dropped if item.text]
            if dropped_texts:
                self.duplex_state.paused_sentences = list(dropped_texts)
        if self.realtime_adapter is not None:
            try:
                self.realtime_adapter.interrupt()
            except Exception:
                pass
        cancel_ms = (time.perf_counter() - t0) * 1000.0
        if self.last_metrics is not None:
            self.last_metrics.interrupted = True
            self.last_metrics.barge_in_cancel_ms = round(cancel_ms, 2)
        return dropped_texts

    def feed_duplex_frame(self, pcm: bytes) -> bool:
        """Feed live mic frames during TTS/thinking to trigger full-duplex barge-in."""
        monitor = self._duplex_monitor
        if monitor is None:
            return False
        return monitor.feed_frame(pcm)

    def active_speaker_verifier(self) -> SpeakerVerifier | None:
        if self.speaker_verifier is not None:
            return self.speaker_verifier
        candidate = SpeakerVerifier(self.workspace, threshold=self.config.speaker.threshold)
        if self.config.speaker.enabled or candidate.is_enrolled():
            return candidate
        return None

    def make_approval_gate_check(self) -> Callable[[ToolSpec, dict[str, Any]], tuple[bool, str]] | None:
        verifier = self.active_speaker_verifier()
        if verifier is None:
            return None
        return make_speaker_gate_check(
            verifier,
            lambda: self.last_speaker_match,
            require_for_dangerous=self.config.speaker.require_for_dangerous,
            require_always=self.config.speaker.require_always,
        )

    def authorize_action(self, risk: Risk, audio: Path | str | bytes | None = None) -> tuple[bool, str]:
        """Explicitly check speaker verification policy for an action risk level."""
        verifier = self.active_speaker_verifier()
        dangerous = risk is Risk.DANGEROUS
        if verifier is None:
            if dangerous and self.config.speaker.enabled:
                return False, "Speaker verification enabled without enrolled voiceprint"
            return True, "speaker verification not enabled"
        if not dangerous and not self.config.speaker.require_always:
            return True, "non-dangerous action"
        match = verifier.verify(audio) if audio is not None else self.last_speaker_match
        if match is None or not match.enrolled or not match.matched:
            reason = match.reason if match and match.reason else "DANGEROUS action accepts only the enrolled voice"
            return False, reason
        return True, "enrolled speaker verified"

    def _build_segmenter(self) -> VADSegmenter | TurnTakingSegmenter:
        assert self.vad is not None
        td = self.config.turn_detection
        if td.semantic_eot or td.adaptive_silence or self.partial_transcriber is not None:
            detector = SemanticTurnDetector(
                base_silence_ms=self.config.vad.min_silence_ms,
                incomplete_extension_ms=td.incomplete_extension_ms,
                max_adaptive_silence_ms=td.max_silence_ms,
            )
            tracker = AdaptiveSilenceTracker(
                base_silence_ms=self.config.vad.min_silence_ms,
                min_silence_ms=td.min_silence_ms,
                max_silence_ms=td.max_silence_ms,
            )
            return TurnTakingSegmenter(
                self.vad,
                sample_rate=self.config.sample_rate,
                threshold=self.config.vad.threshold,
                min_speech_ms=self.config.vad.min_speech_ms,
                base_silence_ms=self.config.vad.min_silence_ms,
                min_silence_ms=td.min_silence_ms,
                max_silence_ms=td.max_silence_ms,
                incomplete_extension_ms=td.incomplete_extension_ms,
                speech_pad_ms=self.config.vad.speech_pad_ms,
                max_utterance_seconds=self.config.vad.max_utterance_seconds,
                partial_transcriber=self.partial_transcriber,
                semantic_detector=detector,
                silence_tracker=tracker,
            )
        return VADSegmenter(
            self.vad,
            sample_rate=self.config.sample_rate,
            threshold=self.config.vad.threshold,
            min_speech_ms=self.config.vad.min_speech_ms,
            min_silence_ms=self.config.vad.min_silence_ms,
            speech_pad_ms=self.config.vad.speech_pad_ms,
            max_utterance_seconds=self.config.vad.max_utterance_seconds,
        )

    def process_wav(
        self,
        wav_path: Path | str,
        *,
        skip_wake: bool = False,
        handler: TextHandler | None = None,
        barge_in_frames: Iterable[bytes] | None = None,
    ) -> VoiceTurnResult:
        """Run one complete turn from a WAV file (16 kHz mono normalized)."""
        read_wav_pcm(wav_path)
        frame_samples = self.wake_detector.frame_length if self.wake_detector is not None else 512
        frames = iter_wav_frames(wav_path, frame_samples=frame_samples)
        return self.process_frames(frames, skip_wake=skip_wake, handler=handler, barge_in_frames=barge_in_frames)

    def _play_sentences(
        self,
        sentences: Iterable[str],
        *,
        language: str,
        metrics: TurnMetrics,
        t_turn_start: float,
        barge_in_frames: Iterable[bytes] | None = None,
        on_start_speaking: Callable[[], None] | None = None,
    ) -> tuple[list[str], bool]:
        assert self.tts is not None
        assert self.vad is not None

        def _on_first_audio(perf_now: float) -> None:
            if metrics.tts_first_audio_ms <= 0.0:
                metrics.tts_first_audio_ms = round((perf_now - t_turn_start) * 1000.0, 2)
                metrics.e2e_first_audio_ms = metrics.tts_first_audio_ms

        from ..hud.presence import Presence
        presence = Presence(self.workspace)
        playback = PlaybackQueue(
            self.tts,
            self.player,
            workspace=self.workspace,
            on_first_audio=_on_first_audio,
            playback_allowed=lambda: presence.speaker() == 'local',
        )
        self._playback_queue = playback
        spoken_sentences: list[str] = []
        routed_sentences: list[str] = []
        speaking_started = False
        barge_monitor: FullDuplexMonitor | None = None
        stop_monitor = threading.Event()
        monitor_thread: threading.Thread | None = None

        if self.config.duplex.enabled:
            def _on_barge(pcm: bytes) -> None:
                t_barge = time.perf_counter()
                self._pending_barge_prefix = pcm
                dropped = self.interrupt_speech()
                elapsed = (time.perf_counter() - t_barge) * 1000.0
                metrics.interrupted = True
                metrics.barge_in_cancel_ms = round(elapsed, 2)
                if dropped:
                    self.duplex_state.paused_sentences = dropped

            barge_monitor = FullDuplexMonitor(
                self.vad,
                sample_rate=self.config.sample_rate,
                threshold=self.config.duplex.barge_in_threshold,
                min_speech_ms=self.config.duplex.barge_in_min_speech_ms,
                on_barge_in=_on_barge,
            )
            self._duplex_monitor = barge_monitor

        if barge_in_frames is not None and barge_monitor is not None:
            def _feed_barge() -> None:
                for chunk in barge_in_frames:
                    if stop_monitor.is_set() or self._interrupt_event.is_set():
                        break
                    if barge_monitor.feed_frame(chunk):
                        break
                    stop_monitor.wait(0.004)

            playback.start()
            monitor_thread = threading.Thread(target=_feed_barge, daemon=True)
            monitor_thread.start()
        else:
            # Async worker lets TTS synthesize Sentence N while Sentence N+1 streams from LLM.
            playback.start()

        interrupted_flag = False
        try:
            for sentence in sentences:
                if self._interrupt_event.is_set():
                    if not self.duplex_state.paused_sentences:
                        self.duplex_state.paused_sentences = [sentence]
                    else:
                        self.duplex_state.paused_sentences.append(sentence)
                    break
                safe_sentence = sanitize_tts_text(sentence, workspace=self.workspace)
                if not safe_sentence:
                    continue
                if presence.speaker() != 'local':
                    playback.barge_in()
                    presence.say(safe_sentence, language=language)
                    routed_sentences.append(safe_sentence)
                    continue
                if not speaking_started:
                    if on_start_speaking is not None:
                        on_start_speaking()
                    speaking_started = True
                playback.enqueue(safe_sentence, language=language)
                spoken_sentences.append(safe_sentence)

            if not self._interrupt_event.is_set():
                playback.wait_idle(timeout=30.0)
            interrupted_flag = self._interrupt_event.is_set()
        finally:
            stop_monitor.set()
            if monitor_thread is not None:
                monitor_thread.join(timeout=1.0)
            playback.close(wait=True, timeout=2.0)
            self._playback_queue = None
            self._duplex_monitor = None

        if interrupted_flag:
            return list(playback.spoken_sentences), True
        if playback.last_error is not None:
            raise playback.last_error
        return list(playback.spoken_sentences) or spoken_sentences or routed_sentences, False

    def process_frames(
        self,
        frames: Iterable[bytes],
        *,
        skip_wake: bool = False,
        handler: TextHandler | None = None,
        barge_in_frames: Iterable[bytes] | None = None,
    ) -> VoiceTurnResult:
        """Run wake -> listen -> VAD/EOT -> STT -> persona/conversation -> streaming handler -> duplex TTS."""
        self._require_enabled()
        self._ensure_components()
        assert self.wake_detector is not None
        assert self.vad is not None
        assert self.stt is not None
        assert self.tts is not None

        active_handler = handler or self.text_handler

        t_turn_start = time.perf_counter()
        metrics = TurnMetrics(provider=self.config.realtime.provider or "local")
        self.last_metrics = metrics
        self._interrupt_event.clear()
        self.llm_cancel_event.clear()
        self.last_speaker_match = None
        turn_states: list[str] = []

        def _record_state(st: VoiceState, **kw: Any) -> None:
            self.transition(st, **kw)
            turn_states.append(st.value)

        _record_state(VoiceState.IDLE)
        frame_iter = iter(frames)
        wake_event: WakeEvent | None = None

        if not skip_wake and self.config.wake.enabled and self.config.wake.engine != "disabled":
            self.wake_detector.reset()
            for frame in frame_iter:
                wake_event = self.wake_detector.process_frame(frame)
                if wake_event is not None:
                    break
            if wake_event is None:
                return VoiceTurnResult(states=turn_states, error="Wake word was not detected", metrics=metrics)
            _record_state(VoiceState.LISTENING, wake_word=wake_event.word, wake_engine=wake_event.engine)
        else:
            _record_state(VoiceState.LISTENING, wake_engine="direct")

        segmenter = self._build_segmenter()
        utterance_pcm = segmenter.collect_utterance(frame_iter)
        if not utterance_pcm:
            _record_state(VoiceState.IDLE, reason="no_speech")
            return VoiceTurnResult(states=turn_states, wake_event=wake_event, error="No speech detected", metrics=metrics)

        _record_state(VoiceState.THINKING, audio_bytes=len(utterance_pcm))
        try:
            from ..notifications import mark_user_present
            mark_user_present(self.workspace, channel="voice")
        except Exception:
            pass

        if self.speaker_verifier is not None:
            self.last_speaker_match = self.speaker_verifier.verify(utterance_pcm)

        t_stt = time.perf_counter()
        lang_hint = None if self.config.stt.language.lower() == "auto" else self.config.stt.language
        transcript = self.stt.transcribe_stream(
            [utterance_pcm],
            on_partial=self.on_partial_transcript,
            language=lang_hint,
        )
        metrics.stt_ms = round((time.perf_counter() - t_stt) * 1000.0, 2)

        reply_lang = resolve_reply_language(self.persona, transcript.language, transcript.text)
        metrics.language = reply_lang
        self.approval_manager.language = reply_lang

        if self.config.speaker.require_always and self.speaker_verifier is not None:
            if not self.last_speaker_match or not self.last_speaker_match.matched:
                _record_state(VoiceState.IDLE, reason="speaker_rejected")
                return VoiceTurnResult(
                    transcript=transcript,
                    states=turn_states,
                    speaker_match=self.last_speaker_match,
                    wake_event=wake_event,
                    error="Speaker verification failed",
                    metrics=metrics,
                    reply_language=reply_lang,
                )

        # 1. Active spoken approval intercept ('yes do it', 'no cancel', 'wait')
        if self.config.conversation.spoken_approvals and self.approval_manager.pending is not None:
            intent, ack_reply = self.approval_manager.submit_intent(
                transcript.text, audio_pcm=utterance_pcm, language=reply_lang
            )
            if intent is not SpokenApprovalIntent.NONE:
                spoken, interrupted = self._play_sentences(
                    [ack_reply] if ack_reply else [],
                    language=reply_lang,
                    metrics=metrics,
                    t_turn_start=t_turn_start,
                    barge_in_frames=barge_in_frames,
                    on_start_speaking=lambda: _record_state(
                        VoiceState.SPEAKING, transcript=transcript.text, language=reply_lang
                    ),
                )
                metrics.total_turn_ms = round((time.perf_counter() - t_turn_start) * 1000.0, 2)
                _record_state(VoiceState.IDLE, **metrics.to_dict())
                return VoiceTurnResult(
                    transcript=transcript,
                    response_text=ack_reply,
                    spoken_sentences=spoken,
                    states=turn_states,
                    speaker_match=self.last_speaker_match,
                    wake_event=wake_event,
                    interrupted=interrupted,
                    metrics=metrics,
                    reply_language=reply_lang,
                    approval_intent=intent.value,
                )

        # 2. Paused speech after barge-in: classify resume vs discard vs new turn
        if self.duplex_state.paused_sentences:
            action = classify_barge_in(transcript.text)
            if action is BargeInAction.DISCARD:
                self.duplex_state.paused_sentences.clear()
                metrics.barge_in_action = action.value
                metrics.total_turn_ms = round((time.perf_counter() - t_turn_start) * 1000.0, 2)
                _record_state(VoiceState.IDLE, **metrics.to_dict())
                return VoiceTurnResult(
                    transcript=transcript,
                    states=turn_states,
                    speaker_match=self.last_speaker_match,
                    wake_event=wake_event,
                    metrics=metrics,
                    barge_in_action=action.value,
                    reply_language=reply_lang,
                )
            if action is BargeInAction.RESUME:
                resumed_sentences = list(self.duplex_state.paused_sentences)
                resume_lang = self.duplex_state.paused_language or reply_lang
                self.duplex_state.paused_sentences.clear()
                metrics.barge_in_action = action.value
                spoken, interrupted = self._play_sentences(
                    resumed_sentences,
                    language=resume_lang,
                    metrics=metrics,
                    t_turn_start=t_turn_start,
                    barge_in_frames=barge_in_frames,
                    on_start_speaking=lambda: _record_state(
                        VoiceState.SPEAKING, transcript=transcript.text, language=resume_lang
                    ),
                )
                if not interrupted:
                    self.duplex_state.paused_sentences.clear()
                else:
                    self.duplex_state.paused_language = resume_lang
                metrics.total_turn_ms = round((time.perf_counter() - t_turn_start) * 1000.0, 2)
                _record_state(VoiceState.IDLE, **metrics.to_dict())
                return VoiceTurnResult(
                    transcript=transcript,
                    response_text=" ".join(resumed_sentences),
                    spoken_sentences=spoken,
                    states=turn_states,
                    speaker_match=self.last_speaker_match,
                    wake_event=wake_event,
                    interrupted=interrupted,
                    metrics=metrics,
                    barge_in_action=action.value,
                    resumed=True,
                    reply_language=resume_lang,
                )
            # Substantive new turn clears old paused buffer.
            self.duplex_state.paused_sentences.clear()

        # 2b. Chief-of-Staff spoken mission trigger, confirmation, & status/pause/resume/cancel
        chief_reply, chief_intent, chief_err = self._handle_chief_turn(
            transcript.text,
            language=reply_lang,
        )
        if chief_reply is not None:
            spoken, interrupted = self._play_sentences(
                [chief_reply],
                language=reply_lang,
                metrics=metrics,
                t_turn_start=t_turn_start,
                barge_in_frames=barge_in_frames,
                on_start_speaking=lambda: _record_state(
                    VoiceState.SPEAKING, transcript=transcript.text, language=reply_lang
                ),
            )
            metrics.total_turn_ms = round((time.perf_counter() - t_turn_start) * 1000.0, 2)
            _record_state(VoiceState.IDLE, **metrics.to_dict())
            return VoiceTurnResult(
                transcript=transcript,
                response_text=chief_reply,
                spoken_sentences=spoken,
                states=turn_states,
                speaker_match=self.last_speaker_match,
                wake_event=wake_event,
                interrupted=interrupted,
                error=chief_err or "",
                metrics=metrics,
                reply_language=reply_lang,
                approval_intent=chief_intent or "",
            )

        # 2c. Opt-in proactive voice intents (morning brief greeting, focus mode, stop suggesting, voice reminders/tasks)
        proactive_reply, proactive_intent, proactive_err = self._handle_proactive_turn(
            transcript.text,
            language=reply_lang,
        )
        if proactive_reply is not None:
            spoken, interrupted = self._play_sentences(
                [proactive_reply],
                language=reply_lang,
                metrics=metrics,
                t_turn_start=t_turn_start,
                barge_in_frames=barge_in_frames,
                on_start_speaking=lambda: _record_state(
                    VoiceState.SPEAKING, transcript=transcript.text, language=reply_lang
                ),
            )
            metrics.total_turn_ms = round((time.perf_counter() - t_turn_start) * 1000.0, 2)
            _record_state(VoiceState.IDLE, **metrics.to_dict())
            return VoiceTurnResult(
                transcript=transcript,
                response_text=proactive_reply,
                spoken_sentences=spoken,
                states=turn_states,
                speaker_match=self.last_speaker_match,
                wake_event=wake_event,
                interrupted=interrupted,
                error=proactive_err or "",
                metrics=metrics,
                reply_language=reply_lang,
                approval_intent=proactive_intent or "",
            )

        # 2d. Opt-in voice email summary & triage ('read', 'reply', 'ignore')
        email_reply, email_intent = self.email_session.handle_utterance(
            transcript.text,
            language=reply_lang,
        )
        if email_reply is not None:
            spoken, interrupted = self._play_sentences(
                [email_reply],
                language=reply_lang,
                metrics=metrics,
                t_turn_start=t_turn_start,
                barge_in_frames=barge_in_frames,
                on_start_speaking=lambda: _record_state(
                    VoiceState.SPEAKING, transcript=transcript.text, language=reply_lang
                ),
            )
            metrics.total_turn_ms = round((time.perf_counter() - t_turn_start) * 1000.0, 2)
            _record_state(VoiceState.IDLE, **metrics.to_dict())
            return VoiceTurnResult(
                transcript=transcript,
                response_text=email_reply,
                spoken_sentences=spoken,
                states=turn_states,
                speaker_match=self.last_speaker_match,
                wake_event=wake_event,
                interrupted=interrupted,
                metrics=metrics,
                reply_language=reply_lang,
                approval_intent=email_intent or "",
            )

        # 2e. Opt-in long-term memory voice commands & privacy mode
        mem_reply, mem_intent, mem_err = self._handle_memory_turn(transcript.text)
        if mem_reply is not None:
            spoken, interrupted = self._play_sentences(
                [mem_reply],
                language=reply_lang,
                metrics=metrics,
                t_turn_start=t_turn_start,
                barge_in_frames=barge_in_frames,
                on_start_speaking=lambda: _record_state(
                    VoiceState.SPEAKING, transcript=transcript.text, language=reply_lang
                ),
            )
            metrics.total_turn_ms = round((time.perf_counter() - t_turn_start) * 1000.0, 2)
            _record_state(VoiceState.IDLE, **metrics.to_dict())
            return VoiceTurnResult(
                transcript=transcript,
                response_text=mem_reply,
                spoken_sentences=spoken,
                states=turn_states,
                speaker_match=self.last_speaker_match,
                wake_event=wake_event,
                interrupted=interrupted,
                error=mem_err or "",
                metrics=metrics,
                reply_language=reply_lang,
                approval_intent=mem_intent or "",
            )

        if active_handler is None:
            raise VoiceError("VoicePipeline requires a text_handler callback")

        # 3. Conversation reference resolution + One-Question Rule
        effective_transcript = transcript
        if self.config.conversation.resolve_references and self.conversation is not None:
            resolution = self.conversation.resolve_reference(transcript.text, language=reply_lang)
            if resolution.needs_clarification and resolution.clarification_question:
                question = resolution.clarification_question
                self.conversation.add_turn(transcript.text, question, language=reply_lang)
                spoken, interrupted = self._play_sentences(
                    [question],
                    language=reply_lang,
                    metrics=metrics,
                    t_turn_start=t_turn_start,
                    barge_in_frames=barge_in_frames,
                    on_start_speaking=lambda: _record_state(
                        VoiceState.SPEAKING, transcript=transcript.text, language=reply_lang
                    ),
                )
                metrics.total_turn_ms = round((time.perf_counter() - t_turn_start) * 1000.0, 2)
                _record_state(VoiceState.IDLE, **metrics.to_dict())
                return VoiceTurnResult(
                    transcript=transcript,
                    response_text=question,
                    spoken_sentences=spoken,
                    states=turn_states,
                    speaker_match=self.last_speaker_match,
                    wake_event=wake_event,
                    interrupted=interrupted,
                    metrics=metrics,
                    clarification_asked=True,
                    reply_language=reply_lang,
                )
            if resolution.resolved_text and resolution.resolved_text != transcript.text:
                from dataclasses import replace

                effective_transcript = replace(transcript, text=resolution.resolved_text)

        # 4. Rate-limited short acknowledgement if model takes > min_delay_ms
        t_llm = time.perf_counter()
        ack_timer: threading.Timer | None = None
        first_token_seen = threading.Event()
        speaking_started_flag = [False]

        def _ensure_speaking_state() -> None:
            if not speaking_started_flag[0]:
                speaking_started_flag[0] = True
                _record_state(VoiceState.SPEAKING, transcript=transcript.text, language=reply_lang)

        if self.config.acknowledgements.enabled and self.ack_manager.enabled:
            delay_s = max(0.001, self.ack_manager.min_delay_ms / 1000.0)

            def _maybe_ack() -> None:
                if first_token_seen.is_set() or self._interrupt_event.is_set() or self.llm_cancel_event.is_set():
                    return
                phrase = self.ack_manager.next_phrase(reply_lang)
                if not phrase:
                    return
                metrics.acknowledged = True
                _ensure_speaking_state()
                self._speak_immediate(phrase, reply_lang)

            ack_timer = threading.Timer(delay_s, _maybe_ack)
            ack_timer.daemon = True
            ack_timer.start()

        try:
            raw_response = active_handler(effective_transcript)
        finally:
            if ack_timer is not None:
                ack_timer.cancel()

        collected_raw: list[str] = []
        hud_dispatched = False

        if isinstance(raw_response, str):
            first_token_seen.set()
            metrics.llm_first_token_ms = round((time.perf_counter() - t_llm) * 1000.0, 2)
            collected_raw.append(raw_response)
            if self.persona.enabled:
                styled = format_for_speech(
                    raw_response,
                    self.persona,
                    user_language=reply_lang,
                    workspace=self.workspace,
                )
                hud_dispatched = bool(styled.dispatched_to)
                sentence_iter: Iterable[str] = iter_sentences([styled.spoken_text]) if styled.spoken_text else []
            else:
                sentence_iter = iter_sentences([raw_response])
        else:
            # True streaming path: sentence buffer flushes sentence-by-sentence directly as tokens stream in!
            buf = StreamingSentenceBuffer(fast_first_clause=self.config.streaming.fast_first_clause)
            max_spoken = self.persona.spoken_style.max_spoken_chars if self.persona.enabled else 10_000
            emitted_spoken_chars = 0
            truncated_long = False

            def _format_chunk(chunk_text: str) -> str:
                return format_for_speech(
                    chunk_text,
                    self.persona,
                    user_language=reply_lang,
                    workspace=self.workspace,
                    dispatch_full=lambda *_: (),
                ).spoken_text

            def _stream_sentences() -> Iterator[str]:
                nonlocal hud_dispatched, emitted_spoken_chars, truncated_long
                for piece in raw_response:
                    if self._interrupt_event.is_set() or self.llm_cancel_event.is_set():
                        return
                    if not piece:
                        continue
                    if not first_token_seen.is_set():
                        first_token_seen.set()
                        if ack_timer is not None:
                            ack_timer.cancel()
                        metrics.llm_first_token_ms = round((time.perf_counter() - t_llm) * 1000.0, 2)
                    collected_raw.append(piece)
                    if truncated_long:
                        continue
                    for sent in buf.push(piece):
                        out_sent = _format_chunk(sent) if self.persona.enabled else sent
                        if not out_sent:
                            continue
                        if self.persona.enabled and (emitted_spoken_chars + len(out_sent) > max_spoken):
                            truncated_long = True
                            break
                        emitted_spoken_chars += len(out_sent)
                        yield out_sent

                if self._interrupt_event.is_set() or self.llm_cancel_event.is_set():
                    return
                tail = buf.flush()
                if tail and not truncated_long:
                    out_tail = _format_chunk(tail) if self.persona.enabled else tail
                    if out_tail:
                        if self.persona.enabled and (emitted_spoken_chars + len(out_tail) > max_spoken):
                            truncated_long = True
                        else:
                            yield out_tail

                if truncated_long and self.persona.enabled:
                    full_blob = "".join(collected_raw).strip()
                    styled_full = format_for_speech(
                        full_blob,
                        self.persona,
                        user_language=reply_lang,
                        workspace=self.workspace,
                        dispatch_full=lambda *_: (),
                    )
                    dispatched_routes, _rec = dispatch_long_output(
                        workspace=self.workspace,
                        full_text=full_blob,
                        summary=styled_full.spoken_text,
                        route=self.persona.spoken_style.long_output_route,
                        language=reply_lang,
                    )
                    hud_dispatched = bool(dispatched_routes)
                    offer = (
                        "বাকি বিস্তারিত ড্যাশবোর্ডে পাঠিয়েছি।"
                        if reply_lang.startswith("bn")
                        else "That's the key summary; I sent the full details to your HUD."
                    )
                    yield offer

            sentence_iter = _stream_sentences()

        self.duplex_state.paused_language = reply_lang
        spoken_sentences, interrupted = self._play_sentences(
            sentence_iter,
            language=reply_lang,
            metrics=metrics,
            t_turn_start=t_turn_start,
            barge_in_frames=barge_in_frames,
            on_start_speaking=_ensure_speaking_state,
        )

        full_response = "".join(collected_raw).strip()
        self.duplex_state.full_response_text = full_response
        if not interrupted:
            self.duplex_state.paused_sentences.clear()

        if self.conversation is not None:
            self.conversation.add_turn(
                transcript.text,
                full_response,
                language=reply_lang,
            )

        metrics.total_turn_ms = round((time.perf_counter() - t_turn_start) * 1000.0, 2)
        _record_state(VoiceState.IDLE, **metrics.to_dict())

        return VoiceTurnResult(
            transcript=transcript,
            response_text=full_response,
            spoken_sentences=spoken_sentences,
            states=turn_states,
            speaker_match=self.last_speaker_match,
            wake_event=wake_event,
            interrupted=interrupted,
            metrics=metrics,
            hud_dispatched=hud_dispatched,
            reply_language=reply_lang,
        )

    def _build_chief(self):
        from ..chief import ChiefOfStaff
        from ..notifications import NotificationRouter

        if self.chief_factory is not None:
            return self.chief_factory(self.workspace)
        return ChiefOfStaff(
            self.workspace,
            speak=lambda txt: self._speak_immediate(txt, self.conversation.last_language or "en"),
            notifications=NotificationRouter(self.workspace),
        )

    def start_chief_mission(
        self,
        goal: str,
        *,
        auto_approve: bool = False,
        source: str = "voice",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Plan and optionally start a Chief-of-Staff mission, speaking its summary."""
        from ..chief import ChiefOfStaff, save_mission

        chief = self._build_chief()
        mission = chief.plan(goal, source=source, **kwargs)
        summary = mission.plan_summary
        self._speak_immediate(summary, self.conversation.last_language or "en")
        can_start = auto_approve or ChiefOfStaff.can_auto_start(
            mission, chief.chief_config(), chief.autonomy_policy()
        )
        if can_start:
            mission = chief.start_background(mission.id, auto_approve=True)
            self._pending_chief_mission_id = None
        else:
            mission.status = "awaiting_approval"
            save_mission(self.workspace, mission)
            self._pending_chief_mission_id = mission.id
        return mission.to_dict()

    def _handle_chief_turn(self, text: str, *, language: str = "en") -> tuple[str | None, str | None, str | None]:
        from ..chief import (
            ChiefDisabledError,
            ChiefError,
            ChiefOfStaff,
            answer_status_question,
            detect_mission_control_intent,
            detect_mission_intent,
            detect_status_question_intent,
            list_missions,
            load_chief_config,
            save_mission,
        )

        normalized = (text or "").strip().lower().rstrip(".!?।")
        if self._pending_chief_mission_id:
            if normalized in {"yes", "go", "approve", "start", "ok", "okay", "sure", "do it", "হ্যাঁ", "শুরু করো"}:
                mid = self._pending_chief_mission_id
                self._pending_chief_mission_id = None
                try:
                    chief = self._build_chief()
                    mission = chief.approve(mid, approved=True, confirm="GO", background=True)
                    reply = "মিশন শুরু করছি।" if language == "bn" else f"Starting mission {mission.id}."
                    return reply, "chief_go", None
                except ChiefError as exc:
                    return f"Could not start mission: {exc}", "chief_error", str(exc)
            if normalized in {"no", "cancel", "stop", "abort", "nevermind", "না"}:
                mid = self._pending_chief_mission_id
                self._pending_chief_mission_id = None
                try:
                    chief = self._build_chief()
                    chief.approve(mid, approved=False)
                except ChiefError:
                    pass
                reply = "মিশন বাতিল করেছি।" if language == "bn" else "Mission cancelled."
                return reply, "chief_cancel", None

        control = detect_mission_control_intent(text)
        status_q = detect_status_question_intent(text) if control is None else None
        if status_q is not None:
            reply = answer_status_question(self.workspace, text)
            return reply, "chief_status", None

        goal = detect_mission_intent(text) if control is None else None
        if control is None and goal is None:
            return None, None, None

        if not load_chief_config(self.workspace).enabled and self.chief_factory is None:
            return (
                "Chief of Staff is disabled in chief.yaml. Set enabled: true to run missions.",
                "chief_disabled",
                "chief_disabled",
            )

        try:
            chief = self._build_chief()
            if control is not None:
                missions = list_missions(self.workspace, limit=10)
                if not missions:
                    return "No Chief of Staff missions found.", f"chief_{control}", None
                target = missions[0]
                if control == "status":
                    return (
                        answer_status_question(self.workspace, "what is the status?", mission_id=target.id),
                        "chief_status",
                        None,
                    )
                if control == "pause":
                    updated = chief.pause(target.id)
                    return f"Mission {updated.id} paused.", "chief_pause", None
                if control == "resume":
                    updated = chief.resume(target.id, background=True)
                    return f"Resuming mission {updated.id}.", "chief_resume", None
                if control == "cancel":
                    self._pending_chief_mission_id = None
                    updated = chief.cancel(target.id)
                    return f"Mission {updated.id} cancelled.", "chief_cancel", None

            assert goal is not None
            mission = chief.plan(goal, source="voice")
            summary = mission.plan_summary
            if ChiefOfStaff.can_auto_start(mission, chief.chief_config(), chief.autonomy_policy()):
                chief.start_background(mission.id, auto_approve=True)
                self._pending_chief_mission_id = None
            else:
                mission.status = "awaiting_approval"
                save_mission(self.workspace, mission)
                self._pending_chief_mission_id = mission.id
            return summary, "chief_plan", None
        except ChiefDisabledError as exc:
            return str(exc), "chief_disabled", "chief_disabled"
        except ChiefError as exc:
            return f"Chief of Staff error: {exc}", "chief_error", str(exc)

    def _build_proactive(self) -> Any:
        if self.proactive_factory is not None:
            return self.proactive_factory(self.workspace)
        from ..jarvis.proactive import ProactiveEngine

        return ProactiveEngine(self.workspace, clock=self.clock)

    def _handle_proactive_turn(self, text: str, *, language: str = "en") -> tuple[str | None, str | None, str | None]:
        from ..jarvis.proactive import detect_proactive_voice_intent, load_proactive_config

        detected = detect_proactive_voice_intent(text)
        if detected is None:
            return None, None, None
        intent, _ = detected
        if self.proactive_factory is None:
            try:
                cfg = load_proactive_config(self.workspace)
            except (ValueError, OSError):
                return None, None, None
            if not cfg.enabled:
                return None, None, None

        engine = self._build_proactive()
        try:
            res = engine.handle_voice_utterance(text, present=True, now=self.clock())
        except Exception as exc:  # noqa: BLE001
            return f"Proactive error: {exc}", "proactive_error", str(exc)
        if not res:
            return None, None, None
        reply = str(res.get("reply") or "").strip()
        if not reply:
            return None, None, None
        return reply, f"proactive_{res.get('intent', intent)}", None

    def _handle_memory_turn(self, text: str) -> tuple[str | None, str | None, str | None]:
        from ..memory import MemoryPolicyError, parse_memory_command

        ltm = getattr(self, "long_term_memory", None)
        if ltm is None:
            return None, None, None
        has_pending = bool(getattr(ltm, "_pending_forget_topic", None))
        if not has_pending and parse_memory_command(text) is None:
            return None, None, None
        try:
            res = ltm.handle_command(text, voice=True)
        except MemoryPolicyError as exc:
            return str(exc), "memory_blocked", str(exc)
        if not res:
            return None, None, None
        return str(res.get("reply") or ""), str(res.get("intent") or "memory"), None

    def run(
        self,
        *,
        handler: TextHandler | None = None,
        stop_event: threading.Event | None = None,
        max_turns: int | None = None,
        sd_module: Any = None,
    ) -> list[VoiceTurnResult]:
        """Run live microphone voice loop until ``stop_event`` is set or ``max_turns`` completed."""
        self._require_enabled()
        self._ensure_components()
        assert self.wake_detector is not None
        stop = stop_event or threading.Event()
        results: list[VoiceTurnResult] = []
        turns = 0
        with AudioInputStream(
            sample_rate=self.config.sample_rate,
            channels=self.config.channels,
            frame_samples=self.wake_detector.frame_length,
            device=self.config.input_device,
            sd_module=sd_module,
            stop_event=stop,
        ) as mic:
            while not stop.is_set():
                had_barge_prefix = bool(self._pending_barge_prefix)

                def _turn_frames() -> Iterator[bytes]:
                    if self._pending_barge_prefix:
                        prefix = self._pending_barge_prefix
                        self._pending_barge_prefix = b""
                        yield prefix
                    yield from mic.frames()

                barge_src = mic.frames() if self.config.duplex.enabled else None
                res = self.process_frames(
                    _turn_frames(),
                    skip_wake=had_barge_prefix,
                    handler=handler,
                    barge_in_frames=barge_src,
                )
                results.append(res)
                turns += 1
                if max_turns is not None and turns >= max_turns:
                    break
        return results


def build_agent_handler(
    workspace: Path | str,
    *,
    pipeline: VoicePipeline | None = None,
    read_only: bool = False,
    auto_yes: bool = False,
    approval_callback: Any = None,
    emit: EventHandler | None = None,
    stream: bool = True,
) -> TextHandler:
    """Create a streaming ``VoicePipeline`` text_handler backed by ``NexusAgent`` with persona & spoken approvals."""
    from ..agent import NexusAgent
    from ..cli import interactive_approval
    from ..config import load_config
    from ..tools import ApprovalDecision

    ws = Path(workspace).resolve()
    cfg = load_config(ws)

    def _safe_approval(spec: ToolSpec, args: dict[str, Any], risk: Risk, preview: str) -> ApprovalDecision:
        from ..autonomy import RiskClass, classify_action_risk

        is_danger = is_dangerous_action(spec, args) or classify_action_risk(spec, args) is RiskClass.DANGEROUS
        if pipeline is not None and is_danger:
            ok, reason = pipeline.authorize_action(Risk.DANGEROUS)
            if not ok:
                return ApprovalDecision(False, reason=reason)
        cb = approval_callback
        if cb is None and pipeline is not None and pipeline.config.conversation.spoken_approvals:
            cb = build_voice_approval_callback(pipeline.approval_manager)
        if cb is None:
            cb = interactive_approval
        return cb(spec, args, risk, preview)

    def _handle(transcript: TranscriptResult) -> Iterable[str] | str:
        from ..hud.presence import Presence
        presence = Presence(ws)
        shared_turn = None
        shared_history = None
        token_queue: queue.Queue[str | None] = queue.Queue()
        done_text: list[str] = []
        errors: list[BaseException] = []

        def _agent_emit(event: Event) -> None:
            if shared_turn:
                presence.agent_event(shared_turn, event.to_json())
            if emit is not None:
                emit(event)
            if event.type is EventType.TEXT_DELTA:
                piece = str(event.data.get("text") or "")
                if piece:
                    token_queue.put(piece)

        persona = pipeline.persona if pipeline is not None else load_persona_config(ws)
        extra_sys = ""
        if persona.enabled:
            extra_sys = build_persona_prompt(
                persona,
                user_language=transcript.language or "en",
                user_text=transcript.text,
            )
        if pipeline is not None and getattr(getattr(pipeline, "long_term_memory", None), "voice_privacy_mode", False):
            extra_sys += (
                "\nVOICE PRIVACY MODE ACTIVE: Do NOT read stored personal memories or user profile facts aloud."
            )

        agent = NexusAgent(
            cfg,
            workspace=ws,
            emit=_agent_emit,
            approval_callback=_safe_approval,
            auto_yes=auto_yes,
        )
        if pipeline is not None:
            agent.cancel_event = pipeline.llm_cancel_event
            gate_check = pipeline.make_approval_gate_check()
            if gate_check is not None:
                agent.gate.voice_speaker_check = gate_check

        from ..computer_workflows import voice_request
        command = voice_request(ws, transcript.text)
        if command is not None:
            # Only literal operator aliases; never interpret OCR, webpages, device
            # responses or learned routines as commands/permission changes.
            if read_only:
                agent.gate.mode = 'plan'
            _context, tools, _specs = agent.tools_for(agent.active_role, read_only=read_only)
            output, error = tools.call(*command)
            if error:
                raise VoiceError(output)
            return 'Reviewed routine completed.' if command[0] == 'workflow_run' else 'Reviewed device request sent. Physical state is not independently verified.'

        from ..vision.service import LOOK_REQUEST, SCREEN_REQUEST, VisionService
        if LOOK_REQUEST.fullmatch(transcript.text.strip()) or SCREEN_REQUEST.search(transcript.text):
            # A literal user vision request never sends its untrusted answer back
            # through agent tools. Preserve read-only and speaker checks on capture.
            if pipeline is not None and pipeline.config.tts.provider not in {'piper', 'kokoro'}:
                raise VoiceError('Vision answers require explicitly local Piper/Kokoro TTS; no automatic cloud speech')
            if read_only:
                agent.gate.mode = 'plan'
            observed = VisionService(ws).look(transcript.text, gate=agent.gate, channel='voice')
            if observed is not None:
                refs = observed.get('references', [])
                citations = ' References: ' + '; '.join(
                    f"page {r['page']}, region {r['region']}" for r in refs[:8]) if refs else ''
                return str(observed['answer']) + citations

        if presence.enabled('voice_enabled') and presence.enabled('conversation_enabled'):
            shared_history = presence.history()
            shared_turn = presence.begin(transcript.text, source='voice')
        kwargs = {'history': shared_history} if shared_history is not None else {}
        if not stream:
            try:
                result = agent.run(transcript.text, read_only=read_only, extra_system=extra_sys, **kwargs)
                final = result.text or result.summary or result.stopped_reason or "Done."
                if shared_turn:
                    presence.finish(shared_turn, final, ok=result.ok)
                return final
            except BaseException:
                if shared_turn:
                    presence.finish(shared_turn, ok=False)
                raise

        def _worker() -> None:
            try:
                result = agent.run(transcript.text, read_only=read_only, extra_system=extra_sys, **kwargs)
                if shared_turn:
                    presence.finish(shared_turn, result.text or result.summary, ok=result.ok)
                final = result.text or result.summary or result.stopped_reason or ""
                if final:
                    done_text.append(final)
            except BaseException as exc:  # pragma: no cover - surfaced to consumer
                if shared_turn:
                    presence.finish(shared_turn, ok=False)
                errors.append(exc)
            finally:
                token_queue.put(None)

        thread = threading.Thread(target=_worker, name="nexus-voice-agent", daemon=True)
        thread.start()

        def _tokens() -> Iterator[str]:
            saw_delta = False
            while True:
                item = token_queue.get()
                if item is None:
                    break
                saw_delta = True
                yield item
            thread.join(timeout=2.0)
            if errors:
                raise errors[0]
            if not saw_delta:
                if done_text:
                    yield done_text[0]
                else:
                    yield "Done."

        return _tokens()

    return _handle


__all__ = [
    "TextHandler",
    "VoicePipeline",
    "VoiceState",
    "VoiceStateEvent",
    "VoiceTurnResult",
    "build_agent_handler",
]
