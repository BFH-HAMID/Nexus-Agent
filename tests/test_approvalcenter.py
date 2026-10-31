"""Approval Center: one reviewed inbox over the three real consent gates.

Every test uses a throwaway workspace (``repo``) and an isolated ``NEXUS_HOME``
from ``conftest.py``; no real task, model call or developer store is touched.
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

import nexus.approvalcenter as center
from nexus.server import ServerState, Session, make_handler
from nexus.webapi import Limiter

TOKEN = "approval-center-test-token"


@pytest.fixture()
def state(repo: Path):
    created = ServerState(repo, token=TOKEN, quiet=True)
    try:
        yield created
    finally:
        created.mission.close()


def _live(state, *, approval_id: str = "live-approval-1", tool: str = "write_file",
          args: dict | None = None, task: str = "Write the notes file") -> Session:
    session = Session(task_id="live-task", task=task, role="engineer")
    session.status = "awaiting_approval"
    session.pending = {
        "proposal": {"available": False},
        "approval_id": approval_id,
        "tool": tool,
        "risk": "write",
        "args": args if args is not None else {"path": "notes.txt", "content": "hello"},
        "preview": "write notes.txt",
        "requested": time.time(),
        "timeout": 600.0,
    }
    state.sessions[session.task_id] = session
    return session


def _action(state, *, identifier: str = "approval-1") -> dict:
    studio = state.workbench.studio
    now = time.time()
    studio.put("jarvis-job", {"task": "Background write", "status": "running", "lease": "lease-1",
                              "created": now, "expires": now + 600, "priority": 5, "attempts": 0,
                              "depth": 0, "active_role": "engineer"}, "job-1")
    row = {
        "job_id": "job-1", "lease": "lease-1", "tool": "write_file",
        "arguments": {"path": "report.txt", "content": "proposed"},
        "args_digest": "a" * 64, "risk": "write", "preview": "write report.txt",
        "proposal": {"available": False}, "before_digest": None, "state": "pending",
        "expires": now + 600, "created": now,
        "confirmation": f"ALLOW ACTION {identifier} " + "a" * 12,
    }
    return studio.put("jarvis-approval", row, identifier)


def _queued(state, task: str = "Queued admission") -> dict:
    from nexus.jarvis.queue import Queue

    return Queue(state.workbench.studio).enqueue(task)


def _ids(body: dict) -> list[str]:
    return [item["reference"] for item in body["items"]]


# ── the inbox is a projection, never an invention ─────────────────────── #


def test_empty_inbox_is_honest(state):
    body = center.inbox(state)
    assert body["items"] == []
    assert body["counts"] == {"live": 0, "action": 0, "queue": 0, "total": 0}
    assert body["errors"] == []
    assert isinstance(body["observed_at"], float)
    assert any("only while its own record" in note for note in body["notes"])


def test_live_request_appears_with_its_real_token_preview_and_deadline(state):
    session = _live(state)
    body = center.inbox(state)
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["source"] == "live" and item["reference"] == session.task_id
    assert item["tool"] == "write_file" and item["risk"] == "write"
    assert item["token"] == "APPROVE ACTION live-approval-1"
    assert item["approval_id"] == "live-approval-1"
    assert item["args"]["path"] == "notes.txt"
    assert item["seconds_left"] is not None and 590 <= item["seconds_left"] <= 600
    assert item["decisions"] == ["approve", "deny"] and item["scopes"] == ["once", "session"]


def test_cancelled_session_and_expired_action_are_not_listed(state):
    session = _live(state)
    session.cancel.set()
    assert center.inbox(state)["items"] == []

    state.sessions.clear()
    row = _action(state)
    row["expires"] = time.time() - 1
    state.workbench.studio.put("jarvis-approval", row, row["id"])
    assert center.inbox(state)["items"] == [], "a past deadline is never shown as pending"


def test_waiting_queue_task_appears_with_the_queue_token(state):
    job = _queued(state)
    body = center.inbox(state)
    item = next(row for row in body["items"] if row["source"] == "queue")
    assert item["reference"] == job["id"]
    assert item["token"] == f"APPROVE QUEUED TASK {job['id']}"
    assert item["decisions"] == ["approve", "reject", "cancel"]


# ── live task decisions go through the existing gate ──────────────────── #


def test_live_approval_requires_the_exact_token_and_records_the_scope(state):
    session = _live(state)
    with pytest.raises(ValueError, match="Type APPROVE ACTION live-approval-1"):
        center.decide(state, {"source": "live", "id": session.task_id, "approval_id": "live-approval-1",
                              "decision": "approve", "confirmation": "yes"})
    body = center.decide(state, {"source": "live", "id": session.task_id,
                                 "approval_id": "live-approval-1", "decision": "approve",
                                 "confirmation": "APPROVE ACTION live-approval-1", "scope": "session"})
    assert body["decided"]["decision"] == "approve" and body["decided"]["scope"] == "session"
    assert session.approval_event.is_set()
    assert session.approval_decision.approved is True
    assert session.approval_decision.scope == "session"


def test_live_deny_needs_no_token(state):
    session = _live(state)
    center.decide(state, {"source": "live", "id": session.task_id,
                          "approval_id": "live-approval-1", "decision": "deny"})
    assert session.approval_decision.approved is False


def test_live_request_is_refused_once_it_is_no_longer_pending(state):
    session = _live(state)
    with pytest.raises(center.Stale, match="newer request"):
        center.decide(state, {"source": "live", "id": session.task_id, "approval_id": "older",
                              "decision": "deny"})
    session.pending = None
    with pytest.raises(center.Stale, match="no longer waiting"):
        center.decide(state, {"source": "live", "id": session.task_id, "approval_id": "live-approval-1",
                              "decision": "deny"})


# ── background actions use the existing one-use store ─────────────────── #


def test_background_action_appears_and_allow_uses_the_store_token(state):
    row = _action(state)
    item = next(row for row in center.inbox(state)["items"] if row["source"] == "action")
    assert item["reference"] == row["id"] and item["token"] == row["confirmation"]
    with pytest.raises(ValueError, match="Exact fresh action approval"):
        center.decide(state, {"source": "action", "id": row["id"], "decision": "allow",
                              "confirmation": "allow"})
    body = center.decide(state, {"source": "action", "id": row["id"], "decision": "allow",
                                 "confirmation": row["confirmation"]})
    assert body["decided"]["decision"] == "allow"
    stored = state.workbench.studio.find("jarvis-approval", row["id"])
    assert stored["state"] == "allowed" and stored["tool"] == "write_file"


def test_background_action_deny_is_recorded_without_a_token(state):
    row = _action(state)
    center.decide(state, {"source": "action", "id": row["id"], "decision": "deny"})
    assert state.workbench.studio.find("jarvis-approval", row["id"])["state"] == "denied"


def test_background_action_that_was_already_consumed_is_stale(state):
    row = _action(state)
    stored = state.workbench.studio.find("jarvis-approval", row["id"])
    stored["state"] = "consumed"
    state.workbench.studio.put("jarvis-approval", stored, row["id"])
    with pytest.raises(center.Stale, match="consumed"):
        center.decide(state, {"source": "action", "id": row["id"], "decision": "deny"})


# ── queued admission goes through queue.review ────────────────────────── #


def test_queue_approval_needs_the_queue_token_and_changes_the_real_job(state):
    job = _queued(state)
    with pytest.raises(ValueError, match="Exact fresh task approval"):
        center.decide(state, {"source": "queue", "id": job["id"], "decision": "approve",
                              "confirmation": "APPROVE QUEUED TASK nope"})
    center.decide(state, {"source": "queue", "id": job["id"], "decision": "approve",
                          "confirmation": f"APPROVE QUEUED TASK {job['id']}"})
    stored = state.workbench.studio.find("jarvis-job", job["id"])
    assert stored["status"] == "approved" and stored["approval_kind"] == "human"


def test_queue_reject_needs_no_token(state):
    job = _queued(state)
    center.decide(state, {"source": "queue", "id": job["id"], "decision": "reject"})
    assert state.workbench.studio.find("jarvis-job", job["id"])["status"] == "rejected"


def test_latched_emergency_stop_refuses_every_grant(state):
    from nexus.computer_control import EmergencyStop

    row = _action(state)
    EmergencyStop().set(True)
    try:
        with pytest.raises(ValueError, match="Emergency stop is latched"):
            center.decide(state, {"source": "action", "id": row["id"], "decision": "allow",
                                  "confirmation": row["confirmation"]})
        assert state.workbench.studio.find("jarvis-approval", row["id"])["state"] == "pending"
        center.decide(state, {"source": "action", "id": row["id"], "decision": "deny"})
    finally:
        EmergencyStop().set(False)


# ── policy, history and redaction ─────────────────────────────────────── #


def test_policy_reports_the_real_gate_facts(state, repo: Path):
    from nexus.permissions import Permissions

    body = center.policy(state)
    assert body["permissions"]["readable"] is True
    assert body["permissions"]["default"] == Permissions(repo).load()["default"]
    assert body["autonomy"]["level"] == "L1"
    assert body["latch"] == {"stopped": False} and body["queue_paused"] is False
    assert body["delivery"]["quiet"] is False
    assert {"run_command", "git", "gh", "office_file", "vscode_open", "browser_action"} <= set(body["always_review"])
    assert body["counts"]["total"] == 0
    assert len(body["not_checked"]) >= 3


def test_policy_reports_an_unreadable_permissions_file_without_guessing(state, repo: Path):
    (repo / "permissions.yaml").write_text("default: [not-a-mode\n")
    body = center.policy(state)
    assert body["permissions"]["readable"] is False
    assert "error" in body["permissions"]


def test_history_lists_only_recorded_decisions(state):
    row = _action(state)
    center.decide(state, {"source": "action", "id": row["id"], "decision": "deny"})
    body = center.history(state, 20)
    assert body["items"], "the decision was recorded in the audit trail"
    first = body["items"][0]
    assert first["source"] == "action" and first["decision"] == "deny"
    assert first["reference"] == row["id"] and first["recorded"] in {"audit", "approval store"}
    assert body["sources"][0]["name"] == "audit"


def test_secrets_never_reach_the_inbox_or_history(state):
    secret = TOKEN
    _live(state, args={"path": "notes.txt", "content": secret}, task=f"Write with {secret}")
    inbox = center.inbox(state)
    assert secret not in json.dumps(inbox)
    assert "[REDACTED" in json.dumps(inbox)


# ── validation and demo refusal ───────────────────────────────────────── #


def test_invalid_decisions_are_refused_before_any_store_is_touched(state):
    _live(state)
    for payload, message in [
        ({"source": "elsewhere", "id": "x", "decision": "deny"}, "source must be"),
        ({"source": "live", "id": "", "decision": "deny"}, "id is required"),
        ({"source": "live", "id": "live-task", "approval_id": "live-approval-1", "decision": "maybe"},
         "decision for live must be"),
        ({"source": "live", "id": "live-task", "approval_id": "live-approval-1", "decision": "deny",
          "scope": "forever"}, "scope must be once or session"),
    ]:
        with pytest.raises(ValueError, match=message):
            center.decide(state, payload)
    assert state.sessions["live-task"].approval_event.is_set() is False


def test_demo_mode_refuses_to_answer_approvals(state):
    state.demo = True
    with pytest.raises(ValueError, match="Live mode is required"):
        center.decide(state, {"source": "action", "id": "x", "decision": "deny"})


# ── HTTP surface ──────────────────────────────────────────────────────── #


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


def test_approval_routes_require_the_gui_token(live):
    for path in ("/api/approvals/inbox", "/api/approvals/policy", "/api/approvals/history"):
        status, body = _call(live["base"] + path, token=None)
        assert status == 401, path
        assert "error" in body
    status, _ = _call(live["base"] + "/api/approvals/decide",
                      {"source": "action", "id": "x", "decision": "deny"}, token=None)
    assert status == 401


def test_inbox_uses_the_structured_envelope_and_is_audited(live, repo: Path):
    from nexus import webapi

    session = _live(live["state"])
    status, body = _call(live["base"] + "/api/approvals/inbox")
    assert status == 200 and body["ok"] is True
    assert body["data"]["items"][0]["reference"] == session.task_id
    assert any(row["action"] == "approval.inbox" for row in webapi.read_audit(repo))


def test_decide_returns_409_for_a_stale_request_and_400_for_bad_input(live):
    status, body = _call(live["base"] + "/api/approvals/decide",
                         {"source": "live", "id": "missing", "approval_id": "x", "decision": "deny"})
    assert status == 409 and body["error"]["code"] == "stale_request"
    status, body = _call(live["base"] + "/api/approvals/decide",
                         {"source": "action", "id": "x", "decision": "maybe"})
    assert status == 400 and body["error"]["code"] == "invalid_request"


def test_decide_answers_a_live_request_over_http(live):
    session = _live(live["state"])
    status, body = _call(live["base"] + "/api/approvals/decide", {
        "source": "live", "id": session.task_id, "approval_id": "live-approval-1",
        "decision": "approve", "confirmation": "APPROVE ACTION live-approval-1"})
    assert status == 200 and body["data"]["decided"]["decision"] == "approve"
    assert session.approval_decision.approved is True


def test_unknown_routes_and_wrong_methods_are_404(live):
    assert _call(live["base"] + "/api/approvals/nope")[0] == 404
    assert _call(live["base"] + "/api/approvals/decide")[0] == 404


def test_approval_reads_are_rate_limited(live):
    live["state"].api_limiter = Limiter(limit=2, window=60)
    assert _call(live["base"] + "/api/approvals/inbox")[0] == 200
    assert _call(live["base"] + "/api/approvals/policy")[0] == 200
    status, body = _call(live["base"] + "/api/approvals/history")
    assert status == 429 and body["error"]["code"] == "rate_limited"
