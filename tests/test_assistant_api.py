"""Authenticated assistant API, CLI wiring, voice email and PWA contracts."""
from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from nexus.assistant import api
from nexus.assistant.common import AssistantError
from nexus.assistant.memory import PersonalMemory
from nexus.assistant.proactive import BriefSchedule, MonitorStore
from nexus.assistant.voice import PorcupineWakeWord, Transcript
from nexus.cli import main
from nexus.server import ServerState, make_handler


@pytest.fixture()
def state(repo: Path):
    created = ServerState(repo, token="test-token", quiet=True)
    try:
        yield created
    finally:
        if getattr(created, "assistant", None):
            created.assistant.stop_worker()
        created.mission.close()


def test_overview_is_honest_and_memory_mutations_are_confirmed(state):
    initial = api.get(state, "overview", {})
    assert initial["capabilities"]["default_enabled"] is False
    assert len(initial["capabilities"]["groups"]) == 9
    assert initial["memory"]["database_exists"] is False
    with pytest.raises(AssistantError, match="SAVE PERSONAL MEMORY"):
        api.post(state, "memory/enable", {"enabled": True, "confirmation": "yes"}, "local")
    assert not PersonalMemory(state.workspace).path.exists()
    api.post(state, "memory/enable", {"enabled": True, "confirmation": "SAVE PERSONAL MEMORY"}, "local")
    added = api.post(state, "memory/entry", {"text": "I prefer Bangla", "kind": "preference",
                                            "confirmation": "ADD PERSONAL MEMORY"}, "local")
    ident = added["id"]
    found = api.get(state, "memory", {"q": ["language preference"]})
    assert found["entries"][0]["id"] == ident
    edited = api.post(state, "memory/entry", {"id": ident, "text": "Use Bangla first", "kind": "preference",
                                             "confirmation": f"EDIT PERSONAL MEMORY {ident}"}, "local")
    assert edited["kind"] == "preference"
    cursor = api.get(state, "overview", {})["sync"]["cursor"]
    unchanged = api.get(state, "overview", {"since": [str(cursor)]})
    assert unchanged["memory_changed"] is False and unchanged["memory"]["unchanged"] is True
    forgotten = api.post(state, "memory/forget", {"id": ident, "confirmation": f"FORGET PERSONAL MEMORY {ident}"}, "local")
    assert forgotten["forgotten"] == 1


def test_demo_refuses_assistant_actions_before_any_write(state):
    state.demo = True
    with pytest.raises(AssistantError, match="live mode"):
        api.post(state, "memory/enable", {"enabled": True, "confirmation": "SAVE PERSONAL MEMORY"})
    assert not PersonalMemory(state.workspace).path.exists()


def _preview_email(state, monkeypatch, *, to="person@example.com"):
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "test-oauth-token")
    monkeypatch.setattr("nexus.assistant.api.FasterWhisperSTT.__init__", lambda self, model: None)
    preview = api.post(state, "email/send/preview", {"to": to, "subject": "Deploy", "body": "Reviewed body",
                                                    "confirmation": "PREVIEW SEND EMAIL"}, "local")
    calls = []
    monkeypatch.setattr("nexus.assistant.personal_data._json_request",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or {"id": "sent-1"})
    return preview, calls


def _voice_payload(preview, **changes):
    return {"to": "person@example.com", "subject": "Deploy", "body": "Reviewed body",
            "stage": "send", "challenge": preview["challenge"],
            "audio_base64": base64.b64encode(b"test-audio").decode(), **changes}


def test_voice_email_verifies_server_side_and_executes_once(state, monkeypatch):
    preview, calls = _preview_email(state, monkeypatch)
    code = preview["challenge"]["confirmation"]
    monkeypatch.setattr("nexus.assistant.api.FasterWhisperSTT.transcribe",
                        lambda self, path: Transcript(f"হ্যাঁ চালাও {code}", "bn"))
    monkeypatch.setattr("nexus.assistant.api.SpeakerVerifier.verify",
                        lambda self, path: {"matched": True, "score": 0.91, "threshold": 0.75})
    result = api.post(state, "voice/email", _voice_payload(preview), "local")
    assert result["external_writes"] == 1 and result["decision"]["approved"] is True
    assert result["language"] == "bn" and len(calls) == 1
    with pytest.raises(AssistantError):
        api.post(state, "voice/email", _voice_payload(preview), "local")
    assert len(calls) == 1


def test_voice_email_rejects_unmatched_speaker_and_action_tampering(state, monkeypatch):
    preview, calls = _preview_email(state, monkeypatch)
    code = preview["challenge"]["confirmation"]
    monkeypatch.setattr("nexus.assistant.api.FasterWhisperSTT.transcribe",
                        lambda self, path: Transcript(f"হ্যাঁ চালাও {code}", "bn"))
    monkeypatch.setattr("nexus.assistant.api.SpeakerVerifier.verify",
                        lambda self, path: {"matched": False, "score": 0.2, "threshold": 0.75})
    refused = api.post(state, "voice/email", _voice_payload(preview), "local")
    assert refused["external_writes"] == 0 and calls == []
    monkeypatch.setattr("nexus.assistant.api.SpeakerVerifier.verify",
                        lambda self, path: {"matched": True, "score": 0.9, "threshold": 0.75})
    with pytest.raises(AssistantError):
        api.post(state, "voice/email", _voice_payload(preview, to="attacker@example.com"), "local")
    assert calls == []


def test_typed_email_preview_still_requires_exact_one_use_code(state, monkeypatch):
    preview, calls = _preview_email(state, monkeypatch)
    body = {"to": "person@example.com", "subject": "Deploy", "body": "Reviewed body",
            "challenge": preview["challenge"], "confirmation": "wrong"}
    with pytest.raises(AssistantError):
        api.post(state, "email/send/execute", body, "local")
    fresh, calls = _preview_email(state, monkeypatch)
    body.update(challenge=fresh["challenge"], confirmation=fresh["challenge"]["confirmation"])
    assert api.post(state, "email/send/execute", body, "local")["external_writes"] == 1
    assert len(calls) == 1


def test_persistent_monitor_and_brief_schedule(repo: Path):
    monitors = MonitorStore(repo)
    row = monitors.add("server", {"metric": "memory", "threshold": 90}, every_minutes=5)
    assert monitors.due(0)[0]["id"] == row["id"]
    monitors.mark(row["id"], {"ok": True, "alert": False}, now=100)
    assert monitors.due(100) == [] and MonitorStore(repo).list()[0]["last_result"]["ok"] is True
    schedule = BriefSchedule(repo)
    schedule.save(enabled=True, at="08:00", timezone="UTC")
    from datetime import datetime, timezone
    current = datetime(2026, 11, 7, 9, tzinfo=timezone.utc)
    assert schedule.due(current)
    schedule.mark(current)
    assert not schedule.due(current)


def test_failed_monitor_notice_is_not_suppressed(state, monkeypatch):
    from nexus.assistant.proactive import run_monitors
    monkeypatch.setattr("nexus.assistant.proactive.check_monitor",
                        lambda *args, **kwargs: {"ok": False, "alert": True, "status": 503})

    class Dispatcher:
        calls = 0

        def notify(self, *args, **kwargs):
            self.calls += 1
            return {"stored": self.calls > 1}

    dispatcher = Dispatcher()
    specs = [{"name": "site", "kind": "website", "config": {"url": "https://example.com"}}]
    run_monitors(state, specs, dispatcher=dispatcher)
    run_monitors(state, specs, dispatcher=dispatcher)
    assert dispatcher.calls == 2


def test_porcupine_no_match_is_not_truthy():
    detector = PorcupineWakeWord.__new__(PorcupineWakeWord)
    detector.frame_length = 1
    detector.engine = type("Engine", (), {"process": lambda self, pcm: -1})()
    assert detector.detect(b"\x01\x00") is False
    detector.engine = type("Engine", (), {"process": lambda self, pcm: 0})()
    assert detector.detect(b"\x01\x00") is True


def test_cli_memory_and_capability_parser(repo: Path, capsys):
    assert main(["assistant", "-C", str(repo), "memory", "enable", "--confirm", "SAVE PERSONAL MEMORY"]) == 0
    capsys.readouterr()
    assert main(["assistant", "-C", str(repo), "memory", "add", "Prefer Bangla", "--kind", "preference",
                 "--confirm", "ADD PERSONAL MEMORY"]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["kind"] == "preference"
    assert main(["assistant", "-C", str(repo), "memory", "edit", "Use Bangla first", "--id", added["id"],
                 "--kind", "preference", "--confirm", f"EDIT PERSONAL MEMORY {added['id']}"]) == 0
    capsys.readouterr()
    assert main(["assistant", "-C", str(repo), "memory", "status", "--query", "language"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["entries"][0]["text"] == "Use Bangla first"
    assert main(["assistant", "capabilities"]) == 0
    capabilities = json.loads(capsys.readouterr().out)
    assert len(capabilities["groups"]) == 9


def test_systemd_unit_render_and_reviewed_install(repo: Path, monkeypatch, tmp_path: Path):
    from nexus.assistant import systemd
    unit = tmp_path / "nexus-assistant.service"
    monkeypatch.setenv("NEXUS_ASSISTANT_UNIT", str(unit))
    calls = []

    class Completed:
        stdout = b"active\n"

    monkeypatch.setattr(systemd, "_systemctl", lambda *args: calls.append(args) or Completed())
    result = systemd.install(repo)
    assert result["installed"] is True and unit.is_file()
    assert calls == [("daemon-reload",), ("enable", "--now", "nexus-assistant.service")]
    text = unit.read_text()
    assert "Restart=always" in text and "NEXUS_ASSISTANT_SCHEDULER=1" in text
    unit.write_text("previous-working-unit\n")

    def fail_enable(*args):
        if args[0] == "enable":
            raise AssistantError("simulated enable failure")
        return Completed()

    monkeypatch.setattr(systemd, "_systemctl", fail_enable)
    with pytest.raises(AssistantError, match="simulated"):
        systemd.install(repo)
    assert unit.read_text() == "previous-working-unit\n"


def test_pwa_public_assets_and_authenticated_api(repo: Path):
    state = ServerState(repo, token="test-token", quiet=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        for path, marker in [("/manifest.webmanifest", b"Nexus Shared HUD"),
                             ("/assistant-icon.svg", b"<svg"), ("/assistant-sw.js", b"Never cache API")]:
            with urllib.request.urlopen(base + path, timeout=5) as response:
                assert response.status == 200 and marker in response.read()
        request = urllib.request.Request(base + "/api/assistant/overview")
        try:
            urllib.request.urlopen(request, timeout=5)
            raise AssertionError("unauthenticated assistant read must fail")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        request.add_header("X-Nexus-Token", "test-token")
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read())
            assert len(body["capabilities"]["groups"]) == 9
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        state.mission.close()
