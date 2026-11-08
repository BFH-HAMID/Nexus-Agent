"""Optional realtime voice adapters behind one interface; local pipeline remains default."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .config import VoiceConfig, VoiceDependencyError, VoiceError


@dataclass(frozen=True)
class RealtimeConfig:
    enabled: bool = False
    provider: str = "local"  # local | gemini-live | livekit | pipecat
    model: str = "gemini-2.0-flash-live-001"
    api_key_env: str = "GEMINI_API_KEY"
    livekit_url_env: str = "LIVEKIT_URL"
    livekit_api_key_env: str = "LIVEKIT_API_KEY"
    livekit_api_secret_env: str = "LIVEKIT_API_SECRET"
    sample_rate: int = 16_000


@dataclass(frozen=True)
class RealtimeEvent:
    kind: str  # connected | transcript_partial | transcript_final | text_delta | audio_delta | interrupted | tool_call | turn_complete | error
    text: str = ""
    audio: bytes = b""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class RealtimeVoiceAdapter(ABC):
    """Unified bidirectional voice session interface."""

    name: str = "realtime"

    @abstractmethod
    def connect(self) -> None:
        """Open the realtime session."""

    @abstractmethod
    def send_audio(self, pcm16: bytes) -> None:
        """Stream 16 kHz mono PCM16 audio frames into the session."""

    @abstractmethod
    def send_text(self, text: str) -> None:
        """Send a user text turn or tool response into the session."""

    @abstractmethod
    def interrupt(self) -> None:
        """Barge-in: cancel in-flight model generation and audio output."""

    @abstractmethod
    def receive_events(self) -> Iterator[RealtimeEvent]:
        """Yield normalized session events."""

    @abstractmethod
    def close(self) -> None:
        """Close the realtime session."""


class LocalPipelineRealtimeAdapter(RealtimeVoiceAdapter):
    """Default adapter delegating to Nexus's local VoicePipeline (no cloud realtime required)."""

    name = "local"

    def __init__(self, pipeline: Any, *, handler: Any = None) -> None:
        self.pipeline = pipeline
        self.handler = handler
        self._connected = False
        self._buffer = bytearray()
        self._events: list[RealtimeEvent] = []

    def connect(self) -> None:
        self._connected = True
        self._events.append(RealtimeEvent(kind="connected", metadata={"adapter": self.name}))

    def send_audio(self, pcm16: bytes) -> None:
        if not self._connected:
            raise VoiceError("Local realtime adapter is not connected")
        self._buffer.extend(pcm16)

    def send_text(self, text: str) -> None:
        if not self._connected:
            raise VoiceError("Local realtime adapter is not connected")
        from .stt import TranscriptResult

        handler = self.handler or self.pipeline.text_handler
        if handler is None:
            raise VoiceError("Local realtime adapter requires a text handler")
        reply = handler(TranscriptResult(text=text, language="en"))
        if isinstance(reply, str):
            self._events.append(RealtimeEvent(kind="text_delta", text=reply))
        else:
            for chunk in reply:
                self._events.append(RealtimeEvent(kind="text_delta", text=str(chunk)))
        self._events.append(RealtimeEvent(kind="turn_complete"))

    def flush_audio(self) -> None:
        if not self._buffer:
            return
        frames = [bytes(self._buffer)]
        self._buffer.clear()
        res = self.pipeline.process_frames(frames, skip_wake=True, handler=self.handler)
        if res.transcript:
            self._events.append(RealtimeEvent(kind="transcript_final", text=res.transcript.text))
        if res.response_text:
            self._events.append(RealtimeEvent(kind="text_delta", text=res.response_text))
        self._events.append(RealtimeEvent(kind="turn_complete"))

    def interrupt(self) -> None:
        self.pipeline.interrupt_speech()
        self._events.append(RealtimeEvent(kind="interrupted"))

    def receive_events(self) -> Iterator[RealtimeEvent]:
        while self._events:
            yield self._events.pop(0)

    def close(self) -> None:
        self._buffer.clear()
        self._connected = False


def _resolve_env_or_key(env_name: str, family: str = "") -> str:
    val = os.environ.get(env_name, "").strip()
    if val:
        return val
    if family:
        try:
            from ..keys import get_key

            stored = get_key(family)
            if stored:
                return stored.strip()
        except Exception:
            pass
    return ""


class GeminiLiveAdapter(RealtimeVoiceAdapter):
    """Optional Gemini Multimodal Live API adapter (requires `.[voice-realtime]`)."""

    name = "gemini-live"

    def __init__(
        self,
        *,
        model: str = "gemini-2.0-flash-live-001",
        api_key: str = "",
        api_key_env: str = "GEMINI_API_KEY",
        sample_rate: int = 16_000,
        session_factory: Any = None,
    ) -> None:
        self.model = (model or "gemini-2.0-flash-live-001").strip()
        self.api_key = (api_key or _resolve_env_or_key(api_key_env, family="gemini")).strip()
        self.api_key_env = api_key_env
        self.sample_rate = int(sample_rate)
        self._session_factory = session_factory
        self._session: Any = None
        self._connected = False
        self._events: list[RealtimeEvent] = []

    def _ensure_deps(self):
        if self._session_factory is not None:
            return self._session_factory
        try:
            from google import genai
        except ImportError as exc:
            raise VoiceDependencyError('Gemini Live adapter requires pip install -e ".[voice-realtime]"') from exc
        if not self.api_key:
            raise VoiceError(f"Gemini Live adapter requires API key in {self.api_key_env} or `nexus keys set gemini`")
        client = genai.Client(api_key=self.api_key)
        return client.aio.live.connect

    def connect(self) -> None:
        factory = self._ensure_deps()
        if not self.api_key and self._session_factory is None:
            raise VoiceError(f"Gemini Live adapter requires API key in {self.api_key_env}")
        self._session = factory(model=self.model)
        self._connected = True
        self._events.append(RealtimeEvent(kind="connected", metadata={"model": self.model}))

    def send_audio(self, pcm16: bytes) -> None:
        if not self._connected or self._session is None:
            raise VoiceError("Gemini Live session is not connected")
        self._session.send_realtime_input(data=pcm16, mime_type=f"audio/pcm;rate={self.sample_rate}")

    def send_text(self, text: str) -> None:
        if not self._connected or self._session is None:
            raise VoiceError("Gemini Live session is not connected")
        self._session.send(input=text, end_of_turn=True)

    def interrupt(self) -> None:
        if self._session is not None and hasattr(self._session, "interrupt"):
            self._session.interrupt()
        self._events.append(RealtimeEvent(kind="interrupted"))

    def receive_events(self) -> Iterator[RealtimeEvent]:
        while self._events:
            yield self._events.pop(0)
        if self._session is not None and hasattr(self._session, "receive_sync"):
            for raw in self._session.receive_sync():
                yield _normalize_realtime_payload(raw)

    def close(self) -> None:
        if self._session is not None and hasattr(self._session, "close"):
            self._session.close()
        self._session = None
        self._connected = False


class LiveKitAdapter(RealtimeVoiceAdapter):
    """Optional LiveKit Agents realtime adapter (requires `.[voice-livekit]`)."""

    name = "livekit"

    def __init__(
        self,
        *,
        url: str = "",
        api_key: str = "",
        api_secret: str = "",
        url_env: str = "LIVEKIT_URL",
        api_key_env: str = "LIVEKIT_API_KEY",
        api_secret_env: str = "LIVEKIT_API_SECRET",
        room_name: str = "nexus-voice",
        room_factory: Any = None,
    ) -> None:
        self.url = (url or os.environ.get(url_env, "")).strip()
        self.api_key = (api_key or os.environ.get(api_key_env, "")).strip()
        self.api_secret = (api_secret or os.environ.get(api_secret_env, "")).strip()
        self.room_name = room_name
        self._room_factory = room_factory
        self._room: Any = None
        self._events: list[RealtimeEvent] = []

    def _ensure_deps(self):
        if self._room_factory is not None:
            return self._room_factory
        try:
            import livekit.agents  # noqa: F401
        except ImportError as exc:
            raise VoiceDependencyError('LiveKit adapter requires pip install -e ".[voice-livekit]"') from exc
        if not (self.url and self.api_key and self.api_secret):
            raise VoiceError("LiveKit adapter requires LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET env vars")
        return None

    def connect(self) -> None:
        factory = self._ensure_deps()
        if factory is not None:
            self._room = factory(url=self.url, room=self.room_name)
        self._events.append(RealtimeEvent(kind="connected", metadata={"room": self.room_name}))

    def send_audio(self, pcm16: bytes) -> None:
        if self._room is None:
            raise VoiceError("LiveKit room is not connected")
        self._room.publish_audio(pcm16)

    def send_text(self, text: str) -> None:
        if self._room is None:
            raise VoiceError("LiveKit room is not connected")
        self._room.publish_data(text)

    def interrupt(self) -> None:
        if self._room is not None and hasattr(self._room, "interrupt"):
            self._room.interrupt()
        self._events.append(RealtimeEvent(kind="interrupted"))

    def receive_events(self) -> Iterator[RealtimeEvent]:
        while self._events:
            yield self._events.pop(0)
        if self._room is not None and hasattr(self._room, "drain_events"):
            for raw in self._room.drain_events():
                yield _normalize_realtime_payload(raw)

    def close(self) -> None:
        if self._room is not None and hasattr(self._room, "disconnect"):
            self._room.disconnect()
        self._room = None


class PipecatAdapter(RealtimeVoiceAdapter):
    """Optional Pipecat pipeline frame adapter (requires `.[voice-pipecat]`)."""

    name = "pipecat"

    def __init__(self, *, runner_factory: Any = None) -> None:
        self._runner_factory = runner_factory
        self._runner: Any = None
        self._events: list[RealtimeEvent] = []

    def _ensure_deps(self):
        if self._runner_factory is not None:
            return self._runner_factory
        try:
            import pipecat  # noqa: F401
        except ImportError as exc:
            raise VoiceDependencyError('Pipecat adapter requires pip install -e ".[voice-pipecat]"') from exc
        return None

    def connect(self) -> None:
        factory = self._ensure_deps()
        if factory is not None:
            self._runner = factory()
        self._events.append(RealtimeEvent(kind="connected", metadata={"adapter": self.name}))

    def send_audio(self, pcm16: bytes) -> None:
        if self._runner is None:
            raise VoiceError("Pipecat runner is not connected")
        self._runner.push_frame({"type": "audio", "data": pcm16})

    def send_text(self, text: str) -> None:
        if self._runner is None:
            raise VoiceError("Pipecat runner is not connected")
        self._runner.push_frame({"type": "text", "text": text})

    def interrupt(self) -> None:
        if self._runner is not None:
            self._runner.push_frame({"type": "cancel"})
        self._events.append(RealtimeEvent(kind="interrupted"))

    def receive_events(self) -> Iterator[RealtimeEvent]:
        while self._events:
            yield self._events.pop(0)
        if self._runner is not None and hasattr(self._runner, "pop_events"):
            for raw in self._runner.pop_events():
                yield _normalize_realtime_payload(raw)

    def close(self) -> None:
        if self._runner is not None and hasattr(self._runner, "close"):
            self._runner.close()
        self._runner = None


def _normalize_realtime_payload(raw: Any) -> RealtimeEvent:
    if isinstance(raw, RealtimeEvent):
        return raw
    if isinstance(raw, dict):
        return RealtimeEvent(
            kind=str(raw.get("kind") or raw.get("type") or "text_delta"),
            text=str(raw.get("text") or ""),
            audio=bytes(raw.get("audio") or b""),
            tool_name=str(raw.get("tool_name") or ""),
            tool_args=dict(raw.get("tool_args") or {}),
            metadata={k: v for k, v in raw.items() if k not in {"kind", "type", "text", "audio", "tool_name", "tool_args"}},
        )
    return RealtimeEvent(kind="text_delta", text=str(raw))


def create_realtime_adapter(
    config: VoiceConfig | RealtimeConfig,
    *,
    pipeline: Any = None,
    handler: Any = None,
) -> RealtimeVoiceAdapter:
    rt = config.realtime if isinstance(config, VoiceConfig) else config
    if not rt.enabled or rt.provider == "local":
        return LocalPipelineRealtimeAdapter(pipeline, handler=handler)
    if rt.provider == "gemini-live":
        return GeminiLiveAdapter(
            model=rt.model,
            api_key_env=rt.api_key_env,
            sample_rate=rt.sample_rate,
        )
    if rt.provider == "livekit":
        return LiveKitAdapter(
            url_env=rt.livekit_url_env,
            api_key_env=rt.livekit_api_key_env,
            api_secret_env=rt.livekit_api_secret_env,
        )
    if rt.provider == "pipecat":
        return PipecatAdapter()
    raise VoiceError(f"Unsupported realtime voice provider: {rt.provider}")


__all__ = [
    "GeminiLiveAdapter",
    "LiveKitAdapter",
    "LocalPipelineRealtimeAdapter",
    "PipecatAdapter",
    "RealtimeConfig",
    "RealtimeEvent",
    "RealtimeVoiceAdapter",
    "create_realtime_adapter",
]
