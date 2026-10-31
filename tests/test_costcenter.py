"""Cost & performance: the ledger, its percentiles, the budget and the runner.

Every test runs on the throwaway ``repo`` workspace with the isolated
``NEXUS_HOME`` from ``conftest.py``. Nothing contacts a provider: traces are
written through the real ``SessionStore`` (as the agent does), and the one
comparison test uses a scripted provider registry.
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

import nexus.costcenter as cc
from nexus.server import ServerState, make_handler
from nexus.sessions import SessionStore
from nexus.webapi import Limiter

TOKEN = "cost-page-test-token"


@pytest.fixture()
def state(repo: Path):
    created = ServerState(repo, token=TOKEN, quiet=True)
    try:
        yield created
    finally:
        if getattr(created, "workflow_scheduler", None):
            created.workflow_scheduler.stop()
        created.mission.close()


def _trace(workspace: Path, *, ident="trace0000001", models=None, wires=None, started=None,
           role="backend", agent="worker", state_name="finished", duration=12.0) -> dict:
    """Write one observation exactly the shape the agent's Trace writes."""
    started = time.time() - 120 if started is None else started
    events = []
    step = 0
    for wire_at, answer_at, who in (wires or []):
        events.append({"step": step, "at": wire_at, "kind": "model_wire", "data": {"agent": who}})
        events.append({"step": step + 1, "at": answer_at, "kind": "model_response",
                       "data": {"usage": {"agent": who}}})
        step += 2
    body = {"schema": 1, "id": ident, "started": started, "workspace": str(workspace),
            "trigger": "a task", "kind": "agent_run", "state": state_name, "complete": True,
            "duration_s": duration, "events": events, "models": models or [],
            "outcome": {"role": role, "agent": agent, "reported_ok": True}}
    SessionStore().save_observation(ident, workspace, body)
    return body


def _call(**overrides) -> dict:
    item = {"agent": "worker", "model": "claude-x", "input_tokens": 1000, "output_tokens": 500,
            "calculated_model_cost_usd": 0.0155, "actual_cost_usd": None, "day": "2026-01-01",
            "price_snapshot": {"source": "vendor page, 2026-01-01", "input_per_million": 3.0,
                               "output_per_million": 15.0}}
    item.update(overrides)
    return item


def _save(state, **policy) -> dict:
    view = cc.policy_view(state.workspace)
    return cc.save_policy(state, {"confirmation": cc.SAVE_TOKEN, "revision": view["revision"], **policy})


def _budget(**overrides) -> dict:
    budget = {"enabled": True, "daily_usd": 1.0, "monthly_usd": 10.0, "stop": True,
              "unknown_price_blocks": False}
    budget.update(overrides)
    return budget


# ── policy ────────────────────────────────────────────────────────────── #


def test_no_policy_means_no_budget_and_no_store(repo: Path):
    view = cc.policy_view(repo)
    assert view["policy"]["budget"]["enabled"] is False
    assert view["policy"]["budget"]["stop"] is True
    assert view["policy"]["resources"]["enabled"] is True
    assert view["revision"] == ""
    assert not cc.store_dir(repo).exists(), "reading the policy must not create the store"
    assert cc.spend(repo, 0)["calls"] == 0


def test_the_policy_needs_its_token_and_a_current_revision(state):
    view = cc.policy_view(state.workspace)
    with pytest.raises(ValueError, match="Type SAVE COST POLICY"):
        cc.save_policy(state, {"budget": _budget(), "revision": view["revision"]})
    with pytest.raises(ValueError, match="changed since you read it"):
        cc.save_policy(state, {"budget": _budget(), "confirmation": cc.SAVE_TOKEN, "revision": "stale"})
    saved = _save(state, budget=_budget(daily_usd=2.5))
    assert saved["policy"]["budget"]["daily_usd"] == 2.5
    assert cc.policy_path(state.workspace).stat().st_mode & 0o777 == 0o600
    assert saved["revision"] and saved["revision"] != view["revision"]


def test_a_policy_that_could_not_work_is_refused(state):
    view = cc.policy_view(state.workspace)
    for body, message in (
        ({"budget": _budget(daily_usd=20.0, monthly_usd=10.0)}, "cannot exceed"),
        ({"budget": _budget(daily_usd=-1)}, "cannot be negative"),
        ({"budget": _budget(enabled="yes")}, "true or false"),
        ({"budget": {"nonsense": 1}}, "Unknown budget key"),
        ({"resources": {"interval_s": 1}}, "interval_s"),
        ({"resources": {"keep_hours": 0}}, "keep_hours"),
        ({"nonsense": 1}, "Unknown policy key"),
    ):
        with pytest.raises(ValueError, match=message):
            cc.save_policy(state, {**body, "confirmation": cc.SAVE_TOKEN, "revision": view["revision"]})


def test_an_unreadable_policy_is_reported_not_guessed(state):
    path = cc.policy_path(state.workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    view = cc.policy_view(state.workspace)
    assert "unreadable" in view["error"]
    assert view["policy"]["budget"]["enabled"] is False
    assert cc.budget_state(state.workspace)["enabled"] is False


# ── the ledger ────────────────────────────────────────────────────────── #


def test_a_recorded_run_becomes_real_calls(state):
    workspace = state.workspace
    now = time.time()
    _trace(workspace, models=[_call(), _call(input_tokens=200, output_tokens=100,
                                             calculated_model_cost_usd=None)],
           wires=[(now - 60, now - 59.2, "worker"), (now - 40, now - 39.7, "worker")])
    imported = cc.sync(workspace, force=True)
    assert imported["imported"] == 1
    body = cc.breakdown(state, 7, "model")
    assert body["rows"][0]["name"] == "claude-x"
    assert body["rows"][0]["calls"] == 2
    assert body["rows"][0]["usd"] == pytest.approx(0.0155)
    assert body["rows"][0]["unpriced"] == 1, "an unknown cost is counted, not dropped"
    assert body["totals"]["calls"] == 2


def test_latency_is_the_call_not_the_run(state):
    workspace = state.workspace
    now = time.time()
    _trace(workspace, models=[_call()], wires=[(now - 60, now - 59.0, "worker")])  # 1000 ms
    cc.sync(workspace, force=True)
    found = cc.latency_view(state, 7, "model")
    assert found["samples"] == 1
    assert found["rows"][0]["p50"] == pytest.approx(1000.0, abs=1.0)
    assert found["overall"]["p50"] == pytest.approx(1000.0, abs=1.0)


def test_percentiles_interpolate_and_never_invent_a_sample():
    assert cc.percentile([], 0.5) is None
    assert cc.percentile([42.0], 0.9) == 42.0
    assert cc.percentile([10, 20], 0.5) == 15.0
    values = [float(n) for n in range(1, 101)]
    assert cc.percentile(values, 0.5) == pytest.approx(50.5, abs=0.1)
    assert cc.percentile(values, 0.9) == pytest.approx(90.1, abs=0.1)
    assert cc.percentile(values, 0.99) == pytest.approx(99.0, abs=0.1)


def test_reimporting_the_same_trace_changes_nothing(state):
    workspace = state.workspace
    _trace(workspace, models=[_call()])
    assert cc.sync(workspace, force=True)["imported"] == 1
    assert cc.sync(workspace, force=True)["imported"] == 0
    assert cc.breakdown(state, 7, "model")["totals"]["calls"] == 1


def test_a_run_without_any_call_is_still_counted_as_a_run(state):
    workspace = state.workspace
    _trace(workspace, ident="trace0000002", models=[], wires=[])
    cc.sync(workspace, force=True)
    overview = cc.overview(state)
    assert overview["totals"]["runs_30d"] == 1
    assert overview["totals"]["calls_mtd"] == 0


def test_grouping_by_role_and_day_uses_the_recorded_values(state):
    workspace = state.workspace
    _trace(workspace, ident="trace0000010", role="backend", models=[_call(), _call()])
    _trace(workspace, ident="trace0000011", role="frontend",
           models=[_call(model="gemini-2.5", calculated_model_cost_usd=None)])
    cc.sync(workspace, force=True)
    roles = cc.breakdown(state, 7, "role")["rows"]
    assert {row["name"]: row["calls"] for row in roles} == {"backend": 2, "frontend": 1}
    days = cc.breakdown(state, 7, "day")["rows"]
    assert len(days) == 1 and days[0]["calls"] == 3
    with pytest.raises(ValueError, match="Group by"):
        cc.breakdown(state, 7, "nonsense")


def test_a_window_bounds_the_ledger(state):
    workspace = state.workspace
    old = time.time() - 40 * 86400
    _trace(workspace, ident="trace0000020", started=old, models=[_call()])
    cc.sync(workspace, force=True)
    assert cc.breakdown(state, 7, "model")["totals"]["calls"] == 0, "a 40-day-old call is outside 7 days"
    assert cc.breakdown(state, 90, "model")["totals"]["calls"] == 1
    with pytest.raises(ValueError, match="between 1 and 365"):
        cc.breakdown(state, 0, "model")


def test_the_report_answers_every_table_in_one_call(state):
    _trace(state.workspace, models=[_call()], wires=[(time.time() - 5, time.time() - 4, "worker")])
    report = cc.report(state, 7)
    assert set(report) >= {"by_model", "by_agent", "by_role", "by_day", "totals", "latency"}
    assert report["by_model"][0]["calls"] == 1
    assert report["latency"]["samples"] == 1


# ── budget ────────────────────────────────────────────────────────────── #


def test_no_budget_never_blocks(state):
    cc.enforce(state.workspace)  # no policy file at all
    _save(state, budget=_budget(enabled=False))
    cc.enforce(state.workspace)  # switched off


def test_the_budget_blocks_before_the_next_call(state):
    _trace(state.workspace, models=[_call(calculated_model_cost_usd=0.9)])
    cc.sync(state.workspace, force=True)
    _save(state, budget=_budget(daily_usd=0.5))
    with pytest.raises(cc.BudgetExceeded, match="daily budget"):
        cc.enforce(state.workspace)
    assert cc.budget_state(state.workspace)["enforced"] is True
    assert cc.budget_state(state.workspace)["today"]["usd"] == pytest.approx(0.9)


def test_a_budget_that_does_not_stop_is_reported_as_such(state):
    _trace(state.workspace, models=[_call(calculated_model_cost_usd=0.9)])
    cc.sync(state.workspace, force=True)
    _save(state, budget=_budget(daily_usd=0.5, stop=False))
    cc.enforce(state.workspace)  # allowed: the page says it is not enforced
    assert cc.budget_state(state.workspace)["enforced"] is False


def test_the_monthly_budget_blocks_even_when_today_is_clean(state):
    workspace = state.workspace
    if cc._month_start() >= cc._day_start() - 60:
        pytest.skip("the first day of the month has no earlier day to charge")
    _trace(workspace, started=cc._month_start() + 3600,
           models=[_call(calculated_model_cost_usd=0.9)])
    cc.sync(workspace, force=True)
    _save(state, budget=_budget(daily_usd=0.5, monthly_usd=0.5))
    assert cc.budget_state(workspace)["today"]["usd"] == 0.0
    with pytest.raises(cc.BudgetExceeded, match="monthly budget"):
        cc.enforce(workspace)


def test_strict_mode_stops_on_an_unpriced_call(state):
    _trace(state.workspace, models=[_call(calculated_model_cost_usd=None)])
    cc.sync(state.workspace, force=True)
    _save(state, budget=_budget(daily_usd=5.0, unknown_price_blocks=True))
    with pytest.raises(cc.BudgetExceeded, match="no verified price"):
        cc.enforce(state.workspace)
    _save(state, budget=_budget(daily_usd=5.0, unknown_price_blocks=False))
    cc.enforce(state.workspace)


def test_the_agent_stops_with_a_budget_reason(repo: Path, monkeypatch):
    """End to end: pricing is configured, the budget is tiny, the run stops."""
    from nexus.agent import NexusAgent
    from nexus.config import load_config

    from .conftest import FakeProvider, fake_registry, text_completion

    cfg = load_config(repo)
    state = ServerState(repo, token=TOKEN, quiet=True)
    try:
        state.workbench.save_settings({"pricing": {
            "fake-model": {"input_per_million": 1000.0, "output_per_million": 1000.0,
                           "source": "test rate"}}})
        _trace(repo, models=[_call(model="fake-model", calculated_model_cost_usd=0.5)])
        cc.sync(repo, force=True)
        _save(state, budget=_budget(daily_usd=0.1))
        provider = FakeProvider("worker", [text_completion("should not be called")], model="fake-model")
        agent = NexusAgent(cfg, workspace=repo, providers=fake_registry(provider, config=cfg), auto_yes=True)
        result = agent.run("do the thing")
        assert "budget stop" in result.stopped_reason
        assert result.ok is False
        assert provider.calls == [], "no model call may happen after the budget is crossed"
    finally:
        if getattr(state, "workflow_scheduler", None):
            state.workflow_scheduler.stop()
        state.mission.close()


# ── providers ─────────────────────────────────────────────────────────── #


def test_the_provider_list_never_carries_a_key(state):
    data = cc.providers(state)
    assert data["agents"], "the bundled agents.yaml has entries"
    for row in data["agents"]:
        assert set(row) >= {"name", "provider", "model", "local", "key_present", "priced"}
        assert "api_key" not in row and "key" not in row
        assert isinstance(row["key_present"], bool)
    assert isinstance(data["priced_models"], list)


# ── comparison ────────────────────────────────────────────────────────── #


def _scripted_registry(monkeypatch, *, fail=()):
    from nexus.providers import ProviderRegistry
    from nexus.types import Completion, TextBlock

    from .conftest import FakeProvider

    class Registry(ProviderRegistry):
        def get(self, name):  # noqa: D401 - test double
            if name in fail:
                raise RuntimeError("provider refused")
            return FakeProvider(
                name, [Completion(content=[TextBlock(text=f"answer from {name}")], model="fake-model",
                                  input_tokens=30, output_tokens=12)], model="fake-model")

    monkeypatch.setattr("nexus.providers.ProviderRegistry", Registry)
    return Registry


def test_a_comparison_needs_its_token_and_two_real_choices(state, monkeypatch):
    names = sorted(state.config.agents)[:2]
    with pytest.raises(ValueError, match="Type START COMPARISON"):
        cc.compare(state, {"agents": names, "prompt": "hello"})
    with pytest.raises(ValueError, match="between 2 and 4"):
        cc.compare(state, {"agents": names[:1], "prompt": "hello", "confirmation": cc.COMPARE_TOKEN})
    with pytest.raises(ValueError, match="Write a prompt"):
        cc.compare(state, {"agents": names, "prompt": " ", "confirmation": cc.COMPARE_TOKEN})
    with pytest.raises(ValueError, match="max_tokens"):
        cc.compare(state, {"agents": names, "prompt": "hello", "max_tokens": 2,
                           "confirmation": cc.COMPARE_TOKEN})
    with pytest.raises(ValueError, match="Unknown agent"):
        cc.compare(state, {"agents": ["nope", names[0]], "prompt": "hello",
                           "confirmation": cc.COMPARE_TOKEN})


def test_a_comparison_really_calls_each_model_and_stores_the_answer(state, monkeypatch):
    cfg = state.config
    names = sorted(cfg.agents)[:2]
    _scripted_registry(monkeypatch)
    started = cc.compare(state, {"agents": names, "prompt": "What is a token?",
                                 "max_tokens": 32, "confirmation": cc.COMPARE_TOKEN})
    job = started["job"]
    deadline = time.time() + 20
    while job["status"] == "running" and time.time() < deadline:
        time.sleep(0.05)
        job = state.workbench.jobs[job["id"]]
    assert job["status"] == "done", job
    found = cc.compare_status(state, job["id"])
    assert found["status"] == "done"
    results = found["result"]["results"]
    assert [row["agent"] for row in results] == names
    assert all(row["ok"] for row in results)
    assert all(row["input_tokens"] == 30 for row in results)
    assert all(row["latency_ms"] is not None for row in results)
    assert cc.comparisons(state.workspace)[0]["id"] == job["id"]
    assert "answer from" in json.dumps(results)


def test_one_failing_model_does_not_hide_the_other(state, monkeypatch):
    names = sorted(state.config.agents)[:2]
    _scripted_registry(monkeypatch, fail={names[1]})
    started = cc.compare(state, {"agents": names, "prompt": "hi", "confirmation": cc.COMPARE_TOKEN})
    job = started["job"]
    deadline = time.time() + 20
    while job["status"] == "running" and time.time() < deadline:
        time.sleep(0.05)
        job = state.workbench.jobs[job["id"]]
    results = cc.compare_status(state, job["id"])["result"]["results"]
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False and "provider refused" in results[1]["error"]


def test_a_comparison_is_blocked_by_the_budget(state, monkeypatch):
    _trace(state.workspace, models=[_call(calculated_model_cost_usd=0.9)])
    cc.sync(state.workspace, force=True)
    _save(state, budget=_budget(daily_usd=0.1))
    names = sorted(state.config.agents)[:2]
    with pytest.raises(cc.BudgetExceeded):
        cc.compare(state, {"agents": names, "prompt": "hi", "confirmation": cc.COMPARE_TOKEN})


# ── resources ─────────────────────────────────────────────────────────── #


def test_a_sample_is_stored_and_an_interval_is_respected(state, monkeypatch):
    monkeypatch.setattr(state.workbench, "metrics", lambda: {
        "data": {"cpu_percent": 12.5, "ram": {"used": 2 ** 30, "total": 4 * 2 ** 30},
                 "gpu_devices": [{"utilization_percent": 30.0, "memory_used_mib": 512,
                                  "memory_total_mib": 8192}]},
        "source": "psutil + optional nvidia-smi"})
    first = cc.resources(state)
    assert first["sample"]["cpu"] == 12.5
    assert first["history"][0]["gpu_util"] == 30.0
    second = cc.resources(state)
    assert second["sample"] is None, "the interval keeps the page from hammering the machine"
    assert len(second["history"]) == 1


def test_sampling_off_records_nothing(state, monkeypatch):
    calls = []
    monkeypatch.setattr(state.workbench, "metrics", lambda: calls.append(1) or {"data": {}})
    _save(state, resources={"enabled": False, "interval_s": 15, "keep_hours": 48})
    body = cc.resources(state)
    assert body["sample"] is None and calls == []


# ── cleanup ───────────────────────────────────────────────────────────── #


def test_clearing_the_ledger_needs_its_token(state):
    _trace(state.workspace, models=[_call()])
    cc.sync(state.workspace, force=True)
    with pytest.raises(ValueError, match="Type CLEAR COST HISTORY"):
        cc.cleanup(state, {})
    body = cc.cleanup(state, {"confirmation": cc.CLEAR_TOKEN})
    assert body["removed"]["call"] == 1
    assert cc.breakdown(state, 7, "model")["totals"]["calls"] == 0, "a plain sync must not resurrect it"
    assert cc.sync(state.workspace, force=True)["imported"] == 0
    with pytest.raises(ValueError, match="no cost history"):
        cc.cleanup(state, {"confirmation": cc.CLEAR_TOKEN})


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


def test_every_cost_read_needs_the_gui_token(live):
    for path in ("overview", "policy", "report", "latency", "breakdown", "budget",
                 "providers", "resources", "comparisons"):
        status, body = _call_http(live["base"] + "/api/cost/" + path, token=None)
        assert status == 401, path
        assert "error" in body


def test_the_overview_shape_drives_the_page(live):
    status, body = _call_http(live["base"] + "/api/cost/overview")
    assert status == 200 and body["ok"] is True
    data = body["data"]
    assert data["budget"]["enabled"] is False and data["budget"]["enforced"] is False
    assert data["spend"]["today"]["usd"] == 0
    assert data["tokens"]["save"] == cc.SAVE_TOKEN
    assert data["store"]["exists"] is False


def test_a_trace_appears_over_http_after_a_sync(live):
    _trace(live["repo"], models=[_call()], wires=[(time.time() - 4, time.time() - 3, "worker")])
    status, body = _call_http(live["base"] + "/api/cost/report?days=7")
    assert status == 200
    assert body["data"]["by_model"][0]["calls"] == 1
    status, body = _call_http(live["base"] + "/api/cost/latency?days=7&group=agent")
    assert status == 200 and body["data"]["group"] == "agent"


def test_a_bad_window_is_a_precise_400(live):
    status, body = _call_http(live["base"] + "/api/cost/breakdown?days=0")
    assert status == 400 and body["error"]["code"] == "invalid_request"
    assert "between 1 and 365" in body["error"]["message"]
    status, body = _call_http(live["base"] + "/api/cost/breakdown?days=7&group=nope")
    assert status == 400 and "Group by" in body["error"]["message"]
    status, body = _call_http(live["base"] + "/api/cost/comparisons?limit=999")
    assert status == 200, "a too-large limit is clamped, not refused"


def test_a_write_without_its_token_is_refused_twice(live):
    status, body = _call_http(live["base"] + "/api/cost/policy/save", {"budget": _budget()})
    assert status == 400 and body["error"]["code"] == "invalid_request"
    assert "SAVE COST POLICY" in body["error"]["message"]
    status, body = _call_http(live["base"] + "/api/cost/nope", {})
    assert status == 404
    status, body = _call_http(live["base"] + "/api/cost/typo")
    assert status == 404


def test_a_budget_exceeded_comparison_is_409(live):
    _trace(live["repo"], models=[_call(calculated_model_cost_usd=0.9)])
    cc.sync(live["repo"], force=True)
    view = cc.policy_view(live["repo"])
    _call_http(live["base"] + "/api/cost/policy/save",
               {"confirmation": cc.SAVE_TOKEN, "revision": view["revision"], "budget": _budget(daily_usd=0.1)})
    names = sorted(live["state"].config.agents)[:2]
    status, body = _call_http(live["base"] + "/api/cost/compare",
                              {"agents": names, "prompt": "hi", "confirmation": cc.COMPARE_TOKEN})
    assert status == 409 and body["error"]["code"] == "budget_exceeded"


def test_cost_reads_are_rate_limited(live):
    live["state"].api_limiter = Limiter(limit=2, window=60)
    assert _call_http(live["base"] + "/api/cost/overview")[0] == 200
    assert _call_http(live["base"] + "/api/cost/policy")[0] == 200
    status, body = _call_http(live["base"] + "/api/cost/budget")
    assert status == 429 and body["error"]["code"] == "rate_limited"
