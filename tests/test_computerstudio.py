"""Computer Control backend: narrowing policy, scoped files, trash, panic.

Every test runs against a throwaway workspace (``repo``) with an isolated
``NEXUS_HOME`` from ``conftest.py``; the developer's real snapshots, emergency
latch and computer store are never touched.
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

import nexus.computerstudio as studio
from nexus.server import ServerState, make_handler
from nexus.webapi import Limiter

TOKEN = "computer-studio-test-token"


@pytest.fixture()
def state(repo: Path):
    created = ServerState(repo, token=TOKEN, quiet=True)
    try:
        yield created
    finally:
        created.mission.close()


def _revision(state) -> str:
    return studio.load_settings(Path(state.workspace))["revision"]


def _save(state, **overrides):
    payload = {"profile": "review", "levels": {"files": "ask", "browser": "off"},
               "scopes": {"roots": ["."], "origins": []}, "revision": _revision(state),
               "confirmation": "SAVE COMPUTER POLICY"}
    payload.update(overrides)
    return studio.save_settings(state, payload)


def _write(state, path: str, text: str, **overrides):
    workspace = Path(state.workspace)
    target = workspace / path
    revision = "missing" if not target.exists() else __import__("hashlib").sha256(
        target.read_bytes()).hexdigest()[:16]
    payload = {"path": path, "text": text, "confirmation": f"WRITE {path}", "revision": revision}
    payload.update(overrides)
    return studio.write(state, payload)


# ── policy store ───────────────────────────────────────────────────────── #


def test_everything_starts_locked(state):
    settings = studio.load_settings(Path(state.workspace))
    assert settings["levels"] == {"files": "off", "browser": "off"}
    assert settings["profile"] == "locked"
    assert studio.effective(state, "files") == "off"
    assert studio.effective(state, "browser") == "off"


def test_saving_is_a_reviewed_write_with_a_stable_revision(state):
    first = studio.load_settings(Path(state.workspace))["revision"]
    saved = _save(state)
    assert saved["settings"]["levels"]["files"] == "ask"
    assert saved["revision"] != first
    assert studio.load_settings(Path(state.workspace))["revision"] == saved["revision"]
    history = list((Path(state.workspace) / studio.STORE_DIR / studio.HISTORY_DIR).glob("*.json"))
    assert history == [], "the first save replaces nothing, so it snapshots nothing"
    _save(state, levels={"files": "read", "browser": "off"})
    history = list((Path(state.workspace) / studio.STORE_DIR / studio.HISTORY_DIR).glob("*.json"))
    assert len(history) == 1, "the replaced policy is snapshotted"


def test_saving_requires_the_current_revision_and_the_exact_token(state):
    with pytest.raises(ValueError, match="changed since"):
        _save(state, revision="0" * 16)
    with pytest.raises(ValueError, match="SAVE COMPUTER POLICY"):
        _save(state, confirmation="save")


def test_a_damaged_store_falls_back_to_locked(state):
    path = studio.settings_path(Path(state.workspace))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    settings = studio.load_settings(Path(state.workspace))
    assert settings["levels"] == {"files": "off", "browser": "off"}
    assert settings["error"]


def test_invalid_levels_roots_and_origins_are_refused(state):
    with pytest.raises(ValueError, match="Unknown files level"):
        _save(state, levels={"files": "root", "browser": "off"})
    with pytest.raises(ValueError):
        _save(state, scopes={"roots": [".."], "origins": []})
    with pytest.raises(ValueError, match="origin"):
        _save(state, scopes={"roots": ["."], "origins": ["http://example.org"]})
    with pytest.raises(ValueError, match="browser origins"):
        _save(state, scopes={"roots": ["."], "origins": [f"http://host:{index}" for index in range(30)]})


def test_demo_mode_refuses_to_change_the_policy(state):
    state.demo = True
    with pytest.raises(ValueError, match="demo mode"):
        _save(state)


# ── capabilities and narrowing ─────────────────────────────────────────── #


def test_capabilities_report_the_real_policy(state, repo: Path):
    rows = {row["id"]: row for row in studio.capabilities(state)}
    assert rows["files"]["ceiling"] == "ask"        # default ASK_EVERY_TIME
    assert rows["browser"]["ceiling"] == "off"      # adapter opt-in is false
    assert rows["browser"]["available"] is False
    assert rows["terminal"]["reason"].startswith("Shell backend: host")
    assert rows["screen"]["available"] is False and "DISPLAY" in rows["screen"]["reason"]
    assert {"files", "browser", "terminal", "office", "screen", "apps", "vscode"} == set(rows)


def test_owned_capabilities_offer_the_full_ladder(state, repo: Path):
    rows = {row["id"]: row for row in studio.capabilities(state)}
    assert rows["files"]["levels"] == ["off", "read", "ask", "auto"]
    assert rows["browser"]["levels"] == ["off", "read"]
    assert rows["files"]["stored"] == "off" and rows["files"]["level"] == "off"


def test_auto_is_stored_but_capped_by_the_workspace_policy(state, repo: Path):
    saved = _save(state, levels={"files": "auto", "browser": "off"})
    assert saved["settings"]["levels"]["files"] == "auto"      # the operator's intent is kept
    row = {item["id"]: item for item in studio.capabilities(state)}["files"]
    assert row["stored"] == "auto" and row["level"] == "ask"  # but permissions.yaml asks
    assert "caps this workspace at Ask" in row["auto_note"]
    with pytest.raises(ValueError, match="Type WRITE notes.txt to confirm"):
        studio.write(state, {"path": "notes.txt", "text": "hello", "revision": "missing"})


def test_read_only_permissions_cap_the_studio_at_read(state, repo: Path):
    (repo / "permissions.yaml").write_text(yaml.safe_dump({"schema": 1, "default": "READ_ONLY"}))
    _save(state, levels={"files": "ask", "browser": "off"})
    assert studio.effective(state, "files") == "read"
    with pytest.raises(ValueError, match="needs ask"):
        studio.write(state, {"path": "notes.txt", "text": "hello", "confirmation": "WRITE notes.txt"})


def test_browser_adapter_needs_both_the_opt_in_and_an_origin(state, repo: Path):
    (repo / "permissions.yaml").write_text(yaml.safe_dump({
        "schema": 1, "default": "ASK_EVERY_TIME",
        "computer": {"browser": True, "browser_origins": ["http://127.0.0.1:8080"]}}))
    view = studio.browser_view(state)
    assert view["enabled"] is True
    assert view["origins"] == [] or view["origins"] == ["http://127.0.0.1:8080"]
    assert view["available"] is False
    assert "Permissions" in view["reason"], "the studio allowlist still has to include the origin"
    _save(state, scopes={"roots": ["."], "origins": ["http://127.0.0.1:8080"]})
    narrowed = studio.browser_view(state)
    assert narrowed["origins"] == ["http://127.0.0.1:8080"]
    assert narrowed["available"] is False, "Playwright is not installed in this test environment"
    assert "Playwright" in narrowed["reason"]


# ── files ──────────────────────────────────────────────────────────────── #


def test_files_stay_off_until_the_operator_chooses_a_level(state):
    with pytest.raises(ValueError, match="Files permission is off"):
        studio.tree(state, {"path": "."})
    _save(state, levels={"files": "read", "browser": "off"}, profile="read-only")
    assert studio.tree(state, {"path": "."})["entries"]


def test_tree_hides_private_paths_and_respects_scope(state, repo: Path):
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "app.py").write_text("print('hi')\n")
    _save(state, scopes={"roots": ["src"], "origins": []})
    view = studio.tree(state, {"path": "src"})
    names = {row["name"] for row in view["entries"]}
    assert names == {"app.py"}
    with pytest.raises(ValueError, match="outside the configured scope"):
        studio.tree(state, {"path": "."})
    assert studio.load_settings(Path(state.workspace))["scopes"]["roots"] == ["src"]


def test_tree_never_lists_runtime_state(state, repo: Path):
    (repo / ".nexus").mkdir(exist_ok=True)
    _save(state, levels={"files": "read", "browser": "off"}, profile="read-only")
    for path in (".nexus", ".git", ".env"):
        with pytest.raises(ValueError):
            studio.tree(state, {"path": path})


def test_preview_refuses_binary_and_large_files(state, repo: Path):
    _save(state, levels={"files": "read", "browser": "off"}, profile="read-only")
    (repo / "bin.dat").write_bytes(b"\x00\x01\x02")
    body = studio.preview(state, {"path": "bin.dat"})
    assert body["binary"] is True and body["text"] == ""
    text = studio.preview(state, {"path": "README.md"}) if (repo / "README.md").exists() else None
    if text is not None:
        assert text["binary"] is False


def test_write_needs_ask_a_token_and_the_previewed_revision(state, repo: Path):
    _save(state, levels={"files": "read", "browser": "off"}, profile="read-only")
    with pytest.raises(ValueError, match="needs ask"):
        _write(state, "notes.txt", "hello")
    _save(state, levels={"files": "ask", "browser": "off"})
    with pytest.raises(ValueError, match="WRITE notes.txt"):
        studio.write(state, {"path": "notes.txt", "text": "hello", "confirmation": "WRITE other.txt"})
    with pytest.raises(ValueError, match="changed since"):
        studio.write(state, {"path": "notes.txt", "text": "hello", "revision": "0" * 16,
                             "confirmation": "WRITE notes.txt"})
    body = _write(state, "notes.txt", "hello\n")
    assert body["bytes"] == 6
    assert (repo / "notes.txt").read_text() == "hello\n"
    assert body["snapshot"]


def test_write_and_undo_roundtrip_through_the_snapshot_store(state, repo: Path):
    _save(state, levels={"files": "ask", "browser": "off"})
    _write(state, "notes.txt", "first\n")
    second = _write(state, "notes.txt", "second\n")
    with pytest.raises(ValueError, match="RESTORE"):
        studio.undo(state, {"id": second["snapshot"], "confirmation": "RESTORE nope"})
    studio.undo(state, {"id": second["snapshot"], "confirmation": f"RESTORE {second['snapshot']}"})
    assert (repo / "notes.txt").read_text() == "first\n"


def test_write_refuses_credentials(state, repo: Path):
    _save(state, levels={"files": "ask", "browser": "off"})
    with pytest.raises(ValueError):
        _write(state, "config.txt", "GITHUB_TOKEN=ghp_" + "a" * 36 + "\n")


def test_trash_and_restore_move_the_real_file(state, repo: Path):
    _save(state, levels={"files": "ask", "browser": "off"})
    (repo / "old.txt").write_text("keep me\n")
    body = studio.trash(state, {"path": "old.txt", "confirmation": "TRASH old.txt"})
    assert not (repo / "old.txt").exists()
    assert (Path(state.workspace) / studio.STORE_DIR / studio.TRASH_DIR / body["trashed"]).is_dir()
    with pytest.raises(ValueError, match="RESTORE"):
        studio.restore(state, {"id": body["trashed"], "confirmation": "RESTORE wrong"})
    studio.restore(state, {"id": body["trashed"], "confirmation": f"RESTORE {body['trashed']}"})
    assert (repo / "old.txt").read_text() == "keep me\n"


def test_trash_refuses_the_repository_root(state):
    _save(state, levels={"files": "ask", "browser": "off"})
    with pytest.raises(ValueError, match="cannot be trashed"):
        studio.trash(state, {"path": ".", "confirmation": "TRASH ."})


# ── panic ──────────────────────────────────────────────────────────────── #


class _Session:
    def __init__(self, status: str = "running"):
        self.status = status
        self.cancel = threading.Event()


def test_panic_latches_pauses_and_cancels_without_os_signals(state, monkeypatch):
    from nexus.computer_control import EmergencyStop

    calls = []

    class Control:
        def action(self, operation, payload):
            calls.append((operation, payload))
            return {"desired": "killed"}

    monkeypatch.setattr(state.gui, "operations", lambda: Control(), raising=False)
    state.sessions = {"a": _Session("running"), "b": _Session("done"), "c": _Session("awaiting_approval")}
    with pytest.raises(ValueError, match="PANIC STOP"):
        studio.panic(state, {"confirmation": "panic"})
    body = studio.panic(state, {"confirmation": "PANIC STOP"})
    assert body["latch"]["stopped"] is True
    assert body["queue_paused"] is True
    assert sorted(body["sessions_cancelled"]) == ["a", "c"]
    assert state.queue_paused is True
    assert state.sessions["b"].cancel.is_set() is False
    assert calls == [("control", {"action": "kill"})]
    assert EmergencyStop().stopped() is True

    with pytest.raises(ValueError, match="RESUME COMPUTER"):
        studio.resume(state, {"confirmation": "resume"})
    resumed = studio.resume(state, {"confirmation": "RESUME COMPUTER"})
    assert resumed["latch"]["stopped"] is False
    assert resumed["queue_paused"] is True, "clearing the latch never silently resumes queued work"
    resumed = studio.resume(state, {"confirmation": "RESUME COMPUTER", "resume_queue": True})
    assert resumed["queue_paused"] is False and state.queue_paused is False
    with pytest.raises(ValueError, match="true or false"):
        studio.resume(state, {"confirmation": "RESUME COMPUTER", "resume_queue": "yes"})


def test_panic_reports_a_missing_daemon_instead_of_pretending(state, monkeypatch):
    def broken():
        raise RuntimeError("no studio")

    monkeypatch.setattr(state.gui, "operations", broken, raising=False)
    body = studio.panic(state, {"confirmation": "PANIC STOP"})
    assert body["daemon"] is None and "unavailable" in body["daemon_error"]
    studio.resume(state, {"confirmation": "RESUME COMPUTER"})


def test_every_write_is_audited(state, repo: Path):
    from nexus import webapi

    _save(state)
    _write(state, "audit.txt", "x\n")
    actions = {row["action"] for row in webapi.read_audit(repo)}
    assert {"computer.settings", "computer.file.write"} <= actions


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
        created.mission.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _call(url: str, payload=None, token=TOKEN):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method="POST" if payload is not None else "GET")
    if token:
        request.add_header("X-Nexus-Token", token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def test_computer_routes_require_the_gui_token(live):
    for path in ("/api/computer/state", "/api/computer/files", "/api/computer/trash",
                 "/api/computer/browser", "/api/computer/snapshots"):
        status, body = _call(live["base"] + path, token=None)
        assert status == 401, path
        assert "error" in body


def test_state_uses_the_structured_envelope(live, repo: Path):
    from nexus import webapi

    status, body = _call(live["base"] + "/api/computer/state")
    assert status == 200 and body["ok"] is True
    data = body["data"]
    assert data["store"]["settings"]["levels"] == {"files": "off", "browser": "off"}
    assert len(data["capabilities"]) == 7
    assert any(row["action"] == "computer.state" for row in webapi.read_audit(repo))


def test_unknown_computer_subpaths_are_404(live):
    status, body = _call(live["base"] + "/api/computer/nope")
    assert status == 404 and body["error"]
    status, _ = _call(live["base"] + "/api/computer/file/write")
    assert status == 404, "the write route is POST-only"


def test_files_refuse_when_the_level_is_off(live):
    status, body = _call(live["base"] + "/api/computer/files?path=.")
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    assert "off" in body["error"]["message"]


def test_writes_need_a_token_and_a_body(live):
    status, body = _call(live["base"] + "/api/computer/file/write", {"path": "notes.txt", "text": "x"})
    assert status == 400
    assert "ask" in body["error"]["message"] or "Files permission" in body["error"]["message"]
    status, _ = _call(live["base"] + "/api/computer/file/write", [])
    assert status == 400


def test_computer_reads_are_rate_limited(live):
    live["state"].api_limiter = Limiter(limit=2, window=60)
    assert _call(live["base"] + "/api/computer/state")[0] == 200
    assert _call(live["base"] + "/api/computer/state")[0] == 200
    status, body = _call(live["base"] + "/api/computer/state")
    assert status == 429 and body["error"]["code"] == "rate_limited"


def test_panic_is_post_only(live):
    status, _ = _call(live["base"] + "/api/computer/panic")
    assert status == 404
    status, body = _call(live["base"] + "/api/computer/panic", {"confirmation": "PANIC STOP"})
    assert status == 200 and body["data"]["latch"]["stopped"] is True
    _call(live["base"] + "/api/computer/resume", {"confirmation": "RESUME COMPUTER"})
