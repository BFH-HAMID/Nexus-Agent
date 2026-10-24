"""Phase 5 backend: the local HTTP + SSE server the sidebar/panel talks to."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

import nexus.server as server_module
from nexus.agent import RunResult
from nexus.server import ServerState, make_handler
from nexus.types import Event, EventType

# --------------------------------------------------------------------------- #
# a scripted stand-in for NexusAgent
# --------------------------------------------------------------------------- #


@dataclass
class FakeAgent:
    config: Any = None
    workspace: Path | None = None
    emit: Any = None
    approval_callback: Any = None
    auto_yes: bool = False
    script: list = field(default_factory=list)
    wants_approval: bool = True

    def run(self, task: str, *, role: str | None = None, read_only: bool = False, **kwargs) -> RunResult:
        self.emit(Event(EventType.ROLE, {"role": role or "backend", "agent": "gemini", "method": "manual"}))
        self.emit(Event(EventType.TEXT, {"text": f"working on: {task}"}))
        if self.wants_approval and self.approval_callback is not None:
            from nexus.types import Risk, ToolSpec

            spec = ToolSpec(name="write_file", description="d", input_schema={"type": "object"}, risk=Risk.WRITE)
            decision = self.approval_callback(spec, {"path": "x.py", "content": "x"}, Risk.WRITE, "x.py (1 chars)")
            self.emit(Event(EventType.STATUS, {"message": f"approval={decision.approved}"}))
            if not decision.approved:
                return RunResult(text="", ok=False, stopped_reason="denied by user")
        self.emit(Event(EventType.TOOL_CALL, {"tool": "write_file", "preview": "x.py"}))
        return RunResult(text="all done", role=role or "backend", agent="gemini", turns=1,
                         tool_calls=1, summary="wrote x.py", changed_files=["x.py"],
                         stopped_reason="finish", duration_s=0.01, ok=True)

    def run_pipeline(self, name: str, task: str) -> list[RunResult]:
        self.emit(Event(EventType.STATUS, {"message": f"pipeline '{name}' -> {task}"}))
        return [
            RunResult(text="step done", role=step, agent="gemini", turns=1, tool_calls=0,
                      summary=f"{step} finished", stopped_reason="finish", duration_s=0.01, ok=True)
            for step in ("backend", "qa")
        ]


@pytest.fixture()
def server(repo: Path, monkeypatch):
    """Real HTTP server on an ephemeral port, with a scripted agent."""
    created: list[FakeAgent] = []

    def factory(config, *, workspace=None, emit=None, approval_callback=None, auto_yes=False, **kwargs):
        agent = FakeAgent(config=config, workspace=workspace, emit=emit,
                          approval_callback=approval_callback, auto_yes=auto_yes)
        created.append(agent)
        return agent

    monkeypatch.setattr(server_module, "NexusAgent", factory)
    state = ServerState(repo, token="", auto_yes=False, quiet=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield SimpleServer(f"http://{host}:{port}", state, created)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class SimpleServer:
    def __init__(self, base: str, state: ServerState, created: list[FakeAgent]):
        self.base = base.rstrip("/")
        self.state = state
        self.created = created

    # -- helpers ----------------------------------------------------------- #
    def get(self, path: str, token: str | None = None) -> tuple[int, Any]:
        request = urllib.request.Request(self.base + path)
        if token:
            request.add_header("X-Nexus-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode()
                return response.status, _maybe_json(body)
        except urllib.error.HTTPError as exc:
            return exc.code, _maybe_json(exc.read().decode())

    def post(self, path: str, payload: dict[str, Any], token: str | None = None) -> tuple[int, Any]:
        data = json.dumps(payload).encode()
        request = urllib.request.Request(self.base + path, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("X-Nexus-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, _maybe_json(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, _maybe_json(exc.read().decode())

    def sse(self, task_id: str, until: str = "done", limit: int = 60) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        with urllib.request.urlopen(f"{self.base}/api/events/{task_id}", timeout=15) as response:
            for raw in response:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:].strip())
                events.append(event)
                if event.get("type") == until or len(events) >= limit:
                    break
        return events

    def wait_for(self, predicate, timeout: float = 10.0, interval: float = 0.05):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(interval)
        raise AssertionError("condition not met within timeout")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _maybe_json(body: str):
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #


def test_health(server):
    status, body = server.get("/health")
    assert status == 200
    assert body["ok"] is True
    assert body["roles"] >= 100
    assert "gemini" in body["agents"]
    assert Path(body["workspace"]).is_dir()


def test_roles_and_agents_and_pipelines_endpoints(server):
    status, roles = server.get("/api/roles")
    assert status == 200 and isinstance(roles, list)
    names = {r["name"] for r in roles}
    assert {"pentesting", "backend", "general"} <= names
    pentesting = next(r for r in roles if r["name"] == "pentesting")
    assert pentesting["agent"] == "openrouter-uncensored"

    status, agents = server.get("/api/agents")
    assert status == 200
    assert agents["deepseek"]["tool_mode"] == "native"
    assert agents["openrouter-uncensored"]["tool_mode"] == "text"

    status, pipelines = server.get("/api/pipelines")
    assert "ship-feature" in pipelines
    assert pipelines["ship-feature"]["steps"][0]["role"] == "backend"


def test_panel_is_served(server):
    request = urllib.request.Request(server.base + "/panel")
    with urllib.request.urlopen(request, timeout=10) as response:
        html = response.read().decode()
        assert response.status == 200
    assert "<title>Nexus Agent</title>" in html
    assert "panel.js" in html

    with urllib.request.urlopen(server.base + "/panel/panel.js", timeout=10) as response:
        assert "api/events" in response.read().decode()


def test_panel_cannot_escape_its_directory(server):
    status, _ = server.get("/panel/../../agents.yaml")
    assert status in {403, 404}


def test_unknown_route(server):
    status, body = server.get("/api/nope")
    assert status == 404
    assert "no route" in body["error"]


def test_task_requires_a_task(server):
    status, body = server.post("/api/task", {})
    assert status == 400
    assert body["error"] == "task is required"


def test_full_run_with_approval_over_http(server):
    status, body = server.post("/api/task", {"task": "write x.py", "role": "backend"})
    assert status == 202
    task_id = body["task_id"]

    # wait until the agent thread asks for approval
    session = server.wait_for(lambda: server.state.get(task_id) if server.state.get(task_id).pending else None)
    assert session.pending["tool"] == "write_file"
    assert server.state.get(task_id).status == "awaiting_approval"

    status, _ = server.post(f"/api/approve/{task_id}", {"approved": True, "scope": "once"})
    assert status == 200

    events = server.sse(task_id)
    kinds = [e["type"] for e in events]
    assert "role" in kinds
    assert "text" in kinds
    assert "approval_request" in kinds
    assert "approval_response" in kinds
    assert kinds[-1] == "done"
    final = events[-1]
    assert final["status"] == "done"
    assert final["result"]["summary"] == "wrote x.py"
    assert final["result"]["changed_files"] == ["x.py"]

    status, tasks = server.get("/api/tasks")
    assert any(t["task_id"] == task_id and t["status"] == "done" for t in tasks)


def test_denied_approval_ends_the_run_as_failed(server):
    _, body = server.post("/api/task", {"task": "write x.py"})
    task_id = body["task_id"]
    server.wait_for(lambda: server.state.get(task_id).pending)
    server.post(f"/api/approve/{task_id}", {"approved": False})
    events = server.sse(task_id)
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "error"
    assert events[-1]["result"]["ok"] is False


def test_approve_without_a_pending_request_conflicts(server):
    status, body = server.post("/api/approve/deadbeef", {"approved": True})
    assert status == 409


def test_cancel_sets_the_status(server):
    _, body = server.post("/api/task", {"task": "write x.py"})
    task_id = body["task_id"]
    server.wait_for(lambda: server.state.get(task_id).pending)
    status, cancelled = server.post(f"/api/cancel/{task_id}", {})
    assert status == 200 and cancelled["status"] == "cancelled"
    events = server.sse(task_id)
    assert events[-1]["type"] == "done"


def test_sse_replays_history_for_a_finished_task(server):
    _, body = server.post("/api/task", {"task": "quick"})
    task_id = body["task_id"]
    server.wait_for(lambda: server.state.get(task_id).pending)
    server.post(f"/api/approve/{task_id}", {"approved": True})
    server.wait_for(lambda: server.state.get(task_id).status == "done")
    events = server.sse(task_id)
    assert events[-1]["type"] == "done"
    assert len(events) > 2  # replayed, not empty


def test_events_for_unknown_task(server):
    request = urllib.request.Request(server.base + "/api/events/nope")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=5)
    assert exc.value.code == 404


def test_token_is_enforced_when_set(repo: Path, monkeypatch):
    monkeypatch.setattr(server_module, "NexusAgent", lambda **kw: FakeAgent(**{k: v for k, v in kw.items() if k in FakeAgent.__dataclass_fields__}))
    state = ServerState(repo, token="s3cret", quiet=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        secured = SimpleServer(base, state, [])
        status, body = secured.get("/health")
        assert status == 401 and body["error"]
        status, body = secured.get("/health", token="s3cret")
        assert status == 200 and body["ok"]
        status, _ = secured.post("/api/task", {"task": "x"}, token="wrong")
        assert status == 401
        # query-string token works for EventSource, which cannot set headers
        with urllib.request.urlopen(f"{base}/health?token=s3cret", timeout=5) as response:
            assert response.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_auto_yes_server_never_blocks(repo: Path, monkeypatch):
    created: list[FakeAgent] = []

    def factory(config, *, workspace=None, emit=None, approval_callback=None, auto_yes=False, **kwargs):
        agent = FakeAgent(config=config, emit=emit, approval_callback=approval_callback, auto_yes=auto_yes)
        created.append(agent)
        return agent

    monkeypatch.setattr(server_module, "NexusAgent", factory)
    state = ServerState(repo, auto_yes=True, quiet=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        client = SimpleServer(f"http://{host}:{port}", state, created)
        _, body = client.post("/api/task", {"task": "go"})
        task_id = body["task_id"]
        events = client.sse(task_id)
        assert events[-1]["type"] == "done"
        assert created[0].approval_callback is None
        assert created[0].auto_yes is True
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_stats_endpoint_describes_the_deck(server):
    status, stats = server.get("/api/stats")
    assert status == 200
    assert stats["version"]
    assert Path(stats["workspace"]).is_dir()
    assert stats["roles"] >= 100
    assert isinstance(stats["categories"], dict) and "development" in stats["categories"]
    assert stats["default_agent"] == "gemini"
    assert stats["agents"]["gemini"]["provider"] == "gemini"
    assert stats["agents"]["hermes"]["optional"] is True
    assert stats["agents"]["gemini"]["fallback"] == "hermes" or stats["agents"]["gemini"]["fallback"]
    assert set(stats["keys"]) == {"gemini", "deepseek", "openrouter"}
    assert "configured" in stats["keys"]["gemini"]
    assert "ship-feature" in stats["pipelines"]
    assert stats["settings"]["auto_route"] in (True, False)
    assert stats["sessions"]["total"] >= 0
    assert set(stats["tokens"]) == {"in", "out"}
    assert stats["auth_required"] is False


def test_dashboard_is_served_with_its_assets(server):
    status, html = server.get("/dashboard")
    assert status == 200
    assert "Nexus Agent" in html and "/dashboard/dashboard.js" in html
    for asset, needle in (("/dashboard/dashboard.css", "--neon"),
                          ("/dashboard/dashboard.js", "EventSource")):
        status, body = server.get(asset)
        assert status == 200, asset
        assert needle in body
    # /gui is an alias of the same deck, and / redirects to it
    assert server.get("/gui")[0] == 200
    opener = urllib.request.build_opener(NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        opener.open(server.base + "/", timeout=10)
    assert excinfo.value.code == 302
    assert excinfo.value.headers["Location"] == "/dashboard"


def test_simulation_console_is_served_and_self_contained(server):
    status, html = server.get("/console")
    assert status == 200
    assert "NEXUS AGENT" in html
    assert "SIMULATION · NOT A LIVE BACKEND" in html
    # one file: no external libraries, no CDNs, no images
    for forbidden in ("http://", "https://", "<link", "@import", "src="):
        assert forbidden not in html, forbidden
    assert "<script>" in html and "<style>" in html
    # the /console/ asset form is served from the panel dir
    assert server.get("/console/index.html")[0] == 200


def test_dashboard_assets_never_need_a_token(repo: Path, monkeypatch):
    monkeypatch.setattr(server_module, "NexusAgent", lambda *a, **k: FakeAgent())
    state = ServerState(repo, token="s3cret", auto_yes=False, quiet=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    helper = SimpleServer(f"http://{host}:{port}", state, [])
    try:
        assert helper.get("/dashboard")[0] == 200
        assert helper.get("/dashboard/dashboard.css")[0] == 200
        assert helper.get("/dashboard/dashboard.js")[0] == 200
        assert helper.get("/api/stats")[0] == 401
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_memory_endpoint_returns_role_notes(server):
    status, body = server.get("/api/memory/backend")
    assert status == 200
    assert body["role"] == "backend"
    assert body["path"].endswith(".md")
    assert body["enabled"] in (True, False)
    assert isinstance(body["text"], str)
    assert server.get("/api/memory/does-not-exist")[0] == 404


def test_pipeline_post_is_validated(server):
    status, body = server.post("/api/task", {"task": "ship it", "pipeline": "nope"})
    assert status == 400
    assert "nope" in json.dumps(body)


def test_pipeline_post_runs_every_step(server):
    status, body = server.post("/api/task", {"task": "ship it", "pipeline": "ship-feature"})
    assert status == 202 and body["task_id"]
    events = server.sse(body["task_id"])
    done = [e for e in events if e["type"] == "done"][-1]
    steps = done["result"]["steps"]
    assert [s["role"] for s in steps] == ["backend", "qa"]
    assert all(s["agent"] == "gemini" for s in steps)
    snapshot = server.wait_for(lambda: server.state.get(body["task_id"]).snapshot())
    assert snapshot["pipeline"] == "ship-feature"
    assert done["result"]["pipeline"] == "ship-feature"


def test_port_is_free_helper():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        assert probe.getsockname()[1] > 0
