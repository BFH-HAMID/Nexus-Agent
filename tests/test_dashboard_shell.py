"""Backend contract for the dashboard shell: palette index, guardrails, audit.

These tests use a real HTTP server on an ephemeral port, the same way
``tests/test_server.py`` does, but they never start a task, so no provider or
agent stand-in is required.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from nexus import palette, webapi
from nexus.server import ServerState, make_handler
from nexus.webapi import Limiter


@pytest.fixture()
def shell_server(repo: Path):
    state = ServerState(repo, token="shell-test-token", auto_yes=False, quiet=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host}:{port}", state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str, token: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("X-Nexus-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


# ── palette projection ──────────────────────────────────────────────────── #


def test_palette_projection_is_small_complete_and_labelled(repo: Path):
    from nexus.config import load_config

    config = load_config(repo)
    index = palette.build(config)
    assert len(index["roles"]) == len(config.roles)
    assert len(index["agents"]) == len(config.agents)
    assert len(index["pipelines"]) == len(config.pipelines)
    assert index["default_agent"] == config.default_agent
    assert index["built_at"] > 0
    row = index["roles"][0]
    assert set(row) == {"name", "title", "category", "agent"}
    assert [item["name"] for item in index["agents"]] == sorted(config.agents)
    # the projection must stay markedly smaller than the roster
    roster = len(json.dumps([role.to_dict() for role in config.roles.roles.values()]))
    assert len(json.dumps(index)) * 2 < roster


def test_palette_projection_never_leaks_personas_or_keys(repo: Path):
    from nexus.config import load_config

    index = palette.build(load_config(repo))
    blob = json.dumps(index)
    assert "persona" not in blob
    assert not re.search(r"sk-[A-Za-z0-9]{16,}", blob), "a provider key reached the palette index"
    for agent in index["agents"]:
        assert set(agent) == {"name", "provider", "model", "key_present"}
        assert isinstance(agent["key_present"], bool)


# ── guardrails ──────────────────────────────────────────────────────────── #


def test_error_envelope_keeps_the_legacy_key():
    payload = webapi.failed("rate_limited", "too fast")
    assert payload["ok"] is False
    assert payload["error"] == {"code": "rate_limited", "message": "too fast"}
    assert payload["error_message"] == "too fast"
    assert webapi.ok({"x": 1}) == {"ok": True, "data": {"x": 1}}


def test_limiter_allows_exactly_the_window_then_cools_down():
    limiter = Limiter(limit=3, window=10)
    assert [limiter.allow("a", now=t) for t in (0, 1, 2)] == [True, True, True]
    assert limiter.allow("a", now=3) is False
    assert limiter.retry_after("a", now=3) >= 1
    # a different caller keeps its own bucket
    assert limiter.allow("b", now=3) is True
    # the window slides
    assert limiter.allow("a", now=11) is True


def test_limiter_rejects_invalid_bounds():
    with pytest.raises(ValueError):
        Limiter(limit=0)


def test_audit_records_are_redacted_and_readable(repo: Path, monkeypatch):
    secret = "sk-live-abcdefghijklmnopqrstuvwxyz0123456789"
    event = webapi.audit_api(repo, "palette.index", detail={"note": "token=" + secret, "count": 3})
    assert event["action"] == "palette.index"
    assert event["risk"] == "low"
    path = webapi.audit_path(repo)
    raw = path.read_text(encoding="utf-8")
    assert secret not in raw, "an API key reached the audit file"
    rows = webapi.read_audit(repo)
    assert rows and rows[-1]["action"] == "palette.index"
    assert rows[-1]["detail"]["count"] == 3


def test_audit_rejects_unbounded_input(repo: Path):
    with pytest.raises(ValueError):
        webapi.audit_api(repo, "")
    with pytest.raises(ValueError):
        webapi.audit_api(repo, "x", risk="extreme")


# ── HTTP surface ────────────────────────────────────────────────────────── #


def test_palette_endpoint_requires_the_gui_token(shell_server):
    base, _ = shell_server
    status, body = _get(base + "/api/palette")
    assert status == 401
    assert "error" in body


def test_palette_endpoint_returns_the_projection_and_audits(shell_server, repo: Path):
    base, _ = shell_server
    status, body = _get(base + "/api/palette", token="shell-test-token")
    assert status == 200
    assert body["ok"] is True
    data = body["data"]
    assert data["roles"] and data["agents"] and data["pipelines"]
    assert "built_at" in data
    rows = webapi.read_audit(repo)
    assert any(row["action"] == "palette.index" for row in rows)


def test_palette_endpoint_is_rate_limited_with_the_structured_error(shell_server):
    base, state = shell_server
    state.api_limiter = Limiter(limit=2, window=60)
    assert _get(base + "/api/palette", token="shell-test-token")[0] == 200
    assert _get(base + "/api/palette", token="shell-test-token")[0] == 200
    status, body = _get(base + "/api/palette", token="shell-test-token")
    assert status == 429
    assert body["ok"] is False
    assert body["error"]["code"] == "rate_limited"
    assert "retry" in body["error"]["message"].lower()


def test_palette_endpoint_refuses_to_serve_without_an_audit_trail(shell_server, repo: Path, monkeypatch):
    base, _ = shell_server

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(webapi, "audit_api", broken)
    status, body = _get(base + "/api/palette", token="shell-test-token")
    assert status == 503
    assert body["ok"] is False
    assert body["error"]["code"] == "audit_unavailable"
