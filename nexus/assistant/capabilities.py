"""Honest runtime inventory for the nine assistant feature families."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from typing import Any


def _module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _feature(name: str, *, implemented: bool = True, ready: bool | None = None,
             requires: str = "explicit opt-in", note: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "implemented": implemented,
        "ready": bool(ready) if ready is not None else False,
        "requires": requires,
        "note": note or ("Adapter is present; live/native acceptance is separate." if ready is None else ""),
    }


def capabilities() -> dict[str, Any]:
    """Report implemented adapters separately from machine/account readiness."""
    voice_ready = {
        "openwakeword": _module("openwakeword"),
        "porcupine": _module("pvporcupine"),
        "faster_whisper": _module("faster_whisper"),
        "piper": bool(shutil.which("piper")) or _module("piper"),
        "kokoro": _module("kokoro"),
        "edge_tts": _module("edge_tts"),
        "elevenlabs_https": True,
        "speaker_verification": _module("resemblyzer"),
        "audio_io": _module("sounddevice"),
    }
    groups = [
        ("voice", "Voice interface", [
            _feature("Wake word: Hey Nexus", ready=voice_ready["openwakeword"] or voice_ready["porcupine"],
                     requires="voice extra + a trained Hey Nexus model"),
            _feature("Bangla/English STT with auto language detection", ready=voice_ready["faster_whisper"],
                     requires="faster-whisper + local model"),
            _feature("Offline Piper/Kokoro and online Edge/ElevenLabs TTS",
                     ready=any(voice_ready[key] for key in ("piper", "kokoro", "edge_tts", "elevenlabs_https")),
                     requires="an explicitly selected TTS engine/model/account"),
            _feature("Sentence streaming and barge-in", ready=voice_ready["audio_io"], requires="audio output/input"),
            _feature("Fresh challenge-bound Bangla/English voice approval", ready=voice_ready["speaker_verification"],
                     requires="speaker enrolment + verified sample + one-use challenge"),
            _feature("Speaker verification", ready=voice_ready["speaker_verification"], requires="resemblyzer enrolment"),
        ]),
        ("perception", "Vision & perception", [
            _feature("Screenshot VLM analysis", requires="vision.yaml + context.yaml opt-in, OCR, and a local Qwen2-VL or reviewed Gemini model",
                     note="Single-request, no-tool adapter; per-capture audit and hard off. Device/model readiness is not inferred."),
            _feature("Camera + YOLO object detection", ready=_module("ultralytics") and _module("cv2"),
                     requires="vision.yaml + vision extra + trusted local YOLOv8 weights + approved camera session"),
            _feature("Face recognition", ready=_module("face_recognition") and _module("cv2"),
                     requires="vision-faces extra + vision.yaml opt-in + consensual enrollment + native keyring"),
            _feature("Document/image question answering", requires="vision-documents extra + files/channel opt-in + configured VLM",
                     note="Bounded in-memory images/PDF pages with validated page/region references."),
        ]),
        ("personal_data", "Personal data integrations", [
            _feature("Gmail read/summary/draft; Calendar, Contacts and Drive", ready=True,
                     requires="Google OAuth access token in the native vault"),
            _feature("Notion search", ready=True, requires="Notion token in the native vault"),
            _feature("Obsidian note search", ready=True, requires="an explicitly allowed local vault directory"),
            _feature("No external send without fresh approval", ready=True, requires="one-use exact action challenge",
                     note="Read and draft paths never imply send permission."),
        ]),
        ("memory", "Long-term memory & personalization", [
            _feature("Persistent profile, preferences, routine and active context", ready=True,
                     requires="personal memory opt-in"),
            _feature("FAISS personal vector memory", ready=_module("faiss"), requires="faiss extra; NumPy fallback available"),
            _feature("Forget/edit/delete UI and revisioned multi-device reads", ready=True, requires="authenticated dashboard"),
        ]),
        ("proactive", "Proactivity", [
            _feature("Morning brief: weather/calendar/email/news", ready=True, requires="explicit source configuration"),
            _feature("Reminders and recurring tasks", ready=True, requires="scheduler running"),
            _feature("Website/server/market/GitHub CI monitors", ready=True, requires="per-monitor opt-in and connectivity"),
            _feature("Telegram/voice/desktop notifications + DND", ready=True, requires="existing channel opt-in"),
        ]),
        ("automation", "Control & automation", [
            _feature("Allowlisted Linux X11 / Windows UIA app, window, input and screenshot controls",
                     ready=bool(shutil.which("xdotool") and shutil.which("wmctrl") or _module("pywinauto")),
                     requires="operator YAML app allowlist + desktop extra + fresh shared-gate approval"),
            _feature("Origin-locked Playwright browser automation", ready=_module("playwright"),
                     requires="computer extra; disabled by default"),
            _feature("Scoped file backup/restore and semantic workflow record/replay", ready=True,
                     requires="operator YAML roots/macros, reviewed SHA-256 pins + per-step fresh approval; no raw keystroke recording"),
            _feature("Home Assistant / MQTT smart home", ready=_module("paho.mqtt.client"),
                     requires="operator YAML exact device/service/topic map + shared approval; TLS MQTT or HTTPS Home Assistant"),
        ]),
        ("reasoning", "Multi-agent & reasoning", [
            _feature("Planner → executor → critic and bounded self-reflection", ready=True,
                     requires="scripted/configured planner, executor and critic"),
            _feature("Simple/local versus complex/large model routing", ready=True,
                     requires="simple_agent/complex_agent orchestration settings"),
            _feature("Web research with citations", ready=True, requires="explicit search/fetch configuration and network"),
        ]),
        ("interface", "Interface & access", [
            _feature("JARVIS HUD live voice waveform", ready=True, requires="browser microphone permission"),
            _feature("Installable mobile PWA", ready=(Path(__file__).parent.parent / "panel" / "manifest.webmanifest").is_file(),
                     requires="HTTPS or loopback browser context"),
            _feature("systemd auto-start/restart and crash recovery", ready=shutil.which("systemctl") is not None,
                     requires="reviewed user-service installation"),
            _feature("Phone ↔ laptop revisioned sync", ready=True,
                     requires="both devices connected to the same authenticated Nexus workspace"),
        ]),
        ("security", "Audit & security", [
            _feature("Chained audit records for every mutation", ready=True, requires="writable audit store"),
            _feature("Email/web/document prompt-injection boundary", ready=True, requires="untrusted evidence wrapper"),
            _feature("Cost/rate limits and native secret vault", ready=True,
                     requires="existing policy; native OS keyring for newly enrolled account tokens"),
        ]),
    ]
    return {
        "schema": 1,
        "default_enabled": False,
        "groups": [{"id": ident, "title": title, "features": rows} for ident, title, rows in groups],
        "engines": voice_ready,
        "acceptance": "Adapter/unit verification is not live microphone, camera, biometric, account, smart-home or OS acceptance.",
    }
