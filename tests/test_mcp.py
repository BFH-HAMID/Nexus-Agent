"""Real child JSON-RPC + local HTTP fixtures; no hosted service/account access."""

import json
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

pytest.importorskip("jsonschema")

from nexus.extensions.catalog import Catalog
from nexus.extensions.tools import invoke, register
from nexus.extensions.transport import VERSION, Session
from nexus.permissions import Permissions
from nexus.tools import ApprovalDecision, ApprovalGate, PathGuard, ToolContext, ToolRegistry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def extension(repo):
    directory = repo / "plugins"
    directory.mkdir()
    shutil.copyfile(ROOT / "examples/mcp/plugins/word_count.py", directory / "word_count.py")
    shutil.copyfile(ROOT / "examples/mcp/plugins/words.mcp.json", directory / "words.mcp.json")
    (repo / "mcp.yaml").write_text(yaml.safe_dump({"enabled": True}))
    gate = ApprovalGate(mode="auto", callback=lambda *args: ApprovalDecision(True))
    gate.permissions = Permissions(repo)
    ctx = ToolContext(workspace=repo, guard=PathGuard(repo), gate=gate)
    registry = ToolRegistry(ctx)
    register(ctx, registry)
    return ctx, registry


def test_discovery_and_hot_reload_execute_no_code(extension, monkeypatch):
    ctx, registry = extension
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("discovery cannot execute"))
    first = Catalog(ctx.workspace).load()["words"]["digest"]
    file = ctx.workspace / "plugins/words.mcp.json"
    data = json.loads(file.read_text())
    data["server"]["tools"][0]["description"] = "Updated declaration"
    file.write_text(json.dumps(data))
    assert Catalog(ctx.workspace).load()["words"]["digest"] != first
    assert registry.has("mcp_words_count")


def test_real_stdio_plugin_runs_only_after_fresh_gate_and_returns_untrusted_data(extension):
    ctx, registry = extension
    reviews = []
    ctx.gate.callback = lambda *args: reviews.append(args) or ApprovalDecision(True, scope="always")
    for _ in range(2):
        text, failed = registry.call("mcp_words_count", {"text": "one two three"})
        assert not failed, text
        data = json.loads(text)
        assert data["untrusted"] and data["result"]["content"][0]["text"] == "3"
    assert len(reviews) == 2
    assert str(sys.executable) in reviews[0][3] and "manifest_sha256" in reviews[0][3]
    assert Permissions(ctx.workspace).load()["default"] == "AUTO_ALLOW"


def test_auto_allow_and_saved_decisions_never_launch_without_fresh_human(extension, monkeypatch):
    import subprocess

    ctx, registry = extension
    (ctx.workspace / "permissions.yaml").write_text("default: AUTO_ALLOW\n")
    ctx.gate.always_allow.add("mcp_words_count")
    ctx.gate.callback = None
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("cannot launch"))
    assert registry.call("mcp_words_count", {"text": "word"})[1]


def test_readonly_refuses_capability_before_connect(extension, monkeypatch):
    import subprocess

    ctx, registry = extension
    (ctx.workspace / "permissions.yaml").write_text("default: READ_ONLY\n")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("no child"))
    assert registry.call("mcp_words_count", {"text": "word"})[1]


def test_changed_manifest_invalidates_previously_registered_tool(extension, monkeypatch):
    import subprocess

    ctx, registry = extension
    (ctx.workspace / "mcp.yaml").write_text("enabled: false\n")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("no child"))
    text, failed = registry.call("mcp_words_count", {"text": "word"})
    assert failed and "uncertain" in text


def test_schema_failure_and_secret_input_never_connect(extension, monkeypatch):
    import subprocess

    ctx, registry = extension
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "a" * 36)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("no child"))
    assert registry.call("mcp_words_count", {"text": 1})[1]
    assert registry.call("mcp_words_count", {"text": "ghp_" + "a" * 36})[1]


def test_persistent_budgets_survive_new_registries(extension):
    ctx, _ = extension
    file = ctx.workspace / "plugins/words.mcp.json"
    data = json.loads(file.read_text())
    data["server"]["rate_per_minute"] = 1
    file.write_text(json.dumps(data))
    first = ToolRegistry(ctx)
    register(ctx, first)
    assert not first.call("mcp_words_count", {"text": "one"})[1]
    fresh = ToolRegistry(ctx)
    register(ctx, fresh)
    assert fresh.call("mcp_words_count", {"text": "two"})[1]


def test_real_child_timeout_and_oversized_output_are_reaped(extension):
    ctx, _ = extension
    for code in (
        "import time; time.sleep(60)",
        'import sys; sys.stdout.write("x"*100000); sys.stdout.flush(); import time; time.sleep(60)',
    ):
        cfg = Catalog(ctx.workspace).load()["words"]["config"]
        cfg.update(command=[sys.executable, "-I", "-c", code], timeout_seconds=1)
        session = Session(ctx.workspace, cfg, lambda: None)
        started = time.monotonic()
        with pytest.raises((ValueError, BrokenPipeError)):
            with session:
                session.exchange("initialize")
        assert time.monotonic() - started < 5 and session.process.poll() is not None


def test_child_environment_is_not_ambient_key_dump(extension, monkeypatch):
    ctx, _ = extension
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "a" * 36)
    cfg = Catalog(ctx.workspace).load()["words"]["config"]
    code = (
        'import os,json,sys; r=json.loads(sys.stdin.readline()); print(json.dumps({"jsonrpc":"2.0","id":r["id"],'
        '"result":{"leaked":"GITHUB_TOKEN" in os.environ}}),flush=True); sys.stdin.read()'
    )
    cfg["command"] = [sys.executable, "-I", "-c", code]
    with Session(ctx.workspace, cfg, lambda: None) as session:
        assert session.exchange("initialize")["leaked"] is False


def test_real_http_json_and_sse_transports_and_no_redirect(extension):
    ctx, _ = extension
    schema = Catalog(ctx.workspace).load()["words"]["config"]["tools"][0]["inputSchema"]
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if "id" not in request:
                self.send_response(202)
                self.end_headers()
                return
            if request["method"] == "initialize":
                result = {"protocolVersion": VERSION}
            elif request["method"] == "tools/list":
                result = {"tools": [{"name": "count", "inputSchema": schema}]}
            else:
                result = {"content": [{"type": "text", "text": "actual local HTTP fixture"}]}
            raw = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})
            sse = request["method"] == "tools/call"
            body = ("data: " + raw + "\n\n" if sse else raw).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        file = ctx.workspace / "plugins/words.mcp.json"
        data = json.loads(file.read_text())
        data["server"].pop("command")
        data["server"].update(transport="http", url=f"http://127.0.0.1:{server.server_port}/mcp")
        file.write_text(json.dumps(data))
        entry = Catalog(ctx.workspace).load()["words"]
        result = json.loads(invoke(ctx, entry, entry["config"]["tools"][0], {"text": "one"}))
        assert result["untrusted"] and "actual local HTTP fixture" in str(result)
        assert [r["method"] for r in requests] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
            "tools/call",
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.mark.parametrize(
    "change",
    [
        {"risk": "read"},
        {"transport": "shell"},
        {"env": {"KEY": "inline-credential"}},
        {"command": ["python", "-c", "bad"]},
        {"transport": "http", "url": "https://u:p@example.com/mcp"},
    ],
)
def test_unsafe_or_unreviewed_manifest_fields_refused(extension, change):
    ctx, _ = extension
    file = ctx.workspace / "plugins/words.mcp.json"
    value = json.loads(file.read_text())
    value["server"].update(change)
    file.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        Catalog(ctx.workspace).load()


def test_server_extra_capabilities_cannot_change_registered_surface(extension):
    ctx, registry = extension
    assert registry.names() == ["mcp_words_count"]
    assert not registry.has("mcp_words_shell")


def test_http_redirect_is_not_followed(extension, monkeypatch):
    import httpx
    ctx, _ = extension
    cfg = Catalog(ctx.workspace).load()['words']['config']
    cfg.update(transport='http', url='http://127.0.0.1:9001/mcp', headers={})
    seen = []
    native = httpx.Client
    def reply(request):
        seen.append(request)
        return httpx.Response(307, headers={'Location': 'https://unapproved.invalid/mcp'})
    monkeypatch.setattr(httpx, 'Client', lambda **kw: native(transport=httpx.MockTransport(reply), **kw))
    with pytest.raises(ValueError):
        with Session(ctx.workspace, cfg, lambda: None) as session:
            session.exchange('initialize')
    assert len(seen) == 1 and seen[0].url.host == '127.0.0.1'


def test_human_only_review_context_not_sent_to_model(extension):
    _, registry = extension
    spec = registry.specs()[0]
    assert spec.approval_context['endpoint'][0] == sys.executable
    assert 'approval_context' not in json.dumps(spec.to_openai())
    assert 'approval_context' not in json.dumps(spec.to_anthropic())


def test_reference_values_masked_before_json_escaping():
    from nexus.extensions.tools import redact_output
    credential = 'sensitive"value\\withquotes'
    result = redact_output({'content': [{'text': credential}]}, [credential])
    serialized = json.dumps(result)
    assert credential not in serialized and 'sensitive' not in serialized
    assert result['content'][0]['text'] == '[REDACTED_SECRET]'


def test_entry_thread_failure_reaps_already_started_child(extension, monkeypatch):
    ctx, _ = extension
    cfg = Catalog(ctx.workspace).load()['words']['config']
    cfg['command'] = [sys.executable, '-I', '-c', 'import time; time.sleep(60)']
    def fail(*args, **kwargs):
        raise RuntimeError('cannot start reader thread')
    monkeypatch.setattr(threading.Thread, 'start', fail)
    session = Session(ctx.workspace, cfg, lambda: None)
    with pytest.raises(RuntimeError):
        with session:
            pytest.fail('entry must fail closed')
    assert session.process.poll() is not None


def test_entry_file_change_revokes_prebuilt_tool(extension):
    ctx, registry = extension
    before = Catalog(ctx.workspace).load()['words']['digest']
    script = ctx.workspace / 'plugins' / 'word_count.py'
    script.write_text(script.read_text() + '\n# operator code change\n')
    after = Catalog(ctx.workspace).load()['words']['digest']
    assert before != after
    output, error = registry.call('mcp_words_count', {'text': 'must not execute changed code'})
    assert error and 'uncertain' in output
