"""Authenticated dashboard API for the opt-in personal assistant surfaces."""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import securitycenter
from ..providers import build_providers
from . import capabilities
from .common import AssistantError, clean_text
from .perception import VisionQuestioner, save_upload
from .personal_data import GoogleWorkspace, NotionNotes, ObsidianNotes
from .reasoning import ReasoningLoop, WebResearcher
from .service import service, tts_for
from .voice import FasterWhisperSTT, SpeakerVerifier

MAX_BODY = 36_000_000
LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost"}


def _safe(value: Any) -> Any:
    from .common import UntrustedDocument
    if isinstance(value, UntrustedDocument):
        return {"source": value.source, "text": value.text, "metadata": _safe(value.metadata),
                "injection_flags": value.injection_flags, "authority": "untrusted-data"}
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _field(payload: dict[str, Any], key: str, limit: int = 4000, *, lines: bool = False) -> str:
    """Optional JSON text fields are absent/empty, never Python ``None``."""
    value = payload.get(key)
    if value is None:
        return ""
    if isinstance(value, str) and not value.strip():
        return ""
    return clean_text(value, limit, lines=lines)


def _confirm(payload: dict[str, Any], phrase: str) -> None:
    if payload.get("confirmation") != phrase:
        raise AssistantError(f'Type exactly "{phrase}" to confirm')


def _audit(state, action: str, client: str, **detail: Any) -> None:
    securitycenter.audit_action(state, action, client, {key: value for key, value in detail.items()
                                                       if key not in {"text", "body", "audio", "token", "raw"}})


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _audio(payload: dict[str, Any]) -> bytes:
    try:
        data = base64.b64decode(str(payload.get("audio_base64") or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AssistantError("audio_base64 must be valid base64") from exc
    if not data or len(data) > 25_000_000:
        raise AssistantError("Audio must contain 1–25,000,000 decoded bytes")
    return data


def _providers(state):
    current = getattr(state, "assistant_providers", None)
    if current is None:
        current = build_providers(state.config.agents)
        state.assistant_providers = current
    return current


def overview(state, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    assistant = service(state)
    since = 0
    if query and query.get("since"):
        try:
            since = int(query["since"][0])
        except (ValueError, IndexError):
            since = 0
    current_revision = assistant.memory.revision
    changed = not since or current_revision > since
    memory = assistant.memory.view() if changed else {"revision": current_revision, "unchanged": True}
    return {"capabilities": capabilities(), "memory": memory,
            "memory_changed": changed,
            "reminders": assistant.reminders.list(), "monitors": assistant.monitors.list(),
            "brief_schedule": assistant.brief_schedule.get(),
            "sync": {"cursor": current_revision, "transport": "authenticated polling; no cloud sync",
                     "scope": "memory revision and local reminder/monitor definitions"},
            "scheduler": {"running": bool(assistant.worker and assistant.worker.thread
                                          and assistant.worker.thread.is_alive()),
                          "opt_in_env": "NEXUS_ASSISTANT_SCHEDULER=1"},
            "browser_automation_default": False}


def get(state, path: str, query: dict[str, list[str]]) -> dict[str, Any]:
    assistant = service(state)
    if path == "overview":
        return _safe(overview(state, query))
    if path == "capabilities":
        return capabilities()
    if path == "memory":
        query_text = (query.get("q") or [""])[0]
        return _safe(assistant.memory.view(query_text))
    if path == "reminders":
        return {"reminders": assistant.reminders.list()}
    if path == "monitors":
        return {"monitors": assistant.monitors.list()}
    raise AssistantError("Unknown assistant GET route")


def post(state, path: str, payload: dict[str, Any], client: str = "") -> dict[str, Any]:
    if state.demo:
        raise AssistantError("Assistant actions require live mode, not --demo")
    # Record intent before any mutation or external call; success is recorded
    # separately below. An unwritable audit store therefore fails closed.
    securitycenter.append_event(state.workspace, {"at": __import__("time").time(), "actor": "user",
                                "action": "assistant." + path[:120], "target": "dashboard",
                                "result": "requested", "risk": "high", "detail": {"client": client}})
    assistant = service(state)
    if path == "memory/enable":
        enabled = payload.get("enabled") is True
        _confirm(payload, "SAVE PERSONAL MEMORY" if enabled else "DISABLE PERSONAL MEMORY")
        result = assistant.memory.set_enabled(enabled)
        _audit(state, "assistant.memory.enable", client, enabled=enabled)
        return result
    if path == "memory/entry":
        ident = _field(payload, "id", 80)
        text = _field(payload, "text", 12_000, lines=True)
        if ident:
            _confirm(payload, f"EDIT PERSONAL MEMORY {ident}")
            result = assistant.memory.edit(ident, text, kind=payload.get("kind"))
            action = "assistant.memory.edit"
        else:
            _confirm(payload, "ADD PERSONAL MEMORY")
            result = assistant.memory.remember(text, kind=str(payload.get("kind") or "note"))
            action = "assistant.memory.add"
        _audit(state, action, client, id=ident or result["id"])
        return _safe(result)
    if path == "memory/profile":
        key = _field(payload, "key", 100)
        value = _field(payload, "value", 2000, lines=True)
        _confirm(payload, "SAVE PERSONAL PROFILE")
        result = assistant.memory.set_profile(key, value)
        _audit(state, "assistant.memory.profile", client, key=key, value_sha256=hashlib.sha256(value.encode()).hexdigest())
        return result
    if path == "memory/context":
        task = _field(payload, "task", 4000, lines=True)
        if task:
            _confirm(payload, "SAVE ACTIVE CONTEXT")
            result = assistant.memory.set_active(task, {"source": "dashboard"})
        else:
            _confirm(payload, "CLEAR ACTIVE CONTEXT")
            result = assistant.memory.clear_active()
        _audit(state, "assistant.memory.context", client, active=bool(task))
        return result
    if path == "memory/forget":
        if payload.get("all") is True:
            _confirm(payload, "FORGET ALL PERSONAL MEMORY")
            result = {"forgotten": assistant.memory.forget_all("FORGET ALL PERSONAL MEMORY")}
        else:
            ident = _field(payload, "id", 80)
            _confirm(payload, f"FORGET PERSONAL MEMORY {ident}")
            result = {"forgotten": int(assistant.memory.forget(ident))}
        _audit(state, "assistant.memory.forget", client, all=payload.get("all") is True)
        return result
    if path == "reminders/add":
        _confirm(payload, "ADD REMINDER")
        result = assistant.reminders.add(_field(payload, "text", 1000, lines=True),
                                         run_at=payload.get("run_at"), every_minutes=payload.get("every_minutes"),
                                         daily=payload.get("daily"), weekly=payload.get("weekly"),
                                         timezone=str(payload.get("timezone") or "UTC"))
        _audit(state, "assistant.reminder.add", client, id=result["id"])
        return result
    if path == "reminders/cancel":
        ident = _field(payload, "id", 80)
        _confirm(payload, f"CANCEL REMINDER {ident}")
        result = {"cancelled": assistant.reminders.cancel(ident)}
        _audit(state, "assistant.reminder.cancel", client, id=ident)
        return result
    if path == "monitors/add":
        _confirm(payload, "ADD MONITOR")
        options = payload.get("options")
        if not isinstance(options, dict):
            raise AssistantError("Monitor options must be an object")
        result = assistant.monitors.add(str(payload.get("kind") or ""), options,
                                        every_minutes=float(payload.get("every_minutes") or 5))
        _audit(state, "assistant.monitor.add", client, id=result["id"], kind=result["kind"])
        return _safe(result)
    if path == "monitors/remove":
        ident = _field(payload, "id", 80)
        _confirm(payload, f"REMOVE MONITOR {ident}")
        result = {"removed": assistant.monitors.remove(ident)}
        _audit(state, "assistant.monitor.remove", client, id=ident)
        return result
    if path == "brief/schedule":
        enabled = payload.get("enabled") is True
        _confirm(payload, "SCHEDULE MORNING BRIEF" if enabled else "DISABLE MORNING BRIEF")
        result = assistant.brief_schedule.save(enabled=enabled, at=str(payload.get("time") or "08:00"),
                                               timezone=str(payload.get("timezone") or "UTC"))
        _audit(state, "assistant.brief.schedule", client, enabled=enabled)
        return result
    if path == "brief/build":
        summarize = payload.get("summarize") is True
        _confirm(payload, "SUMMARIZE MORNING BRIEF" if summarize else "BUILD MORNING BRIEF")
        if summarize:
            from .. import costcenter
            costcenter.enforce(state.workspace)
        result = assistant.morning_brief(summarize=summarize, agent=payload.get("agent"))
        _audit(state, "assistant.brief.build", client, summarized=summarize)
        return _safe(result)
    if path == "scheduler/pass":
        _confirm(payload, "RUN ASSISTANT SCHEDULER")
        result = assistant.pass_once()
        _audit(state, "assistant.scheduler.pass", client)
        return _safe(result)
    if path.startswith("data/"):
        return _personal_data(state, assistant, path.removeprefix("data/"), payload, client)
    if path.startswith("email/"):
        return _email(state, assistant, path.removeprefix("email/"), payload, client)
    if path == "voice/email":
        return _voice_email(state, assistant, payload, client)
    if path == "voice/transcribe":
        _confirm(payload, "TRANSCRIBE AUDIO")
        data = _audio(payload)
        model = str(payload.get("model") or os.environ.get("NEXUS_WHISPER_MODEL") or "small")
        with tempfile.TemporaryDirectory(prefix="nexus-stt-") as directory:
            source = Path(directory) / "audio.wav"
            source.write_bytes(data)
            result = FasterWhisperSTT(model).transcribe(source)
        _audit(state, "assistant.voice.transcribe", client, bytes=len(data), language=result.language)
        return {"transcript": result.text, "language": result.language,
                "verified_speaker": False, "authority": "draft text only; never an approval"}
    if path == "voice/verify":
        _confirm(payload, "VERIFY MY VOICE")
        data = _audio(payload)
        with tempfile.TemporaryDirectory(prefix="nexus-voice-") as directory:
            source = Path(directory) / "audio.wav"
            source.write_bytes(data)
            result = SpeakerVerifier(state.workspace).verify(source)
        _audit(state, "assistant.voice.verify", client, matched=result["matched"])
        return {**result, "authority": "A standalone score is not authorization; use an action-bound challenge."}
    if path == "voice/speak":
        engine = str(payload.get("engine") or "piper")
        online = engine in {"edge", "elevenlabs"}
        _confirm(payload, "UPLOAD TEXT TO TTS" if online else "SPEAK TEXT")
        text = _field(payload, "text", 8000, lines=True)
        clip = tts_for(state, engine).synthesize(text)
        _audit(state, "assistant.voice.speak", client, engine=engine, characters=len(text), online=online)
        return {"audio_base64": base64.b64encode(clip.data).decode(), "format": clip.format,
                "online": online, "characters": len(text)}
    if path == "vision/ask":
        data = _audio(payload) if payload.get("audio_base64") else _upload_bytes(payload)
        question = _field(payload, "question", 3000, lines=True)
        agent_name = str(payload.get("agent") or state.config.default_agent)
        if agent_name not in state.config.agents:
            raise AssistantError("Unknown vision agent")
        from ..context.core import local_agent
        local = local_agent(state.config.agents[agent_name])
        _confirm(payload, "ANALYZE UPLOAD" if local else "UPLOAD DOCUMENT TO CLOUD")
        from .. import costcenter
        costcenter.enforce(state.workspace)
        saved = save_upload(state.workspace, str(payload.get("name") or "upload.txt"), data)
        result = VisionQuestioner(state.config, _providers(state)).ask(saved, question, agent=agent_name)
        _audit(state, "assistant.vision.ask", client, agent=agent_name, local=local, bytes=len(data))
        return _safe({**result, "local": local, "upload_name": saved.name})
    if path == "research":
        _confirm(payload, "RESEARCH WEB")
        urls = payload.get("urls")
        domains = payload.get("allowed_domains")
        if not isinstance(urls, list) or not isinstance(domains, list):
            raise AssistantError("Explicit urls and allowed_domains lists are required")
        from .. import costcenter
        costcenter.enforce(state.workspace)
        result = WebResearcher(state.config, _providers(state),
                               allowed_domains=[str(row) for row in domains]).research(
                                   _field(payload, "question", 3000, lines=True),
                                   urls=[str(row) for row in urls], agent=payload.get("agent"))
        _audit(state, "assistant.research", client, sources=len(result["sources"]))
        return _safe(result)
    if path == "reasoning/run":
        _confirm(payload, "RUN READ-ONLY REASONING")
        from .. import costcenter
        from ..agent import NexusAgent
        costcenter.enforce(state.workspace)
        providers = _providers(state)

        def execute(task, plan, feedback):
            instruction = task + ("\nCritic repair: " + feedback if feedback else "")
            result = NexusAgent(state.config, workspace=state.workspace, providers=providers,
                                emit=state.mission.emit, auto_yes=False).run(instruction, read_only=True)
            return {"text": result.text, "ok": result.ok, "evidence": result.stopped_reason}
        result = ReasoningLoop(state.config, providers, execute,
                               max_repairs=int(payload.get("max_repairs") or 1)).run(
                                   _field(payload, "task", 8000, lines=True), agent=payload.get("agent"))
        _audit(state, "assistant.reasoning.run", client, accepted=result["accepted"], repairs=result["repairs"])
        return _safe(result)
    if path == "automation/backup":
        _confirm(payload, "CREATE BACKUP")
        from .automation import BackupManager
        result = BackupManager(state.workspace).create()
        _audit(state, "assistant.backup.create", client, id=result["id"], files=result["files"])
        return result
    if path == "automation/restore":
        ident = _field(payload, "id", 100)
        target = _field(payload, "target", 500)
        _confirm(payload, f"RESTORE BACKUP {ident}")
        from .automation import BackupManager
        result = BackupManager(state.workspace).restore(ident, target, confirmation=f"RESTORE BACKUP {ident}")
        _audit(state, "assistant.backup.restore", client, id=ident)
        return result
    if path == "automation/desktop":
        if not _enabled("NEXUS_DESKTOP_ENABLED"):
            raise AssistantError("Desktop control is disabled; set NEXUS_DESKTOP_ENABLED=1 explicitly")
        action = _field(payload, "action", 40)
        args = payload.get("args")
        if not isinstance(args, dict):
            raise AssistantError("Desktop args must be an object")
        _confirm(payload, f"APPROVE DESKTOP {action}")
        from .automation import DesktopController
        controller = DesktopController(backend=str(payload.get("backend") or "auto"),
                                       review=lambda _action, _args: True)
        result = controller.execute(action, args)
        _audit(state, "assistant.desktop.execute", client, action=action)
        return result
    raise AssistantError("Unknown assistant action")


def _upload_bytes(payload: dict[str, Any]) -> bytes:
    try:
        data = base64.b64decode(str(payload.get("file_base64") or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AssistantError("file_base64 must be valid base64") from exc
    if not data or len(data) > 10_000_000:
        raise AssistantError("Upload must contain 1–10,000,000 decoded bytes")
    return data


def _personal_data(state, assistant, action: str, payload: dict[str, Any], client: str) -> dict[str, Any]:
    _confirm(payload, "READ PERSONAL DATA")
    limit = min(50, max(1, int(payload.get("limit") or 10)))
    google = assistant.google()
    if action == "gmail":
        result = google.gmail(query=str(payload.get("query") or "newer_than:1d"), limit=min(20, limit))
    elif action == "calendar":
        now = datetime.now().astimezone()
        result = google.calendar(time_min=str(payload.get("time_min") or now.isoformat()),
                                 time_max=str(payload.get("time_max") or (now + timedelta(days=1)).isoformat()), limit=limit)
    elif action == "contacts":
        result = google.contacts(limit=limit)
    elif action == "drive":
        result = google.drive(query=str(payload.get("query") or ""), limit=limit)
    elif action == "drive/read":
        result = google.drive_read(_field(payload, "file_id", 200),
                                   mime_type=str(payload.get("mime_type") or ""), name=str(payload.get("name") or ""))
    elif action == "notion":
        result = NotionNotes(google.vault).search(_field(payload, "query", 500), limit=limit)
    elif action == "obsidian":
        root = Path(str(payload.get("vault") or os.environ.get("NEXUS_OBSIDIAN_VAULT") or "")).expanduser()
        result = ObsidianNotes(root).search(_field(payload, "query", 500), limit=limit)
    else:
        raise AssistantError("Unknown personal data source")
    _audit(state, f"assistant.data.{action.replace('/', '.')}", client, count=len(result))
    return _safe({"documents": result, "external_writes": 0})


def _email(state, assistant, action: str, payload: dict[str, Any], client: str) -> dict[str, Any]:
    google = assistant.google()
    stage, _, kind = action.partition("/")
    if stage not in {"draft", "send"} or kind not in {"preview", "execute"}:
        raise AssistantError("Email action must be draft/send preview/execute")
    email = GoogleWorkspace.email_payload(_field(payload, "to", 320),
                                          _field(payload, "subject", 200),
                                          _field(payload, "body", 20_000, lines=True))
    label = "DRAFT EMAIL" if stage == "draft" else "SEND EMAIL"
    if kind == "preview":
        _confirm(payload, f"PREVIEW {label}")
        challenge = assistant.external.preview(label, email)
        _audit(state, f"assistant.email.{stage}.preview", client)
        return {"challenge": challenge, "payload": {"to": email["to"], "subject": email["subject"]},
                "external_writes": 0}
    challenge = payload.get("challenge")
    if not isinstance(challenge, dict):
        raise AssistantError("The original preview challenge is required")
    confirmation = clean_text(payload.get("confirmation") or payload.get("voice_confirmation") or "", 200)
    method = google.create_draft if stage == "draft" else google.send
    result = method(assistant.external, challenge, confirmation, email)
    _audit(state, f"assistant.email.{stage}.execute", client)
    return _safe({"result": result, "external_writes": 1})


def _voice_email(state, assistant, payload: dict[str, Any], client: str) -> dict[str, Any]:
    """Verify one audio sample, then consume and execute in this same request.

    No browser-supplied speaker score or boolean grant is accepted as authority.
    The original ExternalActions challenge is still the only write credential.
    """
    stage = str(payload.get("stage") or "")
    if stage not in {"draft", "send"}:
        raise AssistantError("Voice email stage must be draft or send")
    challenge = payload.get("challenge")
    if not isinstance(challenge, dict):
        raise AssistantError("The original email preview challenge is required")
    email = GoogleWorkspace.email_payload(_field(payload, "to", 320),
                                          _field(payload, "subject", 200),
                                          _field(payload, "body", 20_000, lines=True))
    data = _audio(payload)
    model = str(payload.get("model") or os.environ.get("NEXUS_WHISPER_MODEL") or "small")
    with tempfile.TemporaryDirectory(prefix="nexus-voice-email-") as directory:
        source = Path(directory) / "decision.wav"
        source.write_bytes(data)
        transcript = FasterWhisperSTT(model).transcribe(source)
        verification = SpeakerVerifier(state.workspace).verify(source)
    assistant.voice_approval.adopt(challenge)
    decision = assistant.voice_approval.decide(str(challenge.get("id") or ""), transcript.text,
                                               verification, email, consume=False)
    if not decision["approved"]:
        if decision["cancelled"]:
            # A clear cancellation invalidates the external challenge as well.
            assistant.external.fresh.consume(str(challenge.get("id") or ""),
                                             str(challenge.get("confirmation") or ""), email)
        _audit(state, f"assistant.email.{stage}.voice", client, approved=False,
               cancelled=decision["cancelled"])
        return {"decision": decision, "transcript": transcript.text, "language": transcript.language,
                "speaker": verification, "external_writes": 0}
    google = assistant.google()
    method = google.create_draft if stage == "draft" else google.send
    result = method(assistant.external, challenge, str(challenge.get("confirmation") or ""), email)
    _audit(state, f"assistant.email.{stage}.voice", client, approved=True)
    return _safe({"decision": decision, "transcript": transcript.text, "language": transcript.language,
                  "speaker": verification, "result": result, "external_writes": 1})
