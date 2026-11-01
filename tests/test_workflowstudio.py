"""Workflow Builder: the page's service layer and its restart-surviving scheduler.

The builder never re-implements execution: saving goes to ``Workflows.save``,
running to ``Workflows.run``, cancelling to ``Workflows.cancel``. What this
module owns is the *schedule* store, the cron maths, the read projections the
canvas needs (layers, diffs, run detail) and the ticker that turns a persisted
schedule into exactly one consented run when its window opens.

Every test uses the throwaway ``repo`` workspace and the isolated ``NEXUS_HOME``
from ``conftest.py``; no provider call, task or real schedule is touched.
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

import nexus.workflowstudio as studio_mod
from nexus.server import ServerState, make_handler
from nexus.webapi import Limiter

TOKEN = "workflow-builder-test-token"
MONDAY_9AM = 1767258000.0          # 2026-01-01 09:00 UTC, a Thursday


@pytest.fixture()
def state(repo: Path):
    created = ServerState(repo, token=TOKEN, quiet=True)
    try:
        yield created
    finally:
        if getattr(created, "workflow_scheduler", None):
            created.workflow_scheduler.stop()
        created.mission.close()


def _node(ident, kind="research", depends=None, condition="success", **extra):
    return {"id": ident, "kind": kind, "role": "general", "task": f"Inspect {ident}",
            "depends_on": depends or [], "condition": condition, **extra}


def _definition(nodes=None, name="Reviewed workflow"):
    return {"name": name, "nodes": nodes if nodes is not None else [_node("research")]}


def _saved(state, definition=None, family=None) -> dict:
    return state.workbench.studio.workflows.save(
        {"definition": definition or _definition(), "family": family or ""})["data"]


def _schedule(state, workflow_id, trigger=None, **extra):
    payload = {"workflow_id": workflow_id, "trigger": trigger or {"kind": "interval", "minutes": 30},
               "confirmation": studio_mod.SCHEDULE_TOKEN, **extra}
    return studio_mod.save_schedule(state, payload)


class FakeRun:
    """Stands in for the real DAG engine; records consent, never executes."""

    def __init__(self, *, busy=False, error=""):
        self.calls: list[dict] = []
        self.busy = busy
        self.error = error

    def __call__(self, payload):
        if self.busy:
            raise ValueError("Stop active/queued work before running a workflow")
        if self.error:
            raise ValueError(self.error)
        if payload.get("confirm") != studio_mod.RUN_TOKEN:
            raise ValueError("Type RUN REVIEWED WORKFLOW")
        self.calls.append(payload)
        return {"data": {"run_id": f"run-{len(self.calls)}"}, "source": "test"}


@pytest.fixture()
def engine(state, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(state.workbench.studio.workflows, "run", fake)
    return fake


# ── the store ─────────────────────────────────────────────────────────── #


def test_store_starts_empty_and_says_so(state):
    store = studio_mod.load_store(Path(state.workspace))
    assert store["schema"] == studio_mod.SCHEMA
    assert store["schedules"] == [] and store["last_tick_at"] == 0.0
    assert store["error"] == ""
    assert len(store["revision"]) == 16
    assert not studio_mod.schedules_path(Path(state.workspace)).exists()


def test_store_damage_is_reported_never_silently_repaired(state):
    path = studio_mod.schedules_path(Path(state.workspace))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    store = studio_mod.load_store(Path(state.workspace))
    assert store["schedules"] == [] and store["error"]
    assert path.read_text() == "{not json"          # left exactly as found
    body = studio_mod.schedules_view(state)
    assert body["error"] == store["error"]


def test_store_is_private_and_written_atomically(state):
    saved = _saved(state)
    studio_mod.save_schedule(state, {"workflow_id": saved["id"], "trigger": {"kind": "interval", "minutes": 10},
                                     "confirmation": studio_mod.SCHEDULE_TOKEN})
    path = studio_mod.schedules_path(Path(state.workspace))
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert [item.name for item in path.parent.iterdir() if item.name.endswith(".tmp")] == []
    assert json.loads(path.read_text())["schema"] == studio_mod.SCHEMA


def test_store_keeps_a_bounded_history_of_changes(state):
    saved = _saved(state)
    for minutes in (10, 20, 30):
        studio_mod.save_schedule(state, {"workflow_id": saved["id"],
                                         "trigger": {"kind": "interval", "minutes": minutes},
                                         "confirmation": studio_mod.SCHEDULE_TOKEN})
    history = sorted(studio_mod.history_path(Path(state.workspace)).iterdir())
    assert 1 <= len(history) <= studio_mod.MAX_HISTORY


def test_revision_changes_only_when_the_schedule_list_does(state):
    saved = _saved(state)
    first = studio_mod.schedules_view(state)["revision"]
    studio_mod.save_schedule(state, {"workflow_id": saved["id"], "trigger": {"kind": "interval", "minutes": 15},
                                     "confirmation": studio_mod.SCHEDULE_TOKEN})
    second = studio_mod.schedules_view(state)["revision"]
    assert first != second
    assert second == studio_mod.schedules_view(state)["revision"]


# ── cron maths ────────────────────────────────────────────────────────── #


@pytest.mark.parametrize("text", ["", "* * *", "* * * * * *", "61 * * * *", "* 24 * * *",
                                  "* * 0 * *", "* * * 13 *", "* * * * 8", "*/0 * * * *",
                                  "a * * * *", None, 7])
def test_cron_refuses_anything_it_cannot_read(text):
    with pytest.raises(ValueError):
        studio_mod.cron_expression(text)


def test_cron_next_matches_a_walking_clock(state):
    after = MONDAY_9AM + 7 * 60
    first = studio_mod.next_cron("*/15 * * * *", after)
    assert first == MONDAY_9AM + 15 * 60
    assert studio_mod.next_cron("*/15 * * * *", first) == first + 15 * 60


def test_cron_honours_weekday_ranges_and_the_sunday_seven(state):
    # 2026-01-01 is a Thursday; the next weekday 09:00 is Thursday itself.
    assert time.strftime("%a %H:%M", time.gmtime(studio_mod.next_cron("0 9 * * 1-5", MONDAY_9AM - 60)))
    thursday = studio_mod.next_cron("0 9 * * 1-5", MONDAY_9AM - 60)
    assert time.strftime("%a %H:%M", time.gmtime(thursday)) == "Thu 09:00"
    # 7 and 0 both mean Sunday.
    assert studio_mod.next_cron("0 9 * * 7", MONDAY_9AM) == studio_mod.next_cron("0 9 * * 0", MONDAY_9AM)


def test_cron_day_of_month_or_day_of_week_is_vixie(state):
    # dom=13 OR dow=Friday: whichever comes first after 1 Jan 2026.
    first = studio_mod.next_cron("0 0 13 * 5", MONDAY_9AM)
    assert time.strftime("%d %a", time.gmtime(first)) in {"13 Thu", "02 Fri", "09 Fri"}


def test_cron_lists_steps_and_ranges(state):
    assert studio_mod.next_cron("0,30 8-10 * * *", MONDAY_9AM) == MONDAY_9AM + 30 * 60     # 09:30
    assert studio_mod.next_cron("0,30 8-10 * * *", MONDAY_9AM + 30 * 60) == MONDAY_9AM + 60 * 60
    assert studio_mod.next_cron("*/20 * * * *", MONDAY_9AM) == MONDAY_9AM + 20 * 60
    with pytest.raises(ValueError, match="Invalid step"):
        studio_mod.next_cron("*/120 * * * *", MONDAY_9AM)     # a step wider than the field is a typo


def test_interval_bounds_are_enforced():
    assert studio_mod.next_run({"kind": "interval", "minutes": 5}, 1000) == 1300
    assert studio_mod.next_run({"kind": "manual"}, 1000) is None
    for minutes in (0, 4, 10_081, "ten", None, True):
        with pytest.raises(ValueError):
            studio_mod.next_run({"kind": "interval", "minutes": minutes}, 1000)


def test_preview_trigger_is_ordered_bounded_and_never_invents_manual_times():
    times = studio_mod.preview_trigger({"kind": "cron", "cron": "*/5 * * * *"}, after=1000)
    assert len(times) == studio_mod.PREVIEW_COUNT
    assert times == sorted(times) and all(value > 1000 for value in times)
    assert studio_mod.preview_trigger({"kind": "interval", "minutes": 60}, 2, after=1000) == [4600, 8200]
    assert studio_mod.preview_trigger({"kind": "manual"}, after=1000) == []


# ── the page's read projections ───────────────────────────────────────── #


def test_layout_layers_a_chain_and_parallel_reads(state):
    definition = _definition([_node("a"), _node("b"), _node("c", depends=["a", "b"]),
                              _node("d", "agent", ["c"])])
    detail = studio_mod.validate_definition(state, {"definition": definition})
    layers = {item["id"]: item["layer"] for item in detail["layout"]["nodes"]}
    assert layers == {"a": 0, "b": 0, "c": 1, "d": 2}
    assert len(detail["layout"]["edges"]) == 3
    assert detail["layout"]["width"] > 0 and detail["layout"]["height"] > 0
    xs = {item["id"]: item["x"] for item in detail["layout"]["nodes"]}
    assert xs["c"] - xs["a"] == 264 and xs["a"] == xs["b"]
    assert detail["writes"] == ["d"]


def test_families_group_versions_newest_first(state):
    first = _saved(state)
    second = _saved(state, _definition([_node("writer", "agent")]), family=first["family"])
    body = studio_mod.families(state)
    assert body["count"] == 1
    family = body["families"][0]
    assert [row["version"] for row in family["versions"]] == [2, 1]
    assert family["latest"]["id"] == second["id"]
    detail = studio_mod.version_detail(state, second["id"])
    assert detail["version"] == 2 and detail["name"] == "Reviewed workflow"
    assert detail["definition"]["nodes"][0]["id"] == "writer"


def test_diff_versions_names_added_removed_and_changed_fields(state):
    first = _saved(state, _definition([_node("a"), _node("gone")]))
    second = _saved(state, _definition([_node("a", task="New task"), _node("fresh")]), family=first["family"])
    diff = studio_mod.diff_versions(state, first["id"], second["id"])
    assert diff["added"] == ["fresh"] and diff["removed"] == ["gone"]
    assert diff["changed"] == [{"id": "a", "fields": ["task"], "before": {"task": "Inspect a"},
                                "after": {"task": "New task"}}]
    assert diff["same_family"] is True and diff["identical"] is False
    assert studio_mod.diff_versions(state, first["id"], first["id"])["identical"] is True


def test_run_rows_and_detail_report_only_what_the_engine_recorded(state):
    normalized, _ = state.workbench.studio.workflows.validate(_definition())
    record = state.workbench.studio.put("workflow-run", {
        "definition": normalized, "status": "failed", "error": "provider said no",
        "nodes": {"research": {"status": "done", "ok": True, "task_id": "t-1", "duration_s": 1.5,
                               "result": {"text": "found it"}},
                  "late": {"status": "pending"}}})
    rows = studio_mod.run_rows(state)
    assert rows["count"] == 1 and rows["items"][0]["id"] == record["id"]
    assert rows["items"][0]["nodes_done"] == 1 and rows["items"][0]["nodes_total"] == 1
    detail = studio_mod.run_detail(state, record["id"])
    assert detail["status"] == "failed" and detail["error"] == "provider said no"
    node = detail["nodes"][0]
    assert node["status"] == "done" and node["task_id"] == "t-1" and node["text"] == "found it"


def test_diff_runs_compares_node_outcomes(state):
    normalized, _ = state.workbench.studio.workflows.validate(_definition())
    first = state.workbench.studio.put("workflow-run", {"definition": normalized, "status": "done",
                                                        "nodes": {"research": {"status": "done", "ok": True}}})
    second = state.workbench.studio.put("workflow-run", {"definition": normalized, "status": "failed",
                                                         "nodes": {"research": {"status": "error", "ok": False}}})
    diff = studio_mod.diff_runs(state, first["id"], second["id"])
    assert diff["changed"][0]["id"] == "research"
    assert diff["identical"] is False


# ── mutations keep their consent gates ────────────────────────────────── #


def test_save_requires_its_typed_token_and_refuses_demo(state):
    with pytest.raises(ValueError, match="SAVE WORKFLOW"):
        studio_mod.save_version(state, {"definition": _definition()})
    state.demo = True
    with pytest.raises(ValueError, match="Live mode is required"):
        studio_mod.save_version(state, {"definition": _definition(), "confirmation": studio_mod.SAVE_TOKEN})
    state.demo = False
    body = studio_mod.save_version(state, {"definition": _definition(), "confirmation": studio_mod.SAVE_TOKEN})
    assert body["saved"]["version"] == 1
    assert state.workbench.studio.items("workflow-run") == []      # saving never runs


def test_run_and_cancel_go_through_the_engine_with_their_tokens(state, engine):
    saved = _saved(state)
    with pytest.raises(ValueError, match="RUN REVIEWED WORKFLOW"):
        studio_mod.run_workflow(state, {"id": saved["id"]})
    body = studio_mod.run_workflow(state, {"id": saved["id"], "confirmation": studio_mod.RUN_TOKEN})
    assert body["started"]["run_id"] == "run-1"
    assert engine.calls[0] == {"id": saved["id"], "confirm": studio_mod.RUN_TOKEN}
    with pytest.raises(ValueError, match="CANCEL RUN run-1"):
        studio_mod.cancel_run(state, {"id": "run-1"})


def test_validate_definition_is_structural_only(state):
    with pytest.raises(ValueError, match="unique"):
        studio_mod.validate_definition(state, {"definition": _definition([_node("a"), _node("a")])})
    body = studio_mod.validate_definition(state, {"definition": _definition()})
    assert body["valid"] is True and body["order"] == ["research"]
    assert state.workbench.studio.items("workflow") == []


# ── schedules ─────────────────────────────────────────────────────────── #


def test_saving_a_schedule_needs_its_token_and_a_real_version(state):
    saved = _saved(state)
    with pytest.raises(ValueError, match="SCHEDULE WORKFLOW"):
        studio_mod.save_schedule(state, {"workflow_id": saved["id"], "trigger": {"kind": "interval", "minutes": 5}})
    with pytest.raises(ValueError):
        studio_mod.save_schedule(state, {"workflow_id": "nope", "trigger": {"kind": "interval", "minutes": 5},
                                         "confirmation": studio_mod.SCHEDULE_TOKEN})
    body = _schedule(state, saved["id"])
    row = body["changed"]
    assert row["action"] == "created" and row["id"].startswith("w-")
    view = studio_mod.schedules_view(state)["schedules"]
    assert view[0]["workflow_id"] == saved["id"] and view[0]["enabled"] is True
    assert view[0]["version_present"] is True and view[0]["described"] == "every 30 minutes"
    assert view[0]["next_run_at"] > time.time()


def test_a_manual_trigger_cannot_be_enabled_and_a_stale_revision_is_refused(state):
    saved = _saved(state)
    with pytest.raises(ValueError, match="manual"):
        studio_mod.save_schedule(state, {"workflow_id": saved["id"], "trigger": {"kind": "manual"},
                                         "enabled": True, "confirmation": studio_mod.SCHEDULE_TOKEN})
    _schedule(state, saved["id"])
    with pytest.raises(ValueError, match="changed"):
        studio_mod.save_schedule(state, {"workflow_id": saved["id"], "trigger": {"kind": "interval", "minutes": 10},
                                         "revision": "0" * 16, "confirmation": studio_mod.SCHEDULE_TOKEN})


def test_schedule_tokens_are_exact_and_schedule_fields_are_pinned(state):
    saved = _saved(state)
    ident = _schedule(state, saved["id"])["changed"]["id"]
    assert studio_mod.schedule_token(ident, "enable") == f"ENABLE SCHEDULE {ident}"
    with pytest.raises(ValueError, match=f"DISABLE SCHEDULE {ident}"):
        studio_mod.toggle_schedule(state, {"id": ident, "enabled": False})
    body = studio_mod.toggle_schedule(state, {"id": ident, "enabled": False,
                                              "confirmation": f"DISABLE SCHEDULE {ident}"})
    row = body["schedules"][0]
    assert row["enabled"] is False and row["next_run_at"] is None
    assert row["version"] == saved["version"] and row["family"] == saved["family"]


def test_removing_a_schedule_needs_its_token_and_keeps_the_versions(state):
    saved = _saved(state)
    ident = _schedule(state, saved["id"])["changed"]["id"]
    with pytest.raises(ValueError, match="REMOVE SCHEDULE"):
        studio_mod.remove_schedule(state, {"id": ident})
    studio_mod.remove_schedule(state, {"id": ident, "confirmation": f"REMOVE SCHEDULE {ident}"})
    assert studio_mod.schedules_view(state)["schedules"] == []
    assert state.workbench.studio.find("workflow", saved["id"])["version"] == 1
    with pytest.raises(ValueError, match="Unknown schedule"):
        studio_mod.remove_schedule(state, {"id": ident, "confirmation": f"REMOVE SCHEDULE {ident}"})


def test_run_now_starts_the_pinned_version_only_with_its_token(state, engine):
    saved = _saved(state)
    ident = _schedule(state, saved["id"])["changed"]["id"]
    with pytest.raises(ValueError, match="RUN SCHEDULE"):
        studio_mod.run_schedule_now(state, {"id": ident})
    body = studio_mod.run_schedule_now(state, {"id": ident, "confirmation": f"RUN SCHEDULE {ident}"})
    assert body["skipped"] is False and body["run_id"] == "run-1"
    assert engine.calls[0]["id"] == saved["id"]


def test_the_schedule_list_is_bounded(state):
    saved = _saved(state)
    for minutes in range(5, 5 + studio_mod.MAX_SCHEDULES):
        _schedule(state, saved["id"], trigger={"kind": "interval", "minutes": minutes})
    assert len(studio_mod.schedules_view(state)["schedules"]) == studio_mod.MAX_SCHEDULES
    with pytest.raises(ValueError, match="At most"):
        _schedule(state, saved["id"], trigger={"kind": "interval", "minutes": 500})


# ── one tick ──────────────────────────────────────────────────────────── #


def _due(state, ident, *, at=None):
    """Make one schedule due now, without waiting for a real interval."""
    store = studio_mod.load_store(Path(state.workspace))
    rows = [dict(row) for row in store["schedules"]]
    for row in rows:
        if row["id"] == ident:
            row["next_run_at"] = at if at is not None else time.time() - 1
    studio_mod._write_store(Path(state.workspace), rows, 0.0)


def test_tick_starts_one_consented_run_and_advances_the_window(state, engine):
    saved = _saved(state)
    ident = _schedule(state, saved["id"], trigger={"kind": "interval", "minutes": 30})["changed"]["id"]
    _due(state, ident)
    body = studio_mod.tick(state)
    assert body["ran"] == 1 and body["actions"][0]["action"] == "ran"
    assert engine.calls == [{"id": saved["id"], "confirm": studio_mod.RUN_TOKEN}]
    row = studio_mod.schedules_view(state)["schedules"][0]
    assert row["runs"] == ["run-1"] and row["last"]["run_id"] == "run-1"
    assert row["next_run_at"] > time.time()
    again = studio_mod.tick(state)                    # the same window never fires twice
    assert again["ran"] == 0 and engine.calls == [{"id": saved["id"], "confirm": studio_mod.RUN_TOKEN}]


def test_tick_defers_while_another_run_holds_the_engine(state, engine):
    saved = _saved(state)
    ident = _schedule(state, saved["id"])["changed"]["id"]
    _due(state, ident)
    state.workflow_busy = True
    body = studio_mod.tick(state)
    assert body["ran"] == 0 and body["actions"][0]["action"] == "deferred"
    row = studio_mod.schedules_view(state)["schedules"][0]
    assert row["next_run_at"] < time.time()          # still due: the retry is not lost
    assert "in progress" in row["last_skip"]["reason"]
    state.workflow_busy = False
    assert studio_mod.tick(state)["ran"] == 1


def test_tick_counts_a_window_missed_while_the_server_was_down(state, engine):
    saved = _saved(state)
    ident = _schedule(state, saved["id"])["changed"]["id"]
    now = time.time()
    _due(state, ident, at=now - 3600)                          # due an hour ago
    store = studio_mod.load_store(Path(state.workspace))
    studio_mod._write_store(Path(state.workspace), store["schedules"], now - 7200)   # last pass: 2 h ago
    body = studio_mod.tick(state)
    assert body["ran"] == 0 and body["actions"][0]["action"] == "missed"
    row = studio_mod.schedules_view(state)["schedules"][0]
    assert row["missed"] == 1 and "not running" in row["last_skip"]["reason"]
    assert engine.calls == []                        # stale work is never replayed


def test_a_window_that_opened_a_moment_before_the_restart_still_runs(state, engine):
    saved = _saved(state)
    ident = _schedule(state, saved["id"])["changed"]["id"]
    now = time.time()
    _due(state, ident, at=now - 20)                            # within the grace window
    store = studio_mod.load_store(Path(state.workspace))
    studio_mod._write_store(Path(state.workspace), store["schedules"], now - 900)
    body = studio_mod.tick(state)
    assert body["ran"] == 1 and body["actions"][0]["action"] == "ran"
    assert studio_mod.schedules_view(state)["schedules"][0]["missed"] == 0


def test_tick_skips_a_latched_emergency_stop(state, engine, monkeypatch):
    saved = _saved(state)
    ident = _schedule(state, saved["id"])["changed"]["id"]
    _due(state, ident)
    monkeypatch.setattr("nexus.computer_control.EmergencyStop.stopped", lambda self: True)
    body = studio_mod.tick(state)
    assert body["ran"] == 0 and "Emergency stop" in body["actions"][0]["reason"]
    assert engine.calls == []


def test_tick_skips_a_paused_queue_and_demo_mode(state, engine):
    saved = _saved(state)
    ident = _schedule(state, saved["id"])["changed"]["id"]
    _due(state, ident)
    state.queue_paused = True
    assert "paused" in studio_mod.tick(state)["actions"][0]["reason"]
    state.queue_paused = False
    _due(state, ident)
    state.demo = True
    assert "Demo mode" in studio_mod.tick(state)["actions"][0]["reason"]
    state.demo = False
    _due(state, ident)
    assert studio_mod.tick(state)["ran"] == 1


def test_tick_reports_a_vanished_version_instead_of_running_it(state, engine):
    saved = _saved(state)
    ident = _schedule(state, saved["id"])["changed"]["id"]
    store = studio_mod.load_store(Path(state.workspace))
    rows = [dict(row) for row in store["schedules"]]
    rows[0]["workflow_id"] = "deleted-version"      # e.g. a workspace restored without that version
    studio_mod._write_store(Path(state.workspace), rows, store["last_tick_at"])
    _due(state, ident)
    view = studio_mod.schedules_view(state)["schedules"][0]
    assert view["version_present"] is False and view["locked_out"] is True
    body = studio_mod.tick(state)
    assert body["ran"] == 0 and "no longer exists" in body["actions"][0]["reason"]


def test_tick_does_nothing_when_another_server_holds_the_lock(state, engine, state_lock=None):
    from filelock import FileLock

    saved = _saved(state)
    ident = _schedule(state, saved["id"])["changed"]["id"]
    _due(state, ident)
    lock = FileLock(str(studio_mod.schedules_path(Path(state.workspace)).parent / studio_mod.LOCK_FILE))
    lock.acquire(timeout=1)
    try:
        body = studio_mod.tick(state)
    finally:
        lock.release()
    assert body["busy"] is True and engine.calls == []
    assert "another server" in body["note"].lower()


def test_tick_with_an_unreadable_store_does_not_touch_it(state, engine):
    path = studio_mod.schedules_path(Path(state.workspace))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]")
    body = studio_mod.tick(state)
    assert body["ran"] == 0 and body["error"]
    assert path.read_text() == "[]"


# ── the ticker that survives a restart ────────────────────────────────── #


def test_scheduler_starts_only_when_a_schedule_exists(state):
    from nexus.workflowstudio import scheduler_for

    scheduler = scheduler_for(state)
    assert scheduler.ensure_started() is False
    saved = _saved(state)
    _schedule(state, saved["id"])
    assert scheduler.ensure_started() is True
    assert scheduler.running is True
    assert scheduler_for(state) is scheduler
    scheduler.stop()
    assert scheduler.running is False
    status = scheduler.status()
    assert status["running"] is False and status["enabled"] == 1
    assert status["interval_s"] >= 1


def test_scheduler_picks_up_an_existing_schedule_after_a_restart(state, engine):
    saved = _saved(state)
    ident = _schedule(state, saved["id"])["changed"]["id"]
    _due(state, ident)
    scheduler = studio_mod.WorkflowScheduler(state, interval=1)
    scheduler.start()
    try:
        deadline = time.time() + 8
        while time.time() < deadline and engine.calls == []:
            time.sleep(0.1)
    finally:
        scheduler.stop()
    assert engine.calls == [{"id": saved["id"], "confirm": studio_mod.RUN_TOKEN}]
    assert scheduler.passes >= 1
    assert studio_mod.schedules_view(state)["schedules"][0]["runs"] == ["run-1"]


def test_scheduler_survives_a_failing_pass_and_reports_it(state, engine, monkeypatch):
    saved = _saved(state)
    _schedule(state, saved["id"])
    monkeypatch.setattr(studio_mod, "tick", lambda _state: (_ for _ in ()).throw(ValueError("boom")))
    scheduler = studio_mod.WorkflowScheduler(state, interval=1)
    scheduler.start()
    time.sleep(0.4)
    scheduler.stop()
    assert scheduler.error == "boom"


# ── overview ──────────────────────────────────────────────────────────── #


def test_overview_reports_live_limits_and_typed_tokens(state, engine):
    saved = _saved(state)
    _schedule(state, saved["id"])
    body = studio_mod.overview(state)
    assert body["families"] == 1 and body["versions"] == 1
    assert body["schedules"] == 1 and body["enabled_schedules"] == 1
    assert body["tokens"]["save"] == "SAVE WORKFLOW"
    assert body["limits"]["schedules"] == studio_mod.MAX_SCHEDULES
    assert [note["id"] for note in body["notes"]] == ["pinned", "missed", "consent"]
    assert body["error"] == "" and body["demo"] is False
    assert body["vocabulary"]["kinds"] == ["agent", "research", "reviewer", "verification", "approval"]


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
        if getattr(created, "workflow_scheduler", None):
            created.workflow_scheduler.stop()
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


def test_workflow_routes_require_the_gui_token(live):
    for path in ("/api/workflows/overview", "/api/workflows/families", "/api/workflows/runs",
                 "/api/workflows/schedules"):
        status, body = _call(live["base"] + path, token=None)
        assert status == 401, path
        assert "error" in body
    status, _ = _call(live["base"] + "/api/workflows/validate", {"definition": _definition()}, token=None)
    assert status == 401


def test_overview_over_http_is_audited_and_shaped(live, repo: Path):
    from nexus import webapi

    status, body = _call(live["base"] + "/api/workflows/overview")
    assert status == 200 and body["ok"] is True
    assert body["data"]["tokens"]["save"] == "SAVE WORKFLOW"
    assert any(row["action"] == "workflow.overview" for row in webapi.read_audit(repo))


def test_save_then_schedule_over_http(live):
    status, body = _call(live["base"] + "/api/workflows/version/save",
                         {"definition": _definition(), "confirmation": "SAVE WORKFLOW"})
    assert status == 200 and body["data"]["saved"]["version"] == 1
    ident = body["data"]["saved"]["id"]
    status, body = _call(live["base"] + "/api/workflows/schedule/save",
                         {"workflow_id": ident, "trigger": {"kind": "cron", "cron": "0 9 * * 1-5"},
                          "confirmation": "SCHEDULE WORKFLOW"})
    assert status == 200
    row = body["data"]["schedules"][0]
    assert row["trigger"] == {"kind": "cron", "cron": "0 9 * * 1-5"}
    status, body = _call(live["base"] + "/api/workflows/schedules")
    assert status == 200 and len(body["data"]["schedules"]) == 1
    assert body["data"]["scheduler"]["running"] in (True, False)


def test_bad_input_and_unknown_routes_are_reported_precisely(live):
    assert _call(live["base"] + "/api/workflows/version")[0] == 400
    assert _call(live["base"] + "/api/workflows/diff?a=1")[0] == 400
    assert _call(live["base"] + "/api/workflows/nope")[0] == 404
    assert _call(live["base"] + "/api/workflows/version/save", {"definition": {}})[0] == 400
    status, body = _call(live["base"] + "/api/workflows/run", {"id": "missing"})
    assert status == 400 and body["error"]["code"] == "invalid_request"


def test_workflow_reads_are_rate_limited(live):
    live["state"].api_limiter = Limiter(limit=2, window=60)
    assert _call(live["base"] + "/api/workflows/overview")[0] == 200
    assert _call(live["base"] + "/api/workflows/families")[0] == 200
    status, body = _call(live["base"] + "/api/workflows/runs")
    assert status == 429 and body["error"]["code"] == "rate_limited"
