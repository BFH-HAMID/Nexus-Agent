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

    def run_pipeline(self, name: str, task: str, **kwargs) -> list[RunResult]:
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
    assert "<title>Nexus Agent · Control Deck</title>" in html
    assert "dashboard.js" in html
    assert "data-gui-tab=\"operations\"" in html

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


def test_old_web_interfaces_resolve_to_the_single_main_gui(server):
    for route in ('/panel', '/console', '/hud', '/console/index.html', '/dashboard/panel.html'):
        status, html = server.get(route)
        assert status == 200
        assert '<title>Nexus Agent · Control Deck</title>' in html
        assert 'data-gui-tab="operations"' in html
        assert 'data-gui-tab="world"' in html
        assert '<iframe' not in html


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


# --------------------------------------------------------------------------- #
# learning surfaces over HTTP (skills / brain / recall)
# --------------------------------------------------------------------------- #


def test_brain_endpoint(server):
    status, body = server.get("/api/brain")
    assert status == 200
    assert "skills" in body and "memory" in body and "sessions" in body
    assert body["learning"]["enabled"] in {True, False}
    assert body["skills"]["total"] >= 4  # the bundled skills ship with Nexus


def test_skills_endpoints(server):
    from nexus import skills as skilllib

    status, rows = server.get("/api/skills")
    assert status == 200 and isinstance(rows, list)
    assert any(row["name"] == "add-a-role" for row in rows)

    status, one = server.get("/api/skills/add-a-role")
    assert status == 200 and "## Procedure" in one["body"]

    status, missing = server.get("/api/skills/not-a-skill")
    assert status == 404

    status, created = server.post("/api/skills", {
        "name": "http-created", "description": "A skill created through the GUI.",
        "body": "# Http Created\n\n## Procedure\n1. do\n", "tags": ["gui"],
    })
    assert status == 201, created
    assert skilllib.get("http-created") is not None

    status, duplicate = server.post("/api/skills", {"name": "http-created", "description": "again"})
    assert status == 400


def test_recall_endpoint(server):
    from nexus.agent import RunResult
    from nexus.sessions import SessionStore
    from nexus.types import Message, TextBlock

    SessionStore().record(
        RunResult(text="fixed the flaky test", role="qa", agent="fake", method="keyword", turns=2,
                  tool_calls=2, summary="retried the suite",
                  messages=[Message(role="user", content=[TextBlock(text="flaky test")])]),
        "fix the flaky test",
    )
    status, rows = server.get("/api/recall?q=flaky")
    assert status == 200 and rows
    assert rows[0]["role"] == "qa"

    status, latest = server.post("/api/recall", {"query": "", "limit": 5})
    assert status == 200 and latest


def test_stats_include_the_brain(server):
    status, body = server.get("/api/stats")
    assert status == 200
    assert body["brain"]["skills"] >= 4
    assert "learning" in body["brain"]


def test_dashboard_ships_the_learning_panel(server):
    status, body = server.get("/dashboard")
    assert status == 200
    assert "LEARNING BRAIN" in body
    assert 'id="brainSkills"' in body


def test_local_model_selection(server, monkeypatch):
    from nexus import integrations

    monkeypatch.setattr(integrations, "ollama_models", lambda: {
        "connected": True, "models": ["custom-dolphin:latest"], "url": "http://localhost:11434"})
    assert server.get("/api/ollama")[1]["models"] == ["custom-dolphin:latest"]
    status, _ = server.post("/api/task", {"task": "hello", "local_model": "not-installed"})
    assert status == 400
    status, body = server.post("/api/task", {
        "task": "hello", "local_model": "custom-dolphin:latest", "auto_yes": True})
    assert status == 202
    server.sse(body["task_id"])
    config = server.created[-1].config
    assert config.agents[config.default_agent].model == "custom-dolphin:latest"
    assert config.agents[config.default_agent].fallback is None


def test_integration_routes_require_auth(server):
    server.state.token = "secret"
    for route in ("/api/ollama", "/api/github"):
        assert server.get(route)[0] == 401


def test_setup_controls_in_both_panels(server):
    for route in ("/dashboard", "/panel"):
        status, html = server.get(route)
        assert status == 200
        assert 'id="integrations"' in html
        assert "integrations.js" in html


def test_demo_server_rejects_dashboard_tasks(server):
    server.state.demo = True
    status, body = server.post("/api/task", {"task": "Create hello.txt"})
    assert status == 409
    assert "live mode" in body["error"]
    assert not server.created


def test_task_auto_approve_reaches_agent(server):
    status, body = server.post("/api/task", {"task": "Create hello.txt", "auto_yes": True})
    assert status == 202
    server.sse(body["task_id"])
    assert server.created[-1].auto_yes is True
    assert server.created[-1].approval_callback is None


def test_approval_rejects_wrong_id_and_malformed_decision(server):
    _, body = server.post("/api/task", {"task": "Create hello.txt"})
    task_id = body["task_id"]
    session = server.state.get(task_id)
    server.wait_for(lambda: session.pending is not None)
    pending_id = session.pending["approval_id"]
    assert server.post(f"/api/approve/{task_id}", {
        "approved": True, "scope": "once", "approval_id": "stale-id"})[0] == 409
    assert not session.approval_event.is_set()
    assert server.post(f"/api/approve/{task_id}", {"approved": "false"})[0] == 400
    assert server.post(f"/api/approve/{task_id}", {"approved": True, "scope": "forever"})[0] == 400
    assert server.post(f"/api/approve/{task_id}", {
        "approved": False, "scope": "once", "approval_id": pending_id})[0] == 200
    events = server.sse(task_id)
    assert events[-1]["result"]["ok"] is False
    assert server.post(f"/api/approve/{task_id}", {"approved": True})[0] == 409


def test_approval_timeout_emits_denial_and_clears_pending(server, monkeypatch):
    monkeypatch.setattr(server_module, "APPROVAL_TIMEOUT", 0.02)
    _, body = server.post("/api/task", {"task": "Create hello.txt"})
    events = server.sse(body["task_id"])
    response = next(e for e in events if e["type"] == "approval_response")
    assert response["approved"] is False
    assert response["reason"] == "approval timed out"
    assert server.state.get(body["task_id"]).pending is None


def test_sse_has_no_duplicate_replay_and_broadcasts_to_each_view(server):
    from concurrent.futures import ThreadPoolExecutor

    _, body = server.post("/api/task", {"task": "Create hello.txt"})
    task_id = body["task_id"]
    session = server.state.get(task_id)
    server.wait_for(lambda: session.pending is not None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        views = [pool.submit(server.sse, task_id) for _ in range(2)]
        assert server.post(f"/api/approve/{task_id}", {"approved": True})[0] == 200
        for future in views:
            events = future.result(timeout=10)
            ids = [e["event_id"] for e in events]
            assert ids == sorted(set(ids))
            assert events[-1]["type"] == "done"
            assert sum(e["type"] == "approval_request" for e in events) == 1
            assert sum(e["type"] == "approval_response" for e in events) == 1


def test_sse_resume_after_last_event_id(server):
    _, body = server.post("/api/task", {"task": "Create hello.txt", "auto_yes": True})
    task_id = body["task_id"]
    all_events = server.sse(task_id)
    cursor = all_events[-2]["event_id"]
    request = urllib.request.Request(server.base + f"/api/events/{task_id}", headers={"Last-Event-ID": str(cursor)})
    with urllib.request.urlopen(request, timeout=10) as response:
        wire = ""
        while True:
            line = response.readline().decode()
            wire += line
            if line.startswith("data: ") and json.loads(line[6:])["type"] == "done":
                break
    remaining = [json.loads(line[6:]) for line in wire.splitlines() if line.startswith("data: ")]
    assert [e["event_id"] for e in remaining] == [all_events[-1]["event_id"]]
    assert f"id: {all_events[-1]['event_id']}" in wire


def test_cancelled_run_cannot_emit_later_tool_calls(server):
    _, body = server.post("/api/task", {"task": "Create hello.txt"})
    session = server.state.get(body["task_id"])
    server.wait_for(lambda: session.pending is not None)
    server.post(f"/api/cancel/{session.task_id}", {})
    events = server.sse(session.task_id)
    assert events[-1]["status"] == "cancelled"
    assert not any(e["type"] == "tool_call" for e in events)


def test_pipeline_read_only_override_applies_to_every_step(repo, monkeypatch):
    from nexus.agent import NexusAgent
    from nexus.config import load_config

    agent = NexusAgent(load_config(repo), workspace=repo, auto_yes=True)
    seen = []

    def run(task, *, role=None, read_only=False, **kwargs):
        seen.append(read_only)
        return RunResult(ok=True, summary="read-only check", role=role)

    monkeypatch.setattr(agent, "run", run)
    results = agent.run_pipeline("ship-feature", "Inspect only", read_only=True)
    assert len(results) > 1
    assert all(seen)


def test_redirect_preserves_ui_auth_query(server):
    opener = urllib.request.build_opener(NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as exc:
        opener.open(server.base + "/?token=example", timeout=10)
    assert exc.value.headers["Location"] == "/dashboard?token=example"


def test_workbench_routes_are_authorized_and_settings_persist(server):
    server.state.token = "secret"
    assert server.get("/api/workbench/settings")[0] == 401
    assert server.post("/api/workbench/settings", {"local_only": True})[0] == 401
    assert server.get("/dashboard/workbench.js")[0] == 200
    status, saved = server.post("/api/workbench/settings", {"language": "bn"}, token="secret")
    assert status == 200 and saved["data"]["language"] == "bn"
    assert server.get("/api/workbench/settings", token="secret")[1]["data"]["language"] == "bn"


def test_workbench_plan_mode_and_durable_detail(server):
    status, task = server.post("/api/task", {"task": "Inspect repository", "mode": "plan", "auto_yes": True})
    assert status == 202
    session = server.state.get(task["task_id"])
    assert session.read_only and not session.auto_yes
    server.wait_for(lambda: session.pending is not None)
    server.post(f"/api/approve/{session.task_id}", {"approved": False})
    server.sse(session.task_id)
    server.wait_for(lambda: bool(server.state.workbench.history(task_id=session.task_id)[0].get("result")))
    status, detail = server.get(f"/api/workbench/history?id={session.task_id}")
    assert status == 200
    assert detail["data"][0]["options"]["mode"] == "plan"


def test_workbench_boundary_saved_and_local_only_enforced_before_submission(server):
    assert server.post("/api/workbench/settings", {"write_paths": ["../outside"]})[0] == 400
    assert server.post("/api/workbench/settings", {"local_only": True})[0] == 200
    status, result = server.post("/api/task", {"task": "Inspect repository"})
    assert status == 400 and "Local-only" in result["error"]
    assert not server.created


def test_approval_includes_diff_and_refuses_stale_file(server):
    task_id = server.post("/api/task", {"task": "Create hello.txt"})[1]["task_id"]
    session = server.state.get(task_id)
    server.wait_for(lambda: session.pending is not None)
    assert session.pending["proposal"]["available"]
    assert session.pending["proposal"]["path"] == "x.py"
    (server.state.workspace / "x.py").write_text("externally modified")
    status, body = server.post(f"/api/approve/{task_id}", {"approved": True})
    assert status == 409 and "changed since preview" in body["error"]
    assert session.pending is not None
    assert server.post(f"/api/approve/{task_id}", {"approved": False})[0] == 200
    server.sse(task_id)


def test_stopping_queued_task_removes_it_without_waiting_for_active_run(server):
    server.state.queue_paused = True
    task = server.post("/api/task", {"task": "Inspect hello.txt"})[1]
    session = server.state.get(task["task_id"])
    assert session.task_id in server.state.task_queue
    status, result = server.post(f"/api/cancel/{session.task_id}", {})
    assert status == 200 and result["status"] == "cancelled"
    assert session.task_id not in server.state.task_queue
    assert server.sse(session.task_id)[-1]["status"] == "cancelled"
    assert not server.created


def test_workflow_http_runs_keep_normal_approval_reachable(server):
    base = '/api/workbench/studio/'
    status, body = server.post(base + 'workflow-save', {'definition': {'name':'HTTP approval flow', 'nodes': [
        {'id':'writer', 'kind':'agent', 'role':'backend', 'task':'Write reviewed file'}]}})
    assert status == 200
    ident = body['data']['id']
    status, body = server.post(base + 'workflow-run', {'id':ident, 'confirm':'RUN REVIEWED WORKFLOW'})
    assert status == 200
    run_id = body['data']['run_id']
    def pending():
        with server.state.lock:
            return next((s for s in server.state.sessions.values() if s.options.get('workflow_run') == run_id and s.pending), None)
    try:
        s = server.wait_for(pending)
        status, live = server.get(base + 'items?kind=workflow-live')
        assert status == 200 and live['data'][0]['task_id'] == s.task_id
        assert server.state.workflow_busy is True
        status, _ = server.post('/api/approve/' + s.task_id, {'approved':True, 'approval_id':s.pending['approval_id']})
        assert status == 200
        server.wait_for(lambda: server.state.workbench.studio.find('workflow-run', run_id)['status'] == 'done')
        server.wait_for(lambda: not server.state.workflow_busy)
    finally:
        server.state.workbench.studio.workflows.cancel(run_id)


def test_repository_toolbar_api_uses_existing_auth(server, monkeypatch):
    github = server.state.workbench.repositories.github
    records = [{"id": "example/proj", "repository": "example/proj", "name": "example/proj", "agent_workspace": True}]
    monkeypatch.setattr(github, "catalog", lambda **_: records)
    monkeypatch.setattr(github, "workspace_slug", lambda: "example/proj")
    monkeypatch.setattr(github, "metadata", lambda _: {"full_name": "example/proj", "default_branch": "main", "permissions": {"push": True}})
    server.state.token = 'repository-test-token'
    status, _ = server.get('/api/workbench/repository/list')
    assert status == 401
    status, body = server.get('/api/workbench/repository/list', token='repository-test-token')
    assert status == 200 and body['data'][0]['agent_workspace'] is True
    ident = body['data'][0]['id']
    status, body = server.get('/api/workbench/repository/status?repo=' + ident, token='repository-test-token')
    assert status == 200 and body['data']['branch'] == 'main'


def test_real_command_stdout_sse_then_cancel_reports_process_termination(server, monkeypatch):
    """Script only the model; exercise real approved tool execution, SSE and cancel."""
    import shlex
    import sys

    from nexus.tools import ApprovalGate, PathGuard, ToolContext, build_tools

    def run(self, task, **kwargs):
        ctx = ToolContext(self.workspace, PathGuard(self.workspace), ApprovalGate(mode="auto"),
                          emit=self.emit, cancel_event=self.cancel_event)
        cmd = f"{shlex.quote(sys.executable)} -u -c " + shlex.quote("import time;print('SSE command ready');time.sleep(30)")
        output, error = build_tools(ctx).call("run_command", {"command": cmd})
        return RunResult(text=output, ok=not error, role="backend", agent="fixture")
    monkeypatch.setattr(FakeAgent, "run", run)
    status, body = server.post("/api/task", {"task": "test-only live command"})
    assert status == 202
    ident = body["task_id"]
    events, failures = [], []
    output_seen = threading.Event()
    def subscriber():
        try:
            with urllib.request.urlopen(server.base + "/api/events/" + ident, timeout=10) as response:
                for raw in response:
                    if not raw.startswith(b"data:"):
                        continue
                    event = json.loads(raw[5:])
                    events.append(event)
                    if event["type"] == "tool_output" and "SSE command ready" in event.get("text", ""):
                        output_seen.set()
                    if event["type"] == "done":
                        break
        except Exception as exc:
            failures.append(exc)
    reader = threading.Thread(target=subscriber, daemon=True)
    reader.start()
    try:
        assert output_seen.wait(5)
        assert not any(e["type"] == "tool_result" for e in events), "Output arrived only after completion"
        assert server.post("/api/cancel/" + ident, {})[0] == 200
        reader.join(5)
        assert not reader.is_alive() and failures == []
        assert any(e["type"] == "process" and e["status"] == "cancelled" for e in events)
        assert events[-1]["type"] == "done" and events[-1]["status"] == "cancelled"
    finally:
        session = server.state.get(ident)
        session.cancel.set()
        reader.join(5)


def test_real_loop_prose_only_task_is_blocked_with_saved_context_and_evidence(server, monkeypatch):
    from nexus.agent import NexusAgent
    from tests.conftest import FakeProvider, fake_registry, text_completion
    def factory(config, **kwargs):
        name = config.agent_for_role(config.role('general')).name
        provider = FakeProvider(name, [text_completion('Here are the files created or changed: placeholder code')] * 3)
        return NexusAgent(config, providers=fake_registry(provider, config=config), **kwargs)
    monkeypatch.setattr(server_module, 'NexusAgent', factory)
    status, body = server.post('/api/task', {'task': 'Implement vector search', 'role': 'general'})
    assert status == 202
    ident = body['task_id']
    events = server.sse(ident)
    done = next(e for e in events if e['type'] == 'done')
    assert done['status'] == 'error' and done['result']['ok'] is False
    assert done['result']['execution']['state'] == 'blocked'
    assert done['result']['tool_calls'] == 0 and done['result']['changed_files'] == []
    assert not any('evidence/history update failed' in e.get('message', '') for e in events)
    studio = server.state.workbench.studio
    assert studio.find('context', ident)['task_id'] == ident
    assert studio.find('evidence', 'evidence-' + ident)['result']['execution']['state'] == 'blocked'


def test_main_gui_operations_share_state_auth_and_review_gates(server):
    server.state.token = 'shared-main-gui-test-token'
    assert server.get('/api/operations/state')[0] == 401
    auth = server.state.token
    status, snapshot = server.get('/api/operations/state', token=auth)
    assert status == 200 and snapshot['context']['enabled'] is False
    assert server.get('/api/stats', token=auth)[0] == 200
    status, job = server.post('/api/operations/action/create', {'task': 'Main GUI queue task'}, token=auth)
    assert status == 200 and job['status'] == 'awaiting_approval'
    assert server.state.workbench.studio.find('jarvis-job', job['id'])['task'] == 'Main GUI queue task'
    assert server.post('/api/operations/action/review', {'id': job['id'], 'action': 'approve', 'confirmation': 'wrong'}, token=auth)[0] == 409
    assert server.post('/api/operations/action/review', {'id': job['id'], 'action': 'cancel'}, token=auth)[0] == 200
    status, meta = server.get('/api/countries', token=auth)
    assert status == 200 and len(meta['items']) == 242
    assert server.get('/api/weather?latitude=nan&longitude=0', token=auth)[0] == 409
    assert server.get('/world/../../agents.yaml')[0] in {403, 404}


def test_one_gui_cookie_covers_original_and_new_apis(server):
    server.state.token = 'main-gui-session-token-' + 'x' * 32
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    request = urllib.request.Request(server.base + '/api/session', data=b'{}',
                                    headers={'X-Nexus-Token': server.state.token, 'Content-Type': 'application/json', 'Origin': server.base})
    with opener.open(request) as response:
        cookie = response.headers['Set-Cookie']
        assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie
        assert server.state.token not in cookie
    for route in ('/api/stats', '/api/operations/state', '/api/countries', '/api/workbench/settings', '/api/workbench/configuration'):
        with opener.open(server.base + route) as response:
            assert response.status == 200
            # In-flight polling must not re-create a cookie after Forget login.
            assert response.headers.get('Set-Cookie') is None
    with opener.open(server.base + '/api/session') as response:
        assert 'Max-Age=' in response.headers['Set-Cookie']
    forged = urllib.request.Request(server.base + '/api/operations/state', headers={'Host': 'evil.invalid'})
    with pytest.raises(urllib.error.HTTPError) as exc:
        opener.open(forged)
    assert exc.value.code == 403
    cross = urllib.request.Request(server.base + '/api/operations/action/control', data=b'{"action":"start"}',
                                  headers={'Origin': 'https://evil.invalid','Content-Type':'application/json'})
    with pytest.raises(urllib.error.HTTPError) as exc:
        opener.open(cross)
    assert exc.value.code == 403
    with opener.open(urllib.request.Request(server.base + '/api/session',data=b'{"forget":true}',headers={'Content-Type':'application/json'})) as response:
        assert 'Max-Age=0' in response.headers['Set-Cookie']
    with pytest.raises(urllib.error.HTTPError) as exc:
        opener.open(server.base + '/api/operations/state')
    assert exc.value.code == 401


def test_gui_assets_embed_original_workflows_and_only_one_login(server):
    _, html = server.get('/dashboard')
    for identifier in ('taskInput', 'repo', 'world-widget', 'controls', 'approvals', 'diagnostics'):
        assert identifier in html
    assert 'Private HUD token' not in html and 'id="token"' not in html
    assert server.get('/dashboard/operations.js')[0] == 200
    assert server.get('/dashboard/gui.js')[0] == 200
    assert server.get('/world/world.js')[0] == 200

def test_world_rate_and_busy_budgets_do_not_starve_private_operations(server):
    from nexus.gui_modules import GuiModules
    modules = GuiModules(server.state)
    modules.requests['world'].extend([time.monotonic()] * 120)
    assert modules.call('GET', '/api/countries', {})[0] == 429
    assert modules.call('GET', '/api/operations/state', {})[0] == 200
    modules.requests['world'].clear()
    for _ in range(3):
        assert modules.slots['world'].acquire(blocking=False)
    try:
        assert modules.call('GET', '/api/countries', {})[0] == 429
        assert modules.call('GET', '/api/operations/state', {})[0] == 200
        assert modules._world is None
        assert modules.operations().studio is server.state.workbench.studio
    finally:
        for _ in range(3):
            modules.slots['world'].release()
