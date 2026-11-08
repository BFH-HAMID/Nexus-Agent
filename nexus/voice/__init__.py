"""Opt-in modular voice core (full-duplex, adaptive EOT, streaming TTS, persona, conversation & approvals).

Importing ``nexus.voice`` never opens audio hardware, downloads model weights,
or performs network I/O. Voice stays off unless ``voice.yaml`` sets ``enabled: true``.
"""

from __future__ import annotations

from .approvals import (
    SpokenApprovalIntent,
    SpokenApprovalManager,
    parse_approval_intent,
)
from .config import (
    VoiceConfig,
    VoiceDependencyError,
    VoiceDisabledError,
    VoiceError,
    hud_state_path,
    load_voice_config,
    read_hud_state,
    require_voice_enabled,
    write_hud_state,
)
from .conversation import (
    AcknowledgementManager,
    ConversationState,
    ReferenceResolution,
)
from .duplex import (
    BargeInAction,
    FullDuplexMonitor,
    StreamingSentenceBuffer,
    TurnMetrics,
    classify_barge_in,
)
from .persona import (
    PersonaConfig,
    build_persona_prompt,
    dispatch_long_output,
    enforce_one_question,
    format_for_speech,
    load_persona_config,
    read_hud_messages,
    resolve_reply_language,
)
from .pipeline import VoicePipeline, VoiceState, VoiceStateEvent, VoiceTurnResult
from .realtime import (
    GeminiLiveAdapter,
    LiveKitAdapter,
    LocalPipelineRealtimeAdapter,
    PipecatAdapter,
    RealtimeVoiceAdapter,
    create_realtime_adapter,
)
from .turn_detection import (
    AdaptiveSilenceTracker,
    SemanticTurnDetector,
    TurnTakingSegmenter,
)

__all__ = [
    "AcknowledgementManager",
    "AdaptiveSilenceTracker",
    "BargeInAction",
    "ConversationState",
    "FullDuplexMonitor",
    "GeminiLiveAdapter",
    "LiveKitAdapter",
    "LocalPipelineRealtimeAdapter",
    "PersonaConfig",
    "PipecatAdapter",
    "RealtimeVoiceAdapter",
    "ReferenceResolution",
    "SemanticTurnDetector",
    "SpokenApprovalIntent",
    "SpokenApprovalManager",
    "StreamingSentenceBuffer",
    "TurnMetrics",
    "TurnTakingSegmenter",
    "VoiceConfig",
    "VoiceDependencyError",
    "VoiceDisabledError",
    "VoiceError",
    "VoicePipeline",
    "VoiceState",
    "VoiceStateEvent",
    "VoiceTurnResult",
    "build_persona_prompt",
    "classify_barge_in",
    "create_realtime_adapter",
    "dispatch_long_output",
    "enforce_one_question",
    "format_for_speech",
    "hud_state_path",
    "load_persona_config",
    "load_voice_config",
    "parse_approval_intent",
    "read_hud_messages",
    "read_hud_state",
    "require_voice_enabled",
    "resolve_reply_language",
    "write_hud_state",
]
