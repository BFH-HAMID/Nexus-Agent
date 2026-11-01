"""Mission Control backend: the summary projection and the five-event live feed.

The HTTP tests use a real ephemeral server, exactly like ``test_server.py``, but
they never start a task: everything here is a read of the local records plus a
tiny SSE reader.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from nexus import mission as mission_module
from nexus.mission import EVENTS, Mission, encode
from nexus.server import ServerState, make_handler
from nexus.webapi import Limiter

TOKEN = "mission-test-token"


@pytest.fixture()
def live(repo: Path):
    state = ServerState(repo, token=TOKEN, auto_yes=False, quiet=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield {"base": f"http://{host}:{port}", "state": state, "mission": state.mission}
    finally:
        state.mission.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str, token: str | None = TOKEN):
    request = urllib.request.Request(url)
    if token:
        request.add_header("X-Nexus-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


class Reader:
    """Minimal SSE client: reads named frames until it has enough or times out."""

    def __init__(self, url: str, token: str = TOKEN) -> None:
        self.request = urllib.request.Request(url)
        self.request.add_header("X-Nexus-Token", token)
        self.response = urllib.request.urlopen(self.request, timeout=15)
        self.buffer = b""
        self.frames: list[tuple[str, dict]] = []

    def read(self, wanted: set[str], timeout: float = 12.0, minimum: int = 1) -> list[tuple[str, dict]]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            while b"\n\n" not in self.buffer:
                self.response.fp.raw._sock.settimeout(max(0.2, deadline - time.time()))  # type: ignore[attr-defined]
                chunk = self.response.read1(4096) if hasattr(self.response, "read1") else self.response.read(4096)
                if not chunk:
                    return self.frames
                self.buffer += chunk
            block, self.buffer = self.buffer.split(b"\n\n", 1)
            name, data = None, None
            for line in block.decode().splitlines():
                if line.startswith("event: "):
                    name = line[7:].strip()
                elif line.startswith("data: "):
                    data = line[6:]
            if name and data is not None:
                self.frames.append((name, json.loads(data)))
                if wanted <= {frame[0] for frame in self.frames} and \
                        sum(1 for frame in self.frames if frame[0] in wanted) >= minimum:
                    return self.frames
        return self.frames

    def close(self) -> None:
        self.response.close()


# ── frame format ────────────────────────────────────────────────────────── #


def test_sse_frames_name_the_event_and_carry_json():
    frame = encode("metrics.tick", {"cpu_percent": 12}, 7)
    text = frame.decode()
    assert text.startswith("id: 7\nevent: metrics.tick\ndata: ")
    assert text.endswith("\n\n")
    body = json.loads(text.split("data: ", 1)[1])
    assert body == {"cpu_percent": 12}
    # Bangla and other non-ASCII text must survive the wire format
    assert "মিশন" in encode("alert.new", {"title": "মিশন"}).decode()
    assert mission_module.keepalive().startswith(b":")


def test_only_the_five_contracted_events_can_be_published(repo: Path):
    hub = Mission(ServerState(repo, token=TOKEN, quiet=True))
    assert EVENTS == ("heartbeat", "task.update", "approval.new", "metrics.tick", "alert.new")
    with pytest.raises(ValueError):
        hub.publish("task.created", {})


# ── summary ─────────────────────────────────────────────────────────────── #


def test_summary_is_honest_about_what_it_does_not_know(repo: Path):
    state = ServerState(repo, token=TOKEN, quiet=True)
    summary = state.mission.summary()
    assert summary["schema"] == mission_module.SCHEMA
    assert summary["observed_at"] > 0
    assert summary["build_ms"] >= 0

    daemon = summary["daemon"]
    assert daemon["state"] == "ok"          # the section was read
    assert daemon["status"] in {"running", "idle", "stopped", "killed", "paused", "unknown"}
    assert isinstance(daemon["lock_held"], bool)
    assert daemon["observed_at"] == summary["observed_at"]

    tasks = summary["tasks"]
    assert tasks["state"] == "ok"
    assert all(isinstance(tasks[key], int) for key in ("active", "queued", "failed_24h", "total"))
    assert tasks["recent"] == []

    approvals = summary["approvals"]
    assert approvals["pending"] == 0 and approvals["items"] == []

    agents = summary["agents"]
    assert agents["total"] > 0
    assert agents["reachability_checked"] is False, "reachability is never inferred"

    usage = summary["usage"]
    assert usage["tokens_in"] == 0 and usage["tokens_out"] == 0
    assert usage["cost_usd"] is None
    assert usage["cost_reason"], "a missing price must explain itself"

    resources = summary["resources"]
    assert resources["state"] == "ok"
    assert isinstance(resources["disk_free_bytes"], int)
    assert resources["gpu_reason"], "no GPU means a reason, not a placeholder number"

    health = summary["health"]
    for name in ("server", "stream", "ollama", "github", "gpu", "mcp", "telegram"):
        assert name in health
    assert health["ollama"] == {"state": "unknown", "text": "not checked", "checked_at": None}
    assert health["stream"]["state"] == "off"
    assert summary["alerts"] == []
    assert state.mission.subscribers == 0
    state.mission.close()


def test_summary_marks_every_section_unavailable_when_the_store_fails(repo: Path, monkeypatch):
    state = ServerState(repo, token=TOKEN, quiet=True)

    class Broken:
        def snapshot(self):
            raise RuntimeError("store is corrupt")

    monkeypatch.setattr(state.gui, "operations", lambda: Broken())
    summary = state.mission.summary(force=True)
    for name in ("daemon", "tasks", "approvals", "usage"):
        assert summary[name]["state"] == "unavailable", name
        assert "RuntimeError" in (summary[name]["reason"] or ""), name
    # the local sample still works even when the store does not
    assert summary["resources"]["state"] == "ok"
    assert summary["agents"]["state"] == "ok"
    state.mission.close()


def test_summary_is_cached_between_ticks(repo: Path):
    state = ServerState(repo, token=TOKEN, quiet=True)
    first = state.mission.summary()
    second = state.mission.summary()
    assert first is second, "back-to-back reads must not rebuild the projection"
    third = state.mission.summary(force=True)
    assert third is not first
    state.mission.close()


def test_intervals_come_from_the_environment_and_are_clamped(monkeypatch):
    monkeypatch.setenv("NEXUS_MISSION_TICK", "0.2")
    monkeypatch.setenv("NEXUS_MISSION_HEARTBEAT", "10000")
    monkeypatch.setenv("NEXUS_MISSION_SLOW", "nonsense")

    class Bare:
        """Construction only reads the environment; no state is touched."""

    hub = Mission(Bare())
    assert hub.tick == 0.5, "floored so a request can never spin the sampler"
    assert hub.heartbeat_every == 300.0, "ceiling keeps the heartbeat sane"
    assert hub.slow_every == 60.0, "an unparseable value falls back to the default"


# ── HTTP ────────────────────────────────────────────────────────────────── #


def test_summary_endpoint_requires_the_gui_token(live):
    status, body = _get(live["base"] + "/api/mission/summary", token=None)
    assert status == 401 and "error" in body


def test_summary_endpoint_returns_the_projection_and_audits(live, repo: Path):
    from nexus import webapi

    status, body = _get(live["base"] + "/api/mission/summary")
    assert status == 200 and body["ok"] is True
    data = body["data"]
    assert data["tasks"]["state"] == "ok"
    assert data["health"]["stream"]["state"] == "off"
    rows = webapi.read_audit(repo)
    assert any(row["action"] == "mission.summary" for row in rows)
    # a second poll inside the audit window does not add a second line
    first = len(rows)
    assert _get(live["base"] + "/api/mission/summary")[0] == 200
    assert len(webapi.read_audit(repo)) == first


def test_summary_endpoint_is_rate_limited(live):
    live["state"].api_limiter = Limiter(limit=2, window=60)
    assert _get(live["base"] + "/api/mission/summary")[0] == 200
    assert _get(live["base"] + "/api/mission/summary")[0] == 200
    status, body = _get(live["base"] + "/api/mission/summary")
    assert status == 429
    assert body["error"]["code"] == "rate_limited"


def test_stream_endpoint_requires_the_gui_token(live):
    status, body = _get(live["base"] + "/api/mission/stream", token=None)
    assert status == 401 and "error" in body


def test_stream_pushes_the_four_opening_events_then_an_alert(live):
    url = live["base"] + "/api/mission/stream"
    reader = Reader(url)
    try:
        frames = reader.read({"heartbeat", "task.update", "approval.new", "metrics.tick"})
        names = [name for name, _ in frames]
        assert set(names) >= {"heartbeat", "task.update", "approval.new", "metrics.tick"}
        heartbeat = dict(frames)["heartbeat"]
        assert heartbeat["subscribers"] == 1
        assert heartbeat["health"]["stream"]["state"] == "ok"
        approval = dict(frames)["approval.new"]
        assert approval["first"] is True and approval["pending"] == 0
        metrics = dict(frames)["metrics.tick"]
        assert metrics["observed_at"] > 0 and "cpu_percent" in metrics
        # A source that recovers raises an informational alert; a source that
        # breaks afterwards raises a warning. Both arrive on the same feed.
        now = time.time()
        state_off = {"state": "off", "text": "unreachable", "checked_at": now}
        state_ok = {"state": "ok", "text": "3 models", "checked_at": now}
        live["mission"].inject_slow({"ollama": state_off})     # first reading = baseline
        live["mission"].inject_slow({"ollama": state_ok})      # recovered
        live["mission"].inject_slow({"ollama": state_off})     # broke again
        alerts = [payload for name, payload in reader.read({"alert.new"}, minimum=2) if name == "alert.new"]
        assert [row["severity"] for row in alerts] == ["info", "warn"]
        assert {row["source"] for row in alerts} == {"ollama"}
        assert alerts[0]["detail"] == "3 models"
        assert live["mission"].subscribers == 1
    finally:
        reader.close()
    deadline = time.time() + 5
    while live["mission"].subscribers and time.time() < deadline:
        time.sleep(0.05)
    assert live["mission"].subscribers == 0, "a dropped client must free its slot"


def test_stream_refuses_extra_connections_with_the_structured_error(live, monkeypatch):
    live["state"].api_limiter = Limiter(limit=1, window=60)
    reader = Reader(live["base"] + "/api/mission/stream")
    try:
        reader.read({"heartbeat"})
        status, body = _get(live["base"] + "/api/mission/stream")
        assert status == 429
        assert body["error"]["code"] == "rate_limited"
    finally:
        reader.close()


def test_stream_reports_too_many_subscribers(live, monkeypatch):
    monkeypatch.setattr(mission_module, "MAX_SUBSCRIBERS", 0)
    status, body = _get(live["base"] + "/api/mission/stream")
    assert status == 503
    assert body["error"]["code"] == "too_many_streams"


def test_the_sampler_only_runs_while_somebody_listens(repo: Path, monkeypatch):
    monkeypatch.setenv("NEXUS_MISSION_TICK", "0.5")
    state = ServerState(repo, token=TOKEN, quiet=True)
    hub = state.mission
    assert hub._thread is None
    subscriber = hub.subscribe(instant=False)
    assert subscriber is not None and hub._thread is not None and hub._thread.is_alive()
    hub.unsubscribe(subscriber)
    deadline = time.time() + 5
    while not hub.stopped and time.time() < deadline:
        time.sleep(0.05)
    assert hub.stopped
    hub.close()


def test_a_stalled_reader_drops_old_frames_instead_of_growing(repo: Path):
    hub = Mission(ServerState(repo, token=TOKEN, quiet=True))
    subscriber = mission_module.Subscriber(1)
    for index in range(mission_module.SUBSCRIBER_QUEUE + 10):
        subscriber.push("metrics.tick", {"n": index})
    assert subscriber.dropped == 10
    drained = []
    while True:
        item = subscriber.next(0.01)
        if item is None:
            break
        drained.append(item[1]["n"])
    assert drained[-1] == mission_module.SUBSCRIBER_QUEUE + 9, "the newest frame is the one kept"
    hub.close()


def test_summary_refreshes_its_health_checks_in_the_background(repo: Path):
    state = ServerState(repo, token=TOKEN, quiet=True)
    hub = state.mission
    hub.inject_slow({"ollama": {"state": "off", "text": "unreachable", "checked_at": time.time()}})
    assert hub.summary(force=True)["health"]["ollama"]["text"] == "unreachable"
    hub.close()


def test_alert_transitions_only_fire_on_a_change(repo: Path):
    hub = Mission(ServerState(repo, token=TOKEN, quiet=True))
    now = time.time()
    hub.inject_slow({"github": {"state": "off", "text": "not connected", "checked_at": now}})
    assert hub._alerts() == [], "the first reading is a baseline, not an incident"
    hub.inject_slow({"github": {"state": "ok", "text": "octocat", "checked_at": now}})
    alerts = hub._alerts()
    assert [row["id"] for row in alerts] == ["github-ok"]
    hub.inject_slow({"github": {"state": "ok", "text": "octocat", "checked_at": now}})
    assert len(hub._alerts()) == 1, "a repeating state must not repeat the alert"
    hub.close()


def test_close_tells_open_streams_to_finish():
    class Quiet:
        started = time.time()

        class config:  # noqa: N801 - mirrors the real attribute shape
            agents: dict = {}

    hub = Mission(Quiet())
    subscriber = hub.subscribe(instant=False)
    hub.close()
    item = subscriber.next(1.0)
    assert item is not None
    assert item[1].get("shutdown") is True
    assert hub.subscribers == 0

