"""One explicit per-server assistant service; no import-time hardware or jobs."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .common import AssistantError
from .personal_data import ExternalActions, GoogleWorkspace, NativeVault
from .proactive import BriefSchedule, MonitorStore, MorningBrief, ProactiveWorker, ReminderStore
from .voice import AudioPlayer, VoiceApproval


def _enabled(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def tts_for(state, engine: str | None = None):
    """Build only the explicitly selected adapter; online engines are named."""
    from .voice import EdgeTTS, ElevenLabsTTS, KokoroTTS, PiperTTS

    selected = engine or os.environ.get("NEXUS_TTS_ENGINE", "piper")
    if selected == "piper":
        model = os.environ.get("NEXUS_PIPER_MODEL", "")
        if not model:
            raise AssistantError("Set NEXUS_PIPER_MODEL to a local Piper .onnx voice")
        return PiperTTS(model, executable=os.environ.get("NEXUS_PIPER_BIN", "piper"))
    if selected == "kokoro":
        return KokoroTTS(voice=os.environ.get("NEXUS_KOKORO_VOICE", "af_heart"))
    if selected == "edge":
        return EdgeTTS(voice=os.environ.get("NEXUS_EDGE_VOICE", "bn-BD-NabanitaNeural"))
    if selected == "elevenlabs":
        voice_id = os.environ.get("NEXUS_ELEVENLABS_VOICE", "")
        if not voice_id:
            raise AssistantError("Set NEXUS_ELEVENLABS_VOICE explicitly")
        return ElevenLabsTTS(voice_id, NativeVault())
    raise AssistantError("TTS engine must be piper, kokoro, edge or elevenlabs")


class AssistantService:
    """Shared state, worker lifecycle and constructors for CLI and web surfaces."""

    def __init__(self, state):
        self.state = state
        self.workspace = Path(state.workspace)
        self.external = ExternalActions()
        self.voice_approval = VoiceApproval()
        self.player = AudioPlayer()
        self.worker: ProactiveWorker | None = None

    @property
    def memory(self):
        from .memory import PersonalMemory
        return PersonalMemory(self.workspace)

    @property
    def reminders(self) -> ReminderStore:
        return ReminderStore(self.workspace)

    @property
    def monitors(self) -> MonitorStore:
        return MonitorStore(self.workspace)

    @property
    def brief_schedule(self) -> BriefSchedule:
        return BriefSchedule(self.workspace)

    def google(self) -> GoogleWorkspace:
        return GoogleWorkspace(NativeVault())

    def morning_brief(self, *, summarize: bool = False, agent: str | None = None) -> dict[str, Any]:
        world = None
        if _enabled("NEXUS_WORLD_ENABLED", True):
            world = self.state.gui.world()
        google = self.google() if _enabled("NEXUS_GOOGLE_ENABLED") else None
        briefer = MorningBrief(google=google, world=world)
        brief = briefer.build()
        if summarize:
            from ..providers import build_providers
            from .personal_data import summarize as summarize_documents
            providers = build_providers(self.state.config.agents)
            documents = briefer.documents(brief)
            if not documents:
                brief["summary"] = {"state": "unavailable", "reason": "No enabled source produced evidence"}
            else:
                brief["summary"] = summarize_documents(self.state.config, providers, documents,
                                                       question="Write a concise morning brief in the user's preferred language.",
                                                       agent=agent)
        return brief

    def _voice_notice(self, text: str) -> None:
        clip = tts_for(self.state).synthesize(text)
        self.player.play(clip, None)

    def start_worker(self, *, interval: float = 30) -> dict[str, Any]:
        if self.worker and self.worker.thread and self.worker.thread.is_alive():
            return {"running": True, "already_running": True}
        voice = self._voice_notice if _enabled("NEXUS_NOTIFY_VOICE") else None
        self.worker = ProactiveWorker(self.state, brief_builder=self.morning_brief,
                                      voice_deliverer=voice, interval=interval)
        self.worker.start()
        return {"running": True, "interval_seconds": self.worker.interval,
                "voice_notifications": bool(voice)}

    def stop_worker(self) -> dict[str, Any]:
        if self.worker:
            self.worker.stop()
        return {"running": False}

    def pass_once(self) -> dict[str, Any]:
        voice = self._voice_notice if _enabled("NEXUS_NOTIFY_VOICE") else None
        worker = ProactiveWorker(self.state, brief_builder=self.morning_brief,
                                 voice_deliverer=voice)
        return worker.pass_once()


def service(state) -> AssistantService:
    current = getattr(state, "assistant", None)
    if current is None:
        current = AssistantService(state)
        state.assistant = current
    return current
