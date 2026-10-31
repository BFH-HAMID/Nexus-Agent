"""The Settings hub's one read: honest, redacted, and never a second store.

The page promises three things. It shows where each preference lives, it never
echoes a secret, and it cannot change anything. These tests drive the real
``ServerState`` — including its Workbench SQLite file — and assert exactly
that, plus the HTTP surface around the route.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

import nexus.settingshub as hub
from nexus import __version__
from nexus.server import ServerState, make_handler

TOKEN = "settings-hub-test-token"


@pytest.fixture()
def state(repo: Path):
    created = ServerState(repo, token=TOKEN, quiet=True)
    try:
        yield created
    finally:
        if getattr(created, "workflow_scheduler", None):
            created.workflow_scheduler.stop()
        created.mission.close()


# ── the snapshot is a read ─────────────────────────────────────────────── #


def test_the_summary_describes_the_running_build(repo: Path, state):
    body = hub.summary(state)
    assert body["schema"] == hub.SCHEMA
    assert body["build"] == {
        "version": __version__,
        "demo": bool(state.demo),
        "auth_required": True,
        "roles": len(state.config.roles),
        "agents": len(state.config.agents),
        "pipelines": len(state.config.pipelines),
    }
    assert body["display"]["keys"] == list(hub.DISPLAY_KEYS)
    assert body["display"]["stored_in"] == "this browser"
    assert body["notes"] == list(hub.NOTES)


def test_reading_the_summary_changes_nothing_on_disk(repo: Path, state):
    before = {path: path.stat().st_mtime_ns for path in repo.rglob("*") if path.is_file()}
    hub.summary(state)
    after = {path: path.stat().st_mtime_ns for path in repo.rglob("*") if path.is_file()}
    assert before == after, "opening Settings must not rewrite a workspace file"


def test_no_secret_is_echoed(repo: Path, state):
    marker = "SECRET-WEATHER-KEY-0123456789abcdef"
    state.weatherapi_key = marker
    body = hub.summary(state)
    assert body["world"]["weather_key_present"] is True
    assert marker not in json.dumps(body), "a credential value must never leave the server"
    assert str(repo) not in json.dumps(body), "absolute workspace paths are not exposed"


# ── workspace preferences come from the Workbench store ────────────────── #


def test_the_saved_workspace_preferences_are_reported(repo: Path, state):
    state.workbench.save_settings({
        "language": "bn",
        "local_only": True,
        "layout": {"focus": True, "cards": {"health": {"hidden": True}, "files": {"hidden": False}}},
        "write_paths": ["src"],
    })
    prefs = hub.summary(state)["workspace"]
    assert prefs["available"] is True
    assert prefs["language"] == "bn"
    assert prefs["local_only"] is True
    assert prefs["focus_mode"] is True
    assert prefs["hidden_cards"] == 1
    assert prefs["write_folders"] == 1


def test_an_unreadable_preference_store_is_reported_not_faked(repo: Path, state, monkeypatch):
    def broken():
        raise OSError("dashboard.db is locked")

    monkeypatch.setattr(state.workbench, "settings", broken)
    prefs = hub.summary(state)["workspace"]
    assert prefs == {"available": False, "reason": "preferences_unavailable"}


def test_the_language_falls_back_to_english_only_when_stored_data_is_invalid(repo: Path, state, monkeypatch):
    monkeypatch.setattr(state.workbench, "settings", lambda: {"language": "fr", "layout": "broken"})
    prefs = hub.summary(state)["workspace"]
    assert prefs["language"] == "en"
    assert prefs["focus_mode"] is False
    assert prefs["hidden_cards"] == 0


# ── World switches, without contacting a publisher ─────────────────────── #


def test_the_world_switches_are_read_from_widget_yaml(repo: Path, state):
    (repo / "widget.yaml").write_text(
        yaml.safe_dump({"enabled": False, "sources": ["bbc", "prothom-alo"], "language": "bn"}),
        encoding="utf-8")
    world = hub.summary(state)["world"]
    assert world["available"] is True
    assert world["enabled"] is False
    assert world["sources"] == 2
    assert world["language"] == "bn"
    assert world["weather_key_present"] is False


def test_broken_world_configuration_is_an_honest_unavailable(repo: Path, state):
    (repo / "widget.yaml").write_text("sources: [not-a-feed]\n", encoding="utf-8")
    world = hub.summary(state)["world"]
    assert world["available"] is False
    assert world["reason"] == "configuration_unavailable"


# ── HTTP surface ───────────────────────────────────────────────────────── #


@pytest.fixture()
def live(repo: Path):
    created = ServerState(repo, token=TOKEN, quiet=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(created))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield {"base": f"http://{host}:{port}", "state": created}
    finally:
        if getattr(created, "workflow_scheduler", None):
            created.workflow_scheduler.stop()
        created.mission.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str, token=TOKEN):
    request = urllib.request.Request(url, method="GET")
    if token:
        request.add_header("X-Nexus-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def test_the_route_needs_the_gui_token(live):
    status, _ = _get(live["base"] + "/api/settings/summary", token=None)
    assert status == 401


def test_the_route_returns_the_snapshot(live):
    status, body = _get(live["base"] + "/api/settings/summary")
    assert status == 200
    assert body["ok"] is True
    assert body["data"]["schema"] == 1
    assert body["data"]["build"]["version"] == __version__


def test_unknown_settings_routes_are_404(live):
    for path in ("/api/settings/nonsense", "/api/settings/summary/extra"):
        status, _ = _get(live["base"] + path)
        assert status == 404, path


def test_the_existing_workbench_settings_route_is_unchanged(live):
    status, body = _get(live["base"] + "/api/workbench/settings")
    assert status == 200
    assert body["data"]["language"] in {"en", "bn"}, "the Workbench keeps its own settings route"
