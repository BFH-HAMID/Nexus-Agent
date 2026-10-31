"""Alerts & channels: notices, delivery, preferences, Telegram and the HTTP surface.

Everything runs offline on the throwaway ``repo`` workspace. Notices are written
through the same ``studio`` table the dashboard uses, so the tests exercise the
real store rather than a stand-in.
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

import nexus.channels as ch
from nexus.jarvis.notifications import native_available
from nexus.server import ServerState, make_handler

TOKEN = "channels-page-test-token"


@pytest.fixture()
def state(repo: Path):
    created = ServerState(repo, token=TOKEN, quiet=True)
    try:
        yield created
    finally:
        if getattr(created, "workflow_scheduler", None):
            created.workflow_scheduler.stop()
        created.mission.close()


def _notice(state, title="Deploy the site", state_name="done", kind="run", detail="worker · 12 s"):
    return ch.record(state, kind, state_name=state_name, title=title, detail=detail, task_id="task-1")


def _save(state, **parts):
    view = ch.policy_view(state.workspace)
    return ch.save_policy(state, {"revision": view["revision"], **parts})


def _save_delivery(state, **parts):
    view = ch.delivery_view(state)
    return ch.save_delivery(state, {"confirmation": ch.SAVE_TOKEN, "revision": view["revision"], **parts})


# ── preferences ───────────────────────────────────────────────────────── #


def test_reading_never_creates_a_store(repo: Path):
    view = ch.policy_view(repo)
    assert view["revision"] == ""
    assert view["file_exists"] is False
    assert view["policy"]["voice"]["enabled"] is False
    assert view["policy"]["mobile"] == {"lite": False, "cards": True, "font_scale": 1.0}
    assert not ch.store_dir(repo).exists(), "a read must not write"


def test_preferences_need_the_current_revision(state):
    view = ch.policy_view(state.workspace)
    with pytest.raises(ValueError, match="changed since you read"):
        ch.save_policy(state, {"revision": "stale", "mobile": {"lite": True}})
    saved = _save(state, mobile={"lite": True})
    assert saved["saved"] is True
    assert saved["policy"]["mobile"]["lite"] is True
    assert saved["revision"] != view["revision"]
    assert ch.policy_path(state.workspace).stat().st_mode & 0o777 == 0o600


def test_a_preference_that_could_not_work_is_refused(state):
    for body, message in (
        ({"mobile": {"font_scale": 3}}, "font_scale"),
        ({"mobile": {"nonsense": 1}}, "Unknown mobile key"),
        ({"voice": {"rate": 4}}, "rate"),
        ({"voice": {"pitch": -1}}, "pitch"),
        ({"voice": {"read": "everything"}}, "read"),
        ({"voice": {"enabled": "yes"}}, "true or false"),
        ({"voice": "loud"}, "must be an object"),
        ({"nonsense": 1}, "Unknown preference key"),
    ):
        with pytest.raises(ValueError, match=message):
            _save(state, **body)


def test_an_unreadable_preference_file_is_reported_not_guessed(state):
    path = ch.policy_path(state.workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    view = ch.policy_view(state.workspace)
    assert "not valid JSON" in view["error"]
    assert view["policy"]["mobile"]["lite"] is False


# ── notices ───────────────────────────────────────────────────────────── #


def test_a_recorded_run_is_one_unread_notice(state):
    written = _notice(state)
    assert written and written["kind"] == "run"
    body = ch.inbox(state)
    assert body["digest"]["total"] == 1 and body["digest"]["unread"] == 1
    assert body["digest"]["states"] == {"done": 1}
    item = body["items"][0]
    assert item["title"] == "Deploy the site"
    assert item["detail"] == "worker · 12 s"
    assert item["read"] is False
    assert item["source"] == "dashboard"


def test_a_notice_is_redacted_before_it_is_stored(state):
    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
    ch.record(state, "run", state_name="done", title=f"Rotate {secret}", detail=f"key {secret}")
    item = ch.inbox(state)["items"][0]
    assert secret not in item["title"] and secret not in item["detail"]
    assert "sk-ant" in item["detail"] or "[redacted" in item["detail"].lower()


def test_notices_are_newest_first_and_capped(state):
    for index in range(5):
        ch.record(state, "run", state_name="done", title=f"Job {index}")
        time.sleep(0.01)
    items = ch.inbox(state)["items"]
    assert [item["title"] for item in items] == [f"Job {index}" for index in reversed(range(5))]
    assert ch.inbox(state, 2)["digest"]["total"] == 2


def test_the_store_keeps_at_most_its_limit(state, monkeypatch):
    monkeypatch.setattr(ch, "MAX_NOTICES", 3)
    for index in range(6):
        ch.record(state, "run", state_name="done", title=f"Job {index}")
    titles = [item["title"] for item in ch.inbox(state)["items"]]
    assert titles == ["Job 5", "Job 4", "Job 3"]
    assert len(state.workbench.studio.items("jarvis-notice")) == 3


def test_a_failed_alert_never_fails_the_run(state, monkeypatch):
    def broken(*_args, **_kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(state.workbench.studio, "put", broken)
    assert ch.record(state, "run", state_name="error", title="Boom") is None
    assert ch.record(state, "nonsense", state_name="done", title="ignored") is None


def test_acknowledging_marks_only_known_notices(state):
    first = _notice(state, "One")
    _notice(state, "Two")
    body = ch.acknowledge(state, {"ids": [first["id"], "not-a-notice"]})
    assert body["acknowledged"] == 1
    assert body["missing"] == ["not-a-notice"]
    assert body["digest"]["unread"] == 1
    with pytest.raises(ValueError, match="up to 200"):
        ch.acknowledge(state, {"ids": [str(index) for index in range(201)]})
    assert ch.acknowledge(state, {})["acknowledged"] == 0


def test_the_inbox_reports_a_broken_store_instead_of_raising(state, monkeypatch):
    def broken(_self):
        raise ValueError("no such table")

    monkeypatch.setattr("nexus.jarvis.inbox.Inbox.items", broken)
    body = ch.inbox(state)
    assert body["items"] == [] and "unavailable" in body["error"]


# ── delivery ──────────────────────────────────────────────────────────── #


def test_delivery_needs_its_token_and_a_current_revision(state):
    with pytest.raises(ValueError, match="Type SAVE ALERT POLICY"):
        ch.save_delivery(state, {"transport": "off", "revision": ch.delivery_view(state)["revision"]})
    with pytest.raises(ValueError, match="changed since you read"):
        ch.save_delivery(state, {"confirmation": ch.SAVE_TOKEN, "revision": "stale"})
    with pytest.raises(ValueError, match="off, local or native"):
        _save_delivery(state, transport="carrier-pigeon")
    with pytest.raises(ValueError, match="whole number of minutes"):
        _save_delivery(state, dnd_minutes=5000)
    with pytest.raises(ValueError, match="true or false"):
        _save_delivery(state, quiet="yes")


def test_delivery_round_trips_through_the_jarvis_policy(state):
    before = ch.delivery_view(state)["revision"]
    saved = _save_delivery(state, transport="local", quiet=True, dnd_minutes=30)
    assert saved["saved"] is True
    assert saved["policy"]["transport"] == "local"
    assert saved["policy"]["quiet"] is True
    assert 0 < saved["policy"]["dnd_remaining_s"] <= 1800
    assert saved["quiet_now"] is True
    assert saved["revision"] != before
    fresh = ch.delivery_view(state)
    assert fresh["policy"]["quiet"] is True
    assert "suppress" in fresh["note"].lower()


def test_a_quiet_pull_keeps_the_notices(state):
    _notice(state)
    _save_delivery(state, transport="local", quiet=True)
    result = ch.pull(state, {"confirmation": ch.PULL_TOKEN})
    assert result["suppressed"] is True
    assert result["count"] == 0
    assert ch.inbox(state)["digest"]["total"] == 1, "suppression must never delete a notice"
    with pytest.raises(ValueError, match="Type PULL NOTICES"):
        ch.pull(state, {})


def test_pushing_needs_the_native_transport_and_a_live_server(state):
    with pytest.raises(ValueError, match="Type SEND OS ALERT"):
        ch.push(state, {})
    with pytest.raises(ValueError, match="native transport"):
        ch.push(state, {"confirmation": ch.PUSH_TOKEN})
    _save_delivery(state, transport="native")
    state.demo = True
    with pytest.raises(ValueError, match="live mode"):
        ch.push(state, {"confirmation": ch.PUSH_TOKEN})
    state.demo = False
    result = ch.push(state, {"confirmation": ch.PUSH_TOKEN})
    assert isinstance(result["result"]["submitted"], bool), "a refusal is reported, never hidden"
    assert not result["result"]["submitted"] or result["result"].get("program")


def test_the_platform_notifier_is_described_honestly():
    found = native_available()
    assert set(found) == {"platform", "program", "present", "note"}
    assert isinstance(found["present"], bool)
    assert found["program"]
    if not found["present"]:
        assert "not" in found["note"].lower() or "missing" in found["note"].lower()


# ── Telegram ──────────────────────────────────────────────────────────── #


def test_the_telegram_view_explains_every_prerequisite(state):
    view = ch.telegram_view(state)
    assert [check["key"] for check in view["checks"]] == ["policy", "whitelist", "token", "pin"]
    assert view["ready"] == all(check["ok"] for check in view["checks"])
    assert set(view["runtime"]) >= {"phase", "alive"}
    assert view["credentials"]["token_present"] in (True, False)
    assert "presence" in view["note"].lower() or "not validity" in view["note"].lower()


def test_starting_the_poller_needs_the_typed_token(state):
    with pytest.raises(ValueError, match="Type START TELEGRAM POLLING"):
        ch.telegram_action(state, {"operation": "telegram-start"})
    with pytest.raises(ValueError, match="Type STOP TELEGRAM POLLING"):
        ch.telegram_action(state, {"operation": "telegram-stop", "confirmation": "STOP"})
    with pytest.raises(ValueError, match="telegram-start or telegram-stop"):
        ch.telegram_action(state, {"operation": "telegram-restart"})


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
        yield {"base": f"http://{host}:{port}", "state": created, "repo": repo}
    finally:
        if getattr(created, "workflow_scheduler", None):
            created.workflow_scheduler.stop()
        created.mission.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _call_http(url: str, payload=None, token=TOKEN):
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


def test_every_channel_read_needs_the_gui_token(live):
    for path in ("overview", "inbox", "delivery", "telegram", "policy"):
        status, body = _call_http(live["base"] + "/api/channels/" + path, token=None)
        assert status == 401, path
        assert "error" in body


def test_the_overview_carries_everything_the_page_paints(live):
    status, body = _call_http(live["base"] + "/api/channels/overview")
    assert status == 200
    data = body["data"]
    assert set(data) >= {"schema", "observed_at", "inbox", "delivery", "telegram", "preferences",
                         "tokens", "limits", "notes"}
    assert data["inbox"]["digest"]["total"] == 0
    assert data["delivery"]["native"]["present"] in (True, False)
    assert data["tokens"]["save_delivery"] == ch.SAVE_TOKEN
    assert data["limits"]["notices"] == ch.MAX_NOTICES


def test_the_inbox_endpoint_keeps_limit_and_pending_honest(live):
    _notice(live["state"])
    status, body = _call_http(live["base"] + "/api/channels/inbox?limit=1")
    assert status == 200
    assert body["data"]["digest"]["total"] == 1
    assert set(body["data"]["pending"]) == {"jobs", "actions"}
    status, body = _call_http(live["base"] + "/api/channels/inbox?limit=notanumber")
    assert status == 200 and body["data"]["digest"]["total"] == 1


def test_unknown_channel_routes_are_404_and_actions_are_validated(live):
    status, _ = _call_http(live["base"] + "/api/channels/nonsense")
    assert status == 404
    status, body = _call_http(live["base"] + "/api/channels/ack", {"ids": "everything"})
    assert status == 400 and body["error"]["code"] == "invalid_request"
    status, body = _call_http(live["base"] + "/api/channels/policy/save", {"revision": "", "mobile": {"lite": True}})
    assert status == 200 and body["data"]["policy"]["mobile"]["lite"] is True


def test_a_write_without_its_token_is_refused_by_the_route(live):
    status, body = _call_http(live["base"] + "/api/channels/notify", {})
    assert status == 400 and "PULL NOTICES" in body["error"]["message"]
    status, body = _call_http(live["base"] + "/api/channels/push", {"confirmation": ch.PUSH_TOKEN})
    assert status == 400 and "native transport" in body["error"]["message"]


def test_an_approval_request_becomes_a_real_notice(repo: Path):
    """The server hook writes one notice when a job stops for approval."""
    created = ServerState(repo, token=TOKEN, quiet=True)
    try:
        ch.record(created, "approval", state_name="awaiting_approval", title="Send the invoice",
                  detail="mailer · risk medium", task_id="task-9")
        inbox = ch.inbox(created)
        assert inbox["digest"]["states"] == {"awaiting_approval": 1}
        assert inbox["items"][0]["kind"] == "approval"
    finally:
        if getattr(created, "workflow_scheduler", None):
            created.workflow_scheduler.stop()
        created.mission.close()
