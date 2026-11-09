"""Explicit CLI for the personal assistant; no command starts on import."""
from __future__ import annotations

import dataclasses
import getpass
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .common import AssistantError, atomic_bytes
from .service import service, tts_for


def _default(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=_default))


def _confirm(args, phrase: str) -> None:
    supplied = getattr(args, "confirm", None)
    if supplied is not None:
        if supplied != phrase:
            raise AssistantError(f'Confirmation must be exactly "{phrase}"')
        return
    if not sys.stdin.isatty():
        raise AssistantError(f'Non-interactive use requires --confirm "{phrase}"')
    if getpass.getpass(f'Type "{phrase}" to confirm: ') != phrase:
        raise AssistantError("Confirmation did not match; no action was executed")


def _audit(workspace: Path, action: str, result: str = "requested", **detail: Any) -> None:
    from .. import securitycenter
    allowed = {key: value for key, value in detail.items() if isinstance(value, (str, int, float, bool))}
    securitycenter.append_event(workspace, {"at": time.time(), "actor": "user", "action": action,
                                "target": "assistant-cli", "result": result, "risk": "high",
                                "detail": {"client": "local-cli", **allowed}})


def _state(args):
    from ..server import ServerState
    return ServerState(Path(args.workspace or ".").resolve(), quiet=True)


def _detector(args):
    from .voice import OpenWakeWord, PorcupineWakeWord
    if not args.wake_model:
        raise AssistantError("Live listening requires --wake-model; alternatively pass a bounded audio file")
    if args.wake_engine == "porcupine":
        from .personal_data import NativeVault
        return PorcupineWakeWord(args.wake_model, access_key=NativeVault().get("porcupine"))
    return OpenWakeWord(args.wake_model)


def _voice(args) -> dict[str, Any]:
    from .voice import AudioPlayer, FasterWhisperSTT, SpeakerVerifier, listen_once, listen_utterance
    workspace = Path(args.workspace or ".").resolve()
    if args.operation == "enroll":
        _confirm(args, "ENROLL MY VOICE")
        if not args.values:
            raise AssistantError("Provide one to five WAV/MP3/FLAC/OGG/M4A samples")
        result = SpeakerVerifier(workspace).enroll(args.values)
        _audit(workspace, "assistant.voice.enroll", "ok", samples=len(args.values))
        return result
    if args.operation == "revoke":
        _confirm(args, "REVOKE MY VOICE")
        result = {"revoked": SpeakerVerifier(workspace).revoke()}
        _audit(workspace, "assistant.voice.revoke", "ok")
        return result
    if args.operation == "stt":
        if len(args.values) != 1:
            raise AssistantError("stt requires exactly one audio file")
        return dataclasses.asdict(FasterWhisperSTT(args.model).transcribe(args.values[0]))
    if args.operation == "verify":
        if len(args.values) != 1:
            raise AssistantError("verify requires exactly one audio file")
        return SpeakerVerifier(workspace).verify(args.values[0])
    if args.operation == "speak":
        text = " ".join(args.values).strip()
        if not text:
            raise AssistantError("speak requires text")
        if args.engine in {"edge", "elevenlabs"}:
            _confirm(args, "UPLOAD TEXT TO TTS")
        elif args.play:
            _confirm(args, "PLAY AUDIO")
        clip = tts_for(None, args.engine).synthesize(text)
        if args.output:
            atomic_bytes(Path(args.output).expanduser().resolve(), clip.data)
        played = AudioPlayer().play(clip, None) if args.play else False
        _audit(workspace, "assistant.voice.speak", "ok", engine=args.engine, online=args.engine in {"edge", "elevenlabs"})
        return {"synthesized": True, "bytes": len(clip.data), "format": clip.format,
                "output": args.output or "", "played": played}
    if args.operation != "ask":
        raise AssistantError("Unknown voice action")
    stt = FasterWhisperSTT(args.model)
    verifier = SpeakerVerifier(workspace)
    if args.values:
        if len(args.values) != 1:
            raise AssistantError("voice ask accepts one audio file, or no file with --wake-model")
        transcript = stt.transcribe(args.values[0])
        verification = verifier.verify(args.values[0])
        transcript = dataclasses.replace(transcript, speaker=verification)
    else:
        transcript = listen_once(_detector(args), stt, speaker=verifier, wake_timeout=args.wake_timeout)
    if not transcript.speaker or not transcript.speaker.get("matched"):
        raise AssistantError("Enrolled speaker did not match; task was not run")
    _audit(workspace, "assistant.voice.task", "verified", language=transcript.language, write=bool(args.write))
    print(f"Transcript [{transcript.language}]: {transcript.text}", flush=True)

    from ..agent import NexusAgent
    from ..config import load_config
    from ..providers import build_providers
    from ..tools import ApprovalDecision
    from ..types import EventType
    from .voice import SpeechStreamer, VoiceApproval

    config = load_config(workspace)
    if args.speak:
        config.settings.stream = True
    providers = build_providers(config.agents)
    speech_queue: queue.Queue[str | None] = queue.Queue()
    synthesizer = None
    streamer = None
    speech_result: dict[str, Any] = {}
    if args.speak:
        synthesizer = tts_for(None, args.engine)
        streamer = SpeechStreamer(synthesizer, AudioPlayer())

    def emit(event) -> None:
        nonlocal saw_delta
        if event.type is EventType.TEXT_DELTA and event.data.get("text"):
            saw_delta = True
            if streamer:
                speech_queue.put(str(event.data["text"]))
        elif event.type is EventType.TEXT and streamer and not saw_delta and event.data.get("text"):
            speech_queue.put(str(event.data["text"]))
        elif event.type in {EventType.STATUS, EventType.ERROR}:
            print(str(event.data.get("message") or ""), file=sys.stderr, flush=True)

    voice_approval = VoiceApproval()
    saw_delta = False

    def approve(spec, tool_args, risk, preview):
        if not args.write:
            return ApprovalDecision(False, reason="read-only voice task")
        payload = {"tool": spec.name, "args": tool_args, "risk": risk.value}
        challenge = voice_approval.begin("NEXUS VOICE TOOL " + spec.name, payload)
        prompt = (f"{spec.name}. {risk.value}. Say approve or cancel with code "
                  f"{challenge['confirmation']}. Preview: {str(preview)[:240]}")
        print(prompt, flush=True)
        if synthesizer:
            AudioPlayer().play(synthesizer.synthesize(prompt), None)
        answer = listen_utterance(stt, speaker=verifier)
        decision = voice_approval.decide(challenge["id"], answer.text, answer.speaker or {}, payload)
        _audit(workspace, "assistant.voice.approval", "ok" if decision["approved"] else "denied",
               tool=spec.name, cancelled=decision["cancelled"])
        return ApprovalDecision(bool(decision["approved"]), scope="once", reason=decision["reason"])

    agent = NexusAgent(config, workspace=workspace, providers=providers, emit=emit,
                       approval_callback=approve, auto_yes=False)
    speaker_thread = None
    if streamer:
        def chunks():
            while True:
                piece = speech_queue.get()
                if piece is None:
                    return
                yield piece
        speaker_thread = threading.Thread(target=lambda: speech_result.update(streamer.speak(chunks())),
                                          name="nexus-first-sentence-tts", daemon=True)
        speaker_thread.start()
        if sys.stdin.isatty():
            def barge_in():
                try:
                    input("Press Enter at any time to interrupt speech (the text task still finishes).\n")
                    streamer.interrupt.set()
                except (EOFError, KeyboardInterrupt):
                    pass
            threading.Thread(target=barge_in, name="nexus-voice-barge-in", daemon=True).start()
    try:
        from ..vision.service import LOOK_REQUEST, SCREEN_REQUEST, VisionService
        requested = LOOK_REQUEST.fullmatch(transcript.text.strip()) or SCREEN_REQUEST.search(transcript.text)
        if requested:
            if args.speak and args.engine not in {'piper', 'kokoro'}:
                raise AssistantError('Vision answers require explicitly local speech, not cloud TTS')
            if not args.write:
                agent.gate.mode = 'plan'
            result = VisionService(workspace).look(transcript.text, gate=agent.gate, channel='voice')
            if streamer:
                speech_queue.put(result['answer'])
        else:
            result = agent.run(transcript.text, read_only=not args.write)
    finally:
        speech_queue.put(None)
        if speaker_thread:
            speaker_thread.join(timeout=60)
    return {"transcript": dataclasses.asdict(transcript), "result": result if isinstance(result, dict) else dataclasses.asdict(result),
            "speech": speech_result, "speaker_verified": True}


def _memory(args) -> dict[str, Any]:
    from .memory import PersonalMemory
    store = PersonalMemory(Path(args.workspace or ".").resolve())
    action = args.operation
    if action == "status":
        return store.view(args.query)
    if action in {"enable", "disable"}:
        enabled = action == "enable"
        _confirm(args, "SAVE PERSONAL MEMORY" if enabled else "DISABLE PERSONAL MEMORY")
        return store.set_enabled(enabled)
    if action == "add":
        _confirm(args, "ADD PERSONAL MEMORY")
        return store.remember(" ".join(args.values), kind=args.kind)
    if action == "edit":
        _confirm(args, f"EDIT PERSONAL MEMORY {args.id}")
        return store.edit(args.id, " ".join(args.values), kind=args.kind)
    if action == "forget":
        if args.all:
            _confirm(args, "FORGET ALL PERSONAL MEMORY")
            return {"forgotten": store.forget_all("FORGET ALL PERSONAL MEMORY")}
        _confirm(args, f"FORGET PERSONAL MEMORY {args.id}")
        return {"forgotten": store.forget(args.id)}
    if action == "profile":
        if args.value is None:
            profile = store.profile()
            return {args.key: profile.get(args.key)} if args.key else profile
        _confirm(args, "SAVE PERSONAL PROFILE")
        return store.set_profile(args.key, args.value)
    if action == "context":
        if args.values:
            _confirm(args, "SAVE ACTIVE CONTEXT")
            return store.set_active(" ".join(args.values), {"source": "cli"})
        _confirm(args, "CLEAR ACTIVE CONTEXT")
        return store.clear_active()
    raise AssistantError("Unknown memory action")


def _remind(args) -> dict[str, Any]:
    from .proactive import ReminderStore
    store = ReminderStore(Path(args.workspace or ".").resolve())
    if args.operation == "list":
        return {"reminders": store.list()}
    if args.operation == "add":
        _confirm(args, "ADD REMINDER")
        return store.add(" ".join(args.values), run_at=args.at, every_minutes=args.every,
                         daily=args.daily, weekly=args.weekly, timezone=args.timezone)
    if args.operation == "cancel":
        _confirm(args, f"CANCEL REMINDER {args.id}")
        return {"cancelled": store.cancel(args.id)}
    raise AssistantError("Unknown reminder action")


def _monitor(args) -> dict[str, Any]:
    from .proactive import MonitorStore
    store = MonitorStore(Path(args.workspace or ".").resolve())
    if args.operation == "list":
        return {"monitors": store.list()}
    if args.operation == "add":
        _confirm(args, "ADD MONITOR")
        options = json.loads(args.options)
        if not isinstance(options, dict):
            raise AssistantError("--options must be a JSON object")
        return store.add(args.kind, options, args.every)
    if args.operation == "remove":
        _confirm(args, f"REMOVE MONITOR {args.id}")
        return {"removed": store.remove(args.id)}
    raise AssistantError("Unknown monitor action")


def _vision(args) -> dict[str, Any]:
    """Legacy commands keep their names, but share TASK 11 policy and approvals."""
    from ..vision.camera import CameraPipeline
    from ..vision.cli import operator_gate
    from ..vision.faces import FaceRegistry
    from ..vision.policy import load_policy
    from ..vision.service import VisionService
    workspace = Path(args.workspace or ".").resolve()
    gate = operator_gate(workspace)
    vision = VisionService(workspace)
    cfg = load_policy(workspace)
    if args.agent not in (None, '', cfg['vlm']['model']):
        raise AssistantError('Set the exact model in vision.yaml; --agent cannot override privacy policy')
    if args.operation == "ask":
        if len(args.values) != 1:
            raise AssistantError("vision ask requires one bounded upload path")
        return vision.ask_path(args.values[0], args.query, gate=gate, cloud=args.cloud, no_people=args.no_people)
    if args.operation == "screen":
        return vision.screen(args.query, gate=gate, cloud=args.cloud, no_people=args.no_people)
    if args.operation == "camera":
        if args.camera_index != cfg['camera']['device'] or (args.faces and not cfg['faces']['enabled']):
            raise AssistantError('Camera and face flags must match vision.yaml')
        if args.weights and (workspace / args.weights).resolve() != (workspace / cfg['camera']['weights']).resolve():
            raise AssistantError('Weights must match vision.yaml')
        return CameraPipeline(workspace).run(gate=gate, frames=1)
    registry = FaceRegistry(workspace)
    if args.operation == "enroll-face":
        if len(args.values) != 1 or not args.consent:
            raise AssistantError("enroll-face requires --consent and one local image")
        return registry.enroll(args.name, registry.load_image(args.values[0]), consent=True, gate=gate)
    if args.operation == "recognize-face":
        if len(args.values) != 1:
            raise AssistantError("recognize-face requires one image")
        return {"faces": registry.recognize(registry.load_image(args.values[0]))}
    if args.operation == "forget-face":
        return {"forgotten": registry.forget(args.name, gate=gate)}
    raise AssistantError("Unknown vision action")


def _data(args) -> dict[str, Any]:
    from datetime import datetime, timedelta

    from .personal_data import ExternalActions, GoogleWorkspace, NativeVault, NotionNotes, ObsidianNotes
    workspace = Path(args.workspace or ".").resolve()
    vault = NativeVault()
    google = GoogleWorkspace(vault)
    if args.operation == "gmail":
        result = google.gmail(query=args.query, limit=args.limit)
    elif args.operation == "calendar":
        now = datetime.now().astimezone()
        result = google.calendar(time_min=now.isoformat(), time_max=(now + timedelta(days=args.days)).isoformat(),
                                 limit=args.limit)
    elif args.operation == "contacts":
        result = google.contacts(limit=args.limit)
    elif args.operation == "drive":
        result = google.drive(query=args.query, limit=args.limit)
    elif args.operation == "notion":
        result = NotionNotes(vault).search(args.query, limit=args.limit)
    elif args.operation == "obsidian":
        result = ObsidianNotes(Path(args.vault or os.environ.get("NEXUS_OBSIDIAN_VAULT", ""))).search(
            args.query, limit=args.limit)
    elif args.operation in {"draft", "send"}:
        if not args.to or args.subject is None:
            raise AssistantError("Email draft/send requires --to and --subject; body is the positional value")
        payload = google.email_payload(args.to, args.subject, " ".join(args.values))
        actions = ExternalActions()
        label = "DRAFT EMAIL" if args.operation == "draft" else "SEND EMAIL"
        challenge = actions.preview(label, payload)
        _print({"preview": {"to": payload["to"], "subject": payload["subject"], "action": label},
                "confirmation": challenge["confirmation"], "external_writes": 0})
        if args.confirm != challenge["confirmation"]:
            if args.confirm is not None or not sys.stdin.isatty():
                raise AssistantError("Email not written: exact fresh preview confirmation is required")
            args.confirm = getpass.getpass("Type the fresh preview confirmation: ")
        _audit(workspace, f"assistant.email.{args.operation}", "requested")
        result = (google.create_draft if args.operation == "draft" else google.send)(
            actions, challenge, args.confirm or "", payload)
        _audit(workspace, f"assistant.email.{args.operation}", "ok")
        return {"result": result, "external_writes": 1}
    else:
        raise AssistantError("Unknown personal-data action")
    _audit(workspace, f"assistant.data.{args.operation}", "ok")
    return {"documents": result, "external_writes": 0}


def _smarthome(args) -> dict[str, Any]:
    import os

    from .automation import HomeAssistant, MqttHome
    from .personal_data import ExternalActions, NativeVault
    vault = NativeVault()
    actions = ExternalActions()
    from ..computer_cli import operator_gate
    workspace = Path(args.workspace or ".").resolve()
    gate = operator_gate(workspace)
    if args.operation in {"states", "call"}:
        entities = args.entity or [row for row in os.environ.get("NEXUS_HA_ENTITIES", "").split(",") if row]
        home = HomeAssistant(vault, base_url=args.url or os.environ.get("NEXUS_HA_URL", ""), entities=entities or None, workspace=workspace, gate=gate)
        if args.operation == "states":
            return {"states": home.states(), "external_writes": 0}
        payload = {"domain": args.domain, "service": args.service_name, "entity_id": args.entity_id}
        challenge = actions.preview("HOME ASSISTANT", payload)
        _print({"preview": payload, "confirmation": challenge["confirmation"], "external_writes": 0})
        if args.confirm != challenge["confirmation"]:
            if args.confirm is not None or not sys.stdin.isatty():
                raise AssistantError("Smart-home service call requires its fresh preview confirmation")
            args.confirm = getpass.getpass("Type the fresh preview confirmation: ")
        result = home.call(actions, challenge, args.confirm or "", payload)
        return {"result": result, "external_writes": 1}
    if args.operation == "publish":
        prefixes = args.topic_prefix or [row for row in os.environ.get("NEXUS_MQTT_PREFIXES", "").split(",") if row]
        home = MqttHome(host=args.host or os.environ.get("MQTT_HOST", ""),
                        username=args.username or os.environ.get("MQTT_USERNAME", ""),
                        password="", topic_prefixes=prefixes or None, port=args.port, workspace=workspace, gate=gate)
        payload = {"topic": args.topic, "message": " ".join(args.values)}
        challenge = actions.preview("MQTT PUBLISH", payload)
        _print({"preview": payload, "confirmation": challenge["confirmation"], "external_writes": 0})
        if args.confirm != challenge["confirmation"]:
            if args.confirm is not None or not sys.stdin.isatty():
                raise AssistantError("MQTT publish requires its fresh preview confirmation")
            args.confirm = getpass.getpass("Type the fresh preview confirmation: ")
        return {"result": home.publish(actions, challenge, args.confirm or "", payload), "external_writes": 1}
    raise AssistantError("Unknown smart-home action")


def command(args) -> int:
    try:
        action = args.action
        workspace = Path(args.workspace or ".").resolve()
        operation = str(getattr(args, "operation", "run"))
        _audit(workspace, f"assistant.cli.{action}.{operation}", "requested")
        if action == "capabilities":
            from . import capabilities
            result = capabilities()
        elif action == "memory":
            result = _memory(args)
        elif action == "remind":
            result = _remind(args)
        elif action == "monitor":
            result = _monitor(args)
        elif action == "voice":
            result = _voice(args)
        elif action == "vision":
            result = _vision(args)
        elif action == "data":
            result = _data(args)
        elif action in {"brief", "scheduler"}:
            state = _state(args)
            try:
                assistant = service(state)
                if action == "brief":
                    phrase = "SUMMARIZE MORNING BRIEF" if args.summarize else "BUILD MORNING BRIEF"
                    _confirm(args, phrase)
                    result = assistant.morning_brief(summarize=args.summarize, agent=args.agent)
                elif args.once:
                    _confirm(args, "RUN ASSISTANT SCHEDULER")
                    result = assistant.pass_once()
                else:
                    _confirm(args, "START ASSISTANT SCHEDULER")
                    result = assistant.start_worker(interval=args.interval)
                    _print(result)
                    while assistant.worker and assistant.worker.thread and assistant.worker.thread.is_alive():
                        time.sleep(1)
                    return 0
            finally:
                state.mission.close()
        elif action == "research":
            from ..config import load_config
            from ..providers import build_providers
            from .reasoning import WebResearcher
            workspace = Path(args.workspace or ".").resolve()
            config = load_config(workspace)
            result = WebResearcher(config, build_providers(config.agents), allowed_domains=args.domain).research(
                " ".join(args.values), urls=args.url, agent=args.agent)
        elif action == "reason":
            from ..agent import NexusAgent
            from ..config import load_config
            from ..providers import build_providers
            from .reasoning import ReasoningLoop
            workspace = Path(args.workspace or ".").resolve()
            config = load_config(workspace)
            providers = build_providers(config.agents)
            approval = None
            if args.write:
                _confirm(args, "RUN WRITING REASONING")
                from ..cli import interactive_approval
                approval = interactive_approval

            def execute(task, plan, feedback):
                text = task + ("\nCritic repair: " + feedback if feedback else "")
                run = NexusAgent(config, workspace=workspace, providers=providers,
                                 approval_callback=approval, auto_yes=False).run(text, read_only=not args.write)
                return {"text": run.text, "ok": run.ok, "evidence": run.stopped_reason}
            result = ReasoningLoop(config, providers, execute, max_repairs=args.repairs).run(
                " ".join(args.values), agent=args.agent)
        elif action == "backup":
            from ..computer_cli import operator_gate
            from .automation import BackupManager
            manager = BackupManager(workspace, gate=operator_gate(workspace))
            if args.operation == "create":
                _confirm(args, "CREATE BACKUP")
                result = manager.create()
            else:
                _confirm(args, f"RESTORE BACKUP {args.id}")
                result = manager.restore(args.id, args.output or "restored",
                                         confirmation=f"RESTORE BACKUP {args.id}")
        elif action == "desktop":
            from .automation import DesktopController
            payload = json.loads(args.options)
            _confirm(args, f"APPROVE DESKTOP {args.operation}")
            from ..computer_cli import operator_gate
            controller = DesktopController(backend=args.backend, workspace=workspace, gate=operator_gate(workspace))
            result = controller.execute(args.operation, payload)
        elif action == "smarthome":
            result = _smarthome(args)
        elif action == "service":
            from .systemd import install, remove, status
            if args.operation == "status":
                result = status()
            elif args.operation == "install":
                _confirm(args, "INSTALL ASSISTANT SERVICE")
                result = install(workspace)
            else:
                _confirm(args, "REMOVE ASSISTANT SERVICE")
                result = remove()
        elif action == "workflow":
            from ..computer_cli import operator_gate
            from .automation import DesktopController, WorkflowRecorder
            recorder = WorkflowRecorder(workspace, gate=operator_gate(workspace))
            if args.operation == "start":
                _confirm(args, "RECORD SEMANTIC WORKFLOW")
                result = recorder.start(args.name)
            elif args.operation == "stop":
                result = recorder.stop()
            elif args.operation == "show":
                from .common import read_json
                result = read_json(recorder.path, {})
            else:
                _confirm(args, "REPLAY REVIEWED WORKFLOW")
                controller = DesktopController(backend=args.backend, workspace=workspace, gate=operator_gate(workspace))
                result = {"actions": recorder.replay(controller, name=args.name)}
        else:
            raise AssistantError("Unknown assistant command")
        _audit(workspace, f"assistant.cli.{action}.{operation}", "ok")
        _print(result)
        return 0
    except (AssistantError, ValueError, OSError, ImportError, json.JSONDecodeError) as exc:
        _print({"error": str(exc)[:1000], "executed": False})
        return 2


def add_parser(sub) -> None:
    parser = sub.add_parser("assistant", help="opt-in voice, vision, memory, proactive and reviewed automation")
    parser.add_argument("-C", "--workspace")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("capabilities")

    memory = actions.add_parser("memory")
    memory.add_argument("operation", choices=["status", "enable", "disable", "add", "edit", "forget", "profile", "context"])
    memory.add_argument("values", nargs="*")
    memory.add_argument("--id", default="")
    memory.add_argument("--kind", default=None)
    memory.add_argument("--key", default="")
    memory.add_argument("--value")
    memory.add_argument("--query", default="")
    memory.add_argument("--all", action="store_true")
    memory.add_argument("--confirm")
    memory.set_defaults(action="memory")

    remind = actions.add_parser("remind")
    remind.add_argument("operation", choices=["list", "add", "cancel"])
    remind.add_argument("values", nargs="*")
    remind.add_argument("--id", default="")
    remind.add_argument("--at", type=float)
    remind.add_argument("--every", type=float)
    remind.add_argument("--daily")
    remind.add_argument("--weekly")
    remind.add_argument("--timezone", default="UTC")
    remind.add_argument("--confirm")
    remind.set_defaults(action="remind")

    monitor = actions.add_parser("monitor")
    monitor.add_argument("operation", choices=["list", "add", "remove"])
    monitor.add_argument("--kind", choices=["server", "website", "market", "github_ci"], default="server")
    monitor.add_argument("--options", default="{}")
    monitor.add_argument("--every", type=float, default=5)
    monitor.add_argument("--id", default="")
    monitor.add_argument("--confirm")
    monitor.set_defaults(action="monitor")

    voice = actions.add_parser("voice")
    voice.add_argument("operation", choices=["stt", "speak", "enroll", "verify", "revoke", "ask"])
    voice.add_argument("values", nargs="*")
    voice.add_argument("--model", default=os.environ.get("NEXUS_WHISPER_MODEL", "small"))
    voice.add_argument("--engine", choices=["piper", "kokoro", "edge", "elevenlabs"], default="piper")
    voice.add_argument("--output")
    voice.add_argument("--play", action="store_true")
    voice.add_argument("--speak", action="store_true", help="stream the answer from its first generated sentence")
    voice.add_argument("--write", action="store_true", help="allow reviewed tools; default voice ask is read-only")
    voice.add_argument("--wake-engine", choices=["openwakeword", "porcupine"], default="openwakeword")
    voice.add_argument("--wake-model")
    voice.add_argument("--wake-timeout", type=float, default=60)
    voice.add_argument("--confirm")
    voice.set_defaults(action="voice")

    vision = actions.add_parser("vision")
    vision.add_argument("operation", choices=["ask", "screen", "camera", "enroll-face", "recognize-face", "forget-face"])
    vision.add_argument("values", nargs="*", help="upload/image path; screen and camera take no path")
    vision.add_argument("--query", default="Describe visible errors briefly.")
    vision.add_argument("--agent")
    vision.add_argument("--cloud", action="store_true", help="separately reviewed HTTPS screenshot upload")
    vision.add_argument("--weights", help="existing local YOLO .pt weights; never downloaded implicitly")
    vision.add_argument("--faces", action="store_true", help="recognize enrolled faces in the explicit camera frame")
    vision.add_argument("--camera-index", type=int, default=0)
    vision.add_argument("--name", default="owner")
    vision.add_argument("--confirm", help="legacy option; never bypasses fresh vision approval")
    vision.add_argument("--consent", action="store_true", help="person freely consented to local face enrollment")
    vision.add_argument("--no-people", action="store_true", help="attest cloud upload contains no people/faces")
    vision.set_defaults(action="vision")

    data = actions.add_parser("data")
    data.add_argument("operation", choices=["gmail", "calendar", "contacts", "drive", "notion", "obsidian", "draft", "send"])
    data.add_argument("values", nargs="*")
    data.add_argument("--query", default="newer_than:1d")
    data.add_argument("--limit", type=int, default=10)
    data.add_argument("--days", type=int, default=1)
    data.add_argument("--vault")
    data.add_argument("--to")
    data.add_argument("--subject")
    data.add_argument("--confirm")
    data.set_defaults(action="data")

    brief = actions.add_parser("brief")
    brief.add_argument("--summarize", action="store_true")
    brief.add_argument("--agent")
    brief.add_argument("--confirm")
    brief.set_defaults(action="brief")

    scheduler = actions.add_parser("scheduler")
    scheduler.add_argument("--once", action="store_true")
    scheduler.add_argument("--interval", type=float, default=30)
    scheduler.add_argument("--confirm")
    scheduler.set_defaults(action="scheduler")

    research = actions.add_parser("research")
    research.add_argument("values", nargs="+")
    research.add_argument("--url", action="append", required=True)
    research.add_argument("--domain", action="append", required=True)
    research.add_argument("--agent")
    research.set_defaults(action="research")

    reason = actions.add_parser("reason")
    reason.add_argument("values", nargs="+")
    reason.add_argument("--agent")
    reason.add_argument("--repairs", type=int, default=1)
    reason.add_argument("--write", action="store_true")
    reason.add_argument("--confirm")
    reason.set_defaults(action="reason")

    backup = actions.add_parser("backup")
    backup.add_argument("operation", choices=["create", "restore"])
    backup.add_argument("--id", default="")
    backup.add_argument("--output")
    backup.add_argument("--confirm")
    backup.set_defaults(action="backup")

    desktop = actions.add_parser("desktop")
    desktop.add_argument("operation", choices=["move", "click", "type", "key", "scroll", "open", "close", "focus", "hotkey", "screenshot"])
    desktop.add_argument("--options", required=True, help="bounded JSON arguments")
    desktop.add_argument("--backend", default="auto")
    desktop.add_argument("--confirm")
    desktop.set_defaults(action="desktop")

    smarthome = actions.add_parser("smarthome")
    smarthome.add_argument("operation", choices=["states", "call", "publish"])
    smarthome.add_argument("values", nargs="*")
    smarthome.add_argument("--url")
    smarthome.add_argument("--entity", action="append")
    smarthome.add_argument("--domain", default="")
    smarthome.add_argument("--service-name", default="")
    smarthome.add_argument("--entity-id", default="")
    smarthome.add_argument("--host")
    smarthome.add_argument("--username")
    smarthome.add_argument("--port", type=int, default=8883)
    smarthome.add_argument("--topic-prefix", action="append")
    smarthome.add_argument("--topic", default="")
    smarthome.add_argument("--confirm")
    smarthome.set_defaults(action="smarthome")

    service_parser = actions.add_parser("service")
    service_parser.add_argument("operation", choices=["status", "install", "remove"])
    service_parser.add_argument("--confirm")
    service_parser.set_defaults(action="service")

    workflow = actions.add_parser("workflow")
    workflow.add_argument("operation", choices=["start", "stop", "show", "replay"])
    workflow.add_argument("--name", default="routine")
    workflow.add_argument("--backend", default="auto")
    workflow.add_argument("--confirm")
    workflow.set_defaults(action="workflow")
    parser.set_defaults(func=command)
