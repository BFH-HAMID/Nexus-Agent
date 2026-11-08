"""CLI for ``nexus voice run | test-mic | test-tts | enroll | devices``."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .audio_io import AudioOutputPlayer, list_devices, test_microphone
from .config import VoiceError, load_voice_config, require_voice_enabled
from .pipeline import VoicePipeline, build_agent_handler
from .speaker import SpeakerVerifier
from .tts import create_tts, sanitize_tts_text


def _confirm_exact(args: argparse.Namespace, phrase: str) -> None:
    supplied = getattr(args, "confirm", None)
    if supplied is not None:
        if supplied != phrase:
            raise VoiceError(f'Confirmation must be exactly "{phrase}"')
        return
    if not sys.stdin.isatty():
        raise VoiceError(f'Non-interactive confirmation requires --confirm "{phrase}"')
    if getpass.getpass(f'Type "{phrase}" to confirm: ') != phrase:
        raise VoiceError("Confirmation did not match; cancelled")


def _audit(workspace: Path, action: str, result: str, **detail: Any) -> None:
    try:
        from .. import audit

        audit.record(
            workspace,
            action,
            who="user",
            target="voice",
            result=result,
            risk="high",
            detail=detail,
        )
    except OSError:
        pass


def cmd_devices(args: argparse.Namespace) -> int:
    devices = list_devices()
    if getattr(args, "json", False):
        print(json.dumps([d.to_dict() for d in devices], indent=2, ensure_ascii=False))
        return 0
    if not devices:
        print("No audio devices reported.")
        return 0
    for dev in devices:
        tags: list[str] = []
        if dev.max_input_channels > 0:
            tags.append(f"in:{dev.max_input_channels}")
        if dev.max_output_channels > 0:
            tags.append(f"out:{dev.max_output_channels}")
        if dev.is_default_input:
            tags.append("default-in")
        if dev.is_default_output:
            tags.append("default-out")
        print(f"  [{dev.index:>2}] {dev.name}  ({', '.join(tags)} @ {int(dev.default_samplerate)} Hz)")
    return 0


def cmd_test_mic(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace or ".").resolve()
    cfg = load_voice_config(workspace)
    device = getattr(args, "device", None)
    if device is None:
        device = cfg.input_device
    report = test_microphone(
        duration_seconds=float(getattr(args, "seconds", 2.0) or 2.0),
        device=device,
        sample_rate=cfg.sample_rate,
        save_path=getattr(args, "save", None),
    )
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    print(
        f"Microphone ok={report['ok']} device={report['device']} "
        f"duration={report['duration_seconds']}s rms={report['rms']} "
        f"peak={report['peak']} dbfs={report['dbfs']} signal={report['signal_detected']}"
    )
    if report["saved_wav"]:
        print(f"Saved capture to {report['saved_wav']}")
    return 0


def cmd_test_tts(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace or ".").resolve()
    cfg = load_voice_config(workspace)
    if getattr(args, "provider", None):
        from dataclasses import replace

        cfg = replace(cfg, tts=replace(cfg.tts, provider=args.provider))
    if getattr(args, "rate", None) is not None:
        from dataclasses import replace

        cfg = replace(cfg, tts=replace(cfg.tts, speaking_rate=float(args.rate)))
    raw_text = " ".join(getattr(args, "text", []) or []).strip() or "Nexus voice output test."
    safe_text = sanitize_tts_text(raw_text, workspace=workspace)
    tts = create_tts(cfg, workspace=workspace)
    clip = tts.synthesize(safe_text, language=getattr(args, "lang", None))
    output_path = getattr(args, "output", None)
    if output_path:
        target = Path(output_path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(clip.data)
    played = False
    if getattr(args, "play", False):
        played = not AudioOutputPlayer(device=cfg.output_device).play(clip)
    report = {
        "ok": True,
        "provider": tts.name,
        "voice": tts.select_voice(getattr(args, "lang", None)),
        "format": clip.format,
        "bytes": len(clip.data),
        "played": played,
        "output": str(Path(output_path).expanduser().resolve()) if output_path else "",
        "redacted": safe_text != " ".join(raw_text.split()).strip(),
    }
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    print(
        f"TTS ok provider={report['provider']} voice={report['voice']} "
        f"format={report['format']} bytes={report['bytes']} played={report['played']}"
    )
    return 0


def cmd_enroll(args: argparse.Namespace) -> int:
    workspace = Path(getattr(args, "workspace", None) or ".").resolve()
    cfg = load_voice_config(workspace)
    verifier = SpeakerVerifier(workspace, threshold=cfg.speaker.threshold)
    if getattr(args, "revoke", False):
        _confirm_exact(args, "REVOKE MY VOICE")
        revoked = verifier.revoke()
        _audit(workspace, "voice.speaker.revoke", "ok", revoked=revoked)
        payload = {"revoked": revoked, "enrolled": False}
        print(json.dumps(payload, indent=2))
        return 0
    if getattr(args, "status", False):
        print(json.dumps(verifier.status(), indent=2))
        return 0
    samples: list[str | Path] = list(getattr(args, "samples", []) or [])
    if getattr(args, "record", False):
        import tempfile

        _confirm_exact(args, "ENROLL MY VOICE")
        count = max(1, min(5, int(getattr(args, "count", 3) or 3)))
        seconds = max(1.0, min(10.0, float(getattr(args, "seconds", 3.0) or 3.0)))
        with tempfile.TemporaryDirectory(prefix="nexus-enroll-") as tmpdir:
            recorded: list[Path] = []
            for idx in range(count):
                wav_path = Path(tmpdir) / f"sample-{idx + 1}.wav"
                test_microphone(
                    duration_seconds=seconds,
                    device=cfg.input_device,
                    sample_rate=cfg.sample_rate,
                    save_path=wav_path,
                )
                recorded.append(wav_path)
            result = verifier.enroll(recorded)
    else:
        if not samples:
            raise VoiceError(
                "Provide 1 to 5 audio sample paths (or pass --record to capture from microphone)"
            )
        _confirm_exact(args, "ENROLL MY VOICE")
        result = verifier.enroll(samples)
    _audit(workspace, "voice.speaker.enroll", "ok", samples=result["samples"], dimensions=result["dimensions"])
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        print(f"Enrolled voiceprint ({result['samples']} samples, {result['dimensions']} dims) -> {result['path']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from dataclasses import replace

    workspace = Path(args.workspace or ".").resolve()
    cfg = require_voice_enabled(workspace)
    if getattr(args, "no_ack", False):
        cfg = replace(cfg, acknowledgements=replace(cfg.acknowledgements, enabled=False))
    if getattr(args, "no_duplex", False):
        cfg = replace(cfg, duplex=replace(cfg.duplex, enabled=False))
    if getattr(args, "realtime", None):
        rt_provider = str(args.realtime)
        cfg = replace(
            cfg,
            realtime=replace(cfg.realtime, enabled=rt_provider != "local", provider=rt_provider),
        )
    pipeline = VoicePipeline(cfg, workspace=workspace)
    handler = build_agent_handler(
        workspace,
        pipeline=pipeline,
        read_only=bool(getattr(args, "read_only", False)),
        auto_yes=bool(getattr(args, "yes", False)),
    )
    pipeline.text_handler = handler
    wav_path = getattr(args, "wav", None)
    if wav_path:
        result = pipeline.process_wav(wav_path, skip_wake=bool(getattr(args, "skip_wake", False)))
        payload = {
            "ok": not result.error,
            "transcript": result.transcript.text if result.transcript else "",
            "language": result.transcript.language if result.transcript else "",
            "reply_language": result.reply_language,
            "response": result.response_text,
            "spoken_sentences": result.spoken_sentences,
            "states": result.states,
            "speaker_matched": result.speaker_match.matched if result.speaker_match else None,
            "clarification_asked": result.clarification_asked,
            "hud_dispatched": result.hud_dispatched,
            "barge_in_action": result.barge_in_action,
            "approval_intent": result.approval_intent,
            "metrics": result.metrics.to_dict() if result.metrics else {},
            "error": result.error,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if not result.error else 2

    stop = threading.Event()
    max_turns = getattr(args, "max_turns", None)
    try:
        results = pipeline.run(handler=handler, stop_event=stop, max_turns=max_turns)
    except KeyboardInterrupt:
        stop.set()
        return 130
    print(json.dumps({"turns": len(results), "at": time.time()}))
    return 0


def command(args: argparse.Namespace) -> int:
    action = getattr(args, "voice_action", "")
    try:
        if action == "devices":
            return cmd_devices(args)
        if action == "test-mic":
            return cmd_test_mic(args)
        if action == "test-tts":
            return cmd_test_tts(args)
        if action == "enroll":
            return cmd_enroll(args)
        if action == "run":
            return cmd_run(args)
        raise VoiceError(f"Unknown voice command: {action}")
    except VoiceError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("voice", help="opt-in modular voice core (wake, Silero VAD, STT, TTS, speaker gate)")
    parser.add_argument("-C", "--workspace", default=None, help="workspace root (contains voice.yaml)")
    actions = parser.add_subparsers(dest="voice_action", required=True)

    p_run = actions.add_parser("run", help="run the voice assistant pipeline (requires voice.yaml enabled: true)")
    p_run.add_argument("-C", "--workspace", default=argparse.SUPPRESS)
    p_run.add_argument("--wav", help="process one WAV file through the pipeline instead of live mic")
    p_run.add_argument("--skip-wake", action="store_true", help="skip wake-word gating for --wav testing")
    p_run.add_argument("--read-only", action="store_true", help="run agent handler in plan/read-only mode")
    p_run.add_argument("--yes", "-y", action="store_true", help="auto-approve clean non-dangerous edits (never bypasses dangerous/speaker gates)")
    p_run.add_argument("--no-ack", action="store_true", help="disable short spoken acknowledgements ('Okay, on it')")
    p_run.add_argument("--no-duplex", action="store_true", help="disable full-duplex barge-in monitoring")
    p_run.add_argument("--realtime", choices=["local", "gemini-live", "livekit", "pipecat"], default=None, help="override realtime adapter for this run (local default)")
    p_run.add_argument("--max-turns", type=int, default=None)
    p_run.add_argument("--json", action="store_true")

    p_mic = actions.add_parser("test-mic", help="capture a bounded microphone sample and report RMS/peak levels")
    p_mic.add_argument("-C", "--workspace", default=argparse.SUPPRESS)
    p_mic.add_argument("--seconds", type=float, default=2.0)
    p_mic.add_argument("--device", default=None)
    p_mic.add_argument("--save", help="save captured 16 kHz mono WAV to path")
    p_mic.add_argument("--json", action="store_true")

    p_tts = actions.add_parser("test-tts", help="synthesize secret-redacted text using configured TTS provider")
    p_tts.add_argument("-C", "--workspace", default=argparse.SUPPRESS)
    p_tts.add_argument("text", nargs="*", help="text to synthesize")
    p_tts.add_argument("--provider", choices=["piper", "kokoro", "edge", "elevenlabs", "auto"])
    p_tts.add_argument("--lang", help="language hint (e.g. bn, en)")
    p_tts.add_argument("--rate", type=float, help="speaking rate (0.5..2.0)")
    p_tts.add_argument("--output", "-o", help="save synthesized audio to path")
    p_tts.add_argument("--play", action="store_true", help="play audio to speaker")
    p_tts.add_argument("--json", action="store_true")

    p_enroll = actions.add_parser("enroll", help="enroll or revoke owner voiceprint for DANGEROUS action gating")
    p_enroll.add_argument("-C", "--workspace", default=argparse.SUPPRESS)
    p_enroll.add_argument("samples", nargs="*", help="1..5 WAV/audio samples of owner voice")
    p_enroll.add_argument("--record", action="store_true", help="capture samples from microphone")
    p_enroll.add_argument("--count", type=int, default=3, help="number of samples to record when using --record")
    p_enroll.add_argument("--seconds", type=float, default=3.0, help="seconds per recorded sample")
    p_enroll.add_argument("--confirm", default=None, help="exact confirmation phrase ('ENROLL MY VOICE' or 'REVOKE MY VOICE')")
    p_enroll.add_argument("--revoke", action="store_true", help="delete enrolled speaker voiceprint")
    p_enroll.add_argument("--status", action="store_true", help="show enrollment status")
    p_enroll.add_argument("--json", action="store_true")

    p_dev = actions.add_parser("devices", help="list input/output audio devices")
    p_dev.add_argument("-C", "--workspace", default=argparse.SUPPRESS)
    p_dev.add_argument("--json", action="store_true")

    parser.set_defaults(func=command)


__all__ = ["add_parser", "command"]
