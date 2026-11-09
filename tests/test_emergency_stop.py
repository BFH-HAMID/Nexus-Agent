"""Emergency stop / resume: CLI, latch, Telegram /stop, HUD endpoint, voice phrase."""

from __future__ import annotations

import json
import subprocess
import sys

from nexus.computer_control import EmergencyStop

# --------------------------------------------------------------------------- #
# EmergencyStop latch
# --------------------------------------------------------------------------- #


def test_emergency_stop_latch_defaults_off():
    latch = EmergencyStop()
    assert latch.stopped() is False


def test_emergency_stop_latch_set_and_clear():
    latch = EmergencyStop()
    latch.set(True)
    assert latch.stopped() is True
    latch.set(False)
    assert latch.stopped() is False


def test_emergency_stop_latch_set_true_stays_until_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "home"))
    latch = EmergencyStop()
    latch.set(True)
    # A fresh instance reads the same persisted state.
    latch2 = EmergencyStop()
    assert latch2.stopped() is True
    latch2.set(False)
    assert EmergencyStop().stopped() is False


# --------------------------------------------------------------------------- #
# CLI: nexus stop / nexus resume
# --------------------------------------------------------------------------- #


def _run_cli(*args: str, env=None) -> subprocess.CompletedProcess:
    import os

    return subprocess.run(
        [sys.executable, "-m", "nexus", *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=30,
    )


def test_cli_stop_and_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "home"))
    # Initially not stopped.
    assert EmergencyStop().stopped() is False

    result = _run_cli("stop", "--all")
    assert result.returncode == 0
    assert "EMERGENCY STOP latched" in result.stdout
    assert EmergencyStop().stopped() is True

    result = _run_cli("resume")
    assert result.returncode == 0
    assert "Emergency stop cleared" in result.stdout
    assert EmergencyStop().stopped() is False


def test_cli_stop_json(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "home"))
    result = _run_cli("stop", "--json")
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data == {"stopped": True, "resume": "nexus resume"}

    result = _run_cli("resume", "--json")
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data == {"stopped": False}


# --------------------------------------------------------------------------- #
# Telegram /stop
# --------------------------------------------------------------------------- #


def test_telegram_stop_command(tmp_path, monkeypatch):
    """`/stop` latches the emergency stop via Telegram."""
    import httpx

    from nexus.server import ServerState
    from nexus.telegram.bot import Bot

    TOKEN = "123456789:" + "a" * 30
    PIN = "86420975"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_APPROVAL_PIN", PIN)
    (tmp_path / "telegram.yaml").write_text(
        __import__("yaml").safe_dump({"enabled": True, "user_ids": [123]})
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True, "result": []}))
    )
    studio = ServerState(tmp_path, quiet=True).workbench.studio
    bot = Bot(studio, client=client)

    try:
        # Make sure emergency stop is clear first.
        EmergencyStop().set(False)
        assert EmergencyStop().stopped() is False

        msg = {
            "from": {"id": 123, "is_bot": False, "username": "not-trusted"},
            "chat": {"id": 123, "type": "private"},
            "date": int(__import__("time").time()),
            "text": "/stop",
        }
        result = bot.handle(msg)
        assert "EMERGENCY STOP latched" in result
        assert EmergencyStop().stopped() is True
    finally:
        bot.close()
        client.close()


# --------------------------------------------------------------------------- #
# HUD emergency_stop action
# --------------------------------------------------------------------------- #


def test_hud_emergency_stop_action(tmp_path, monkeypatch):
    """HUD `/api/action/emergency_stop` latches the emergency stop."""
    from nexus.hud.core import HUD
    from nexus.server import ServerState

    studio = ServerState(tmp_path, quiet=True).workbench.studio
    hud = HUD(studio)

    EmergencyStop().set(False)
    assert EmergencyStop().stopped() is False

    result = hud.action("emergency_stop", {})
    assert result["stopped"] is True
    assert EmergencyStop().stopped() is True


# --------------------------------------------------------------------------- #
# CLI subcommand registration
# --------------------------------------------------------------------------- #


def test_cli_stop_resume_registered():
    from nexus.cli import build_parser

    parser = build_parser()
    ns = parser.parse_args(["stop", "--all"])
    assert ns.func.__name__ == "cmd_stop"

    ns = parser.parse_args(["resume"])
    assert ns.func.__name__ == "cmd_resume"
