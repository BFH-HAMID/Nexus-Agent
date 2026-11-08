"""Offline tests for TASK 7: scheduling, crash checkpoint/resume, idempotency, watchdogs, self-monitoring, replay, and daemon integration."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from nexus.autonomy import load_autonomy_dict, resolve_timezone
from nexus.chief import (
    _ACTIVE_RUNTIMES,
    ChiefOfStaff,
    MissionStep,
    answer_status_question,
    load_mission,
    save_mission,
)
from nexus.cli import main
from nexus.config import load_config
from nexus.idempotency import IdempotencyLedger, compute_idempotency_key, is_external_action
from nexus.jarvis.daemon import Daemon
from nexus.monitoring import collect_self_monitoring
from nexus.notifications import NotificationRouter
from nexus.scheduler import Scheduler, parse_schedule_expression
from nexus.server import ServerState
from nexus.tools import ApprovalDecision, ApprovalGate, PathGuard, ToolContext, build_tools
from nexus.types import Completion, ToolCall
from nexus.watchdog import check_stuck_missions

from .conftest import FakeProvider, fake_registry, finish_completion, tool_completion


@pytest.fixture(autouse=True)
def _isolated_nexus_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "nexus-home"))


def _enable_chief(repo: Path, **overrides) -> None:
    data = {
        "schema": 1,
        "enabled": True,
        "concurrency": 2,
        "max_retries": 0,
        "retry_backoff_seconds": 0.0,
        "max_cost_usd": 5.0,
        "critic_enabled": False,
        "auto_start_autonomy_levels": ["L2", "L3"],
        "require_approval_for_high_risk": True,
        **overrides,
    }
    (repo / "chief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _write_autonomy(repo: Path, **overrides) -> None:
    data = {
        "schema": 1,
        "level": "L1",
        **overrides,
    }
    (repo / "autonomy.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _make_providers(repo: Path, completions_by_agent: dict[str, list]):
    cfg = load_config(repo)
    cfg.settings.workspace = repo
    cfg.settings.prefer_local = False
    providers_list = [
        FakeProvider(name, list(items))
        for name, items in completions_by_agent.items()
    ]
    return cfg, fake_registry(*providers_list, config=cfg)


# --------------------------------------------------------------------------- #
# 1. Default-OFF posture & strict operator policy validation
# --------------------------------------------------------------------------- #


def test_task7_defaults_off_and_autonomy_validation(repo):
    policy = load_autonomy_dict(repo)
    assert policy["level"] == "L1"
    assert policy["scheduler"]["enabled"] is False
    assert policy["watchdog"]["enabled"] is False

    sched = Scheduler(repo)
    sched.add("every day at 7 run the market report", timezone="Asia/Dhaka")
    # Default scheduler.enabled=false must never dispatch even when forced past due time
    assert sched.tick(now=time.time() + 200_000) == []

    # Invalid timezone or bad watchdog bounds fail closed
    _write_autonomy(repo, timezone="Invalid/Not_A_Zone")
    with pytest.raises(ValueError, match="Unknown IANA"):
        load_autonomy_dict(repo)

    _write_autonomy(repo, watchdog={"enabled": True, "max_identical_tool_calls": 1})
    with pytest.raises(ValueError, match="2..50"):
        load_autonomy_dict(repo)


# --------------------------------------------------------------------------- #
# 2. Scheduler: cron, English & Bangla NL, timezone awareness, idempotency
# --------------------------------------------------------------------------- #


def test_scheduler_parses_cron_english_bangla_and_timezones(repo):
    # Reference timestamp: Sunday 2026-11-08 06:00:00 UTC == 12:00:00 Asia/Dhaka (+06:00)
    ref_utc = datetime(2026, 11, 8, 6, 0, tzinfo=ZoneInfo("UTC")).timestamp()

    # English recurring: "every day at 7 run the market report" in Asia/Dhaka (+06:00)
    p_en_daily = parse_schedule_expression(
        "every day at 7 run the market report",
        timezone="Asia/Dhaka",
        now=ref_utc,
    )
    assert p_en_daily.kind == "cron"
    assert p_en_daily.cron_expr == "0 7 * * *"
    assert "market report" in p_en_daily.task
    nxt_dhaka = datetime.fromtimestamp(p_en_daily.next_run_at, tz=ZoneInfo("Asia/Dhaka"))
    assert (nxt_dhaka.year, nxt_dhaka.month, nxt_dhaka.day, nxt_dhaka.hour, nxt_dhaka.minute) == (2026, 11, 9, 7, 0)

    # English delayed: "next Friday remind me"
    p_en_fri = parse_schedule_expression(
        "next Friday remind me",
        timezone="America/New_York",
        now=ref_utc,
    )
    assert p_en_fri.kind == "once"
    assert "remind me" in p_en_fri.task
    fri_ny = datetime.fromtimestamp(p_en_fri.next_run_at, tz=ZoneInfo("America/New_York"))
    assert fri_ny.weekday() == 4  # Friday
    assert (fri_ny.year, fri_ny.month, fri_ny.day) == (2026, 11, 13)

    # Cron expression with explicit task
    p_cron = parse_schedule_expression("*/15 9-17 * * 1-5 check pipeline", timezone="UTC", now=ref_utc)
    assert p_cron.kind == "cron"
    assert p_cron.cron_expr == "*/15 9-17 * * 1-5"
    assert p_cron.task == "check pipeline"
    nxt_cron = datetime.fromtimestamp(p_cron.next_run_at, tz=ZoneInfo("UTC"))
    assert nxt_cron.weekday() == 0  # Monday
    assert (nxt_cron.hour, nxt_cron.minute) == (9, 0)

    # Bangla daily with Bangla digits: "প্রতিদিন সকাল ৭টায় বাজারের রিপোর্ট চালাও"
    p_bn_daily = parse_schedule_expression(
        "প্রতিদিন সকাল ৭টায় বাজারের রিপোর্ট চালাও",
        timezone="Asia/Dhaka",
        now=ref_utc,
    )
    assert p_bn_daily.kind == "cron"
    assert p_bn_daily.cron_expr == "0 7 * * *"
    assert "বাজারের রিপোর্ট" in p_bn_daily.task

    # Bangla next weekday: "আগামী শুক্রবার বিকাল ৩টায় মনে করিয়ে দিও"
    p_bn_fri = parse_schedule_expression(
        "আগামী শুক্রবার বিকাল ৩টায় মনে করিয়ে দিও",
        timezone="Asia/Dhaka",
        now=ref_utc,
    )
    assert p_bn_fri.kind == "once"
    bn_fri_dt = datetime.fromtimestamp(p_bn_fri.next_run_at, tz=ZoneInfo("Asia/Dhaka"))
    assert bn_fri_dt.weekday() == 4
    assert (bn_fri_dt.hour, bn_fri_dt.minute) == (15, 0)

    # Bangla relative delay: "৩০ মিনিট পর টেস্ট চালাও"
    p_bn_rel = parse_schedule_expression("৩০ মিনিট পর টেস্ট চালাও", timezone="Asia/Dhaka", now=ref_utc)
    assert p_bn_rel.kind == "once"
    assert pytest.approx(p_bn_rel.next_run_at, abs=1.0) == ref_utc + 1800

    # Timezone resolution from project .nexus/agents.yaml settings & NEXUS_TIMEZONE
    (repo / ".nexus").mkdir(exist_ok=True)
    (repo / ".nexus" / "agents.yaml").write_text(
        yaml.safe_dump({"settings": {"timezone": "Asia/Dhaka"}}),
        encoding="utf-8",
    )
    assert resolve_timezone(repo) == "Asia/Dhaka"


def test_scheduler_persists_dispatches_idempotently_and_honors_l1(repo):
    _write_autonomy(repo, level="L1", scheduler={"enabled": True, "timezone": "Asia/Dhaka"})
    now_ref = datetime(2026, 11, 8, 6, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    clock_val = [now_ref]
    studio = ServerState(repo, quiet=True).workbench.studio
    sched = Scheduler(repo, studio=studio, clock=lambda: clock_val[0])

    once_entry = sched.add("in 5 minutes check market brief", priority=3)
    daily_entry = sched.add("every day at 13 summarize reports")
    assert len(sched.list()) == 2

    # Not due yet
    assert sched.tick() == []

    # Advance 6 minutes -> one-shot fires ONCE, disables itself, and lands in awaiting_approval (L1)
    clock_val[0] = now_ref + 360
    fired = sched.tick()
    assert len(fired) == 1
    assert fired[0]["schedule_id"] == once_entry.id
    assert sched.get(once_entry.id).enabled is False
    assert sched.get(once_entry.id).run_count == 1
    # Re-tick at same timestamp does not duplicate
    assert sched.tick() == []

    jobs = studio.items("jarvis-job")
    assert len(jobs) == 1
    assert jobs[0]["status"] == "awaiting_approval"
    assert jobs[0]["source"] == "event:schedule"

    # Advance past 13:00 Asia/Dhaka (07:00 UTC) -> daily cron fires & advances next_run_at to next day
    clock_val[0] = datetime(2026, 11, 8, 7, 5, tzinfo=ZoneInfo("UTC")).timestamp()
    fired_daily = sched.tick()
    assert len(fired_daily) == 1
    assert fired_daily[0]["schedule_id"] == daily_entry.id
    updated_daily = sched.get(daily_entry.id)
    assert updated_daily.enabled is True
    assert updated_daily.run_count == 1
    assert updated_daily.next_run_at > clock_val[0]


# --------------------------------------------------------------------------- #
# 3. Crash checkpoint & exact-once resume + idempotency keys
# --------------------------------------------------------------------------- #


def test_idempotency_ledger_prevents_duplicate_external_action_on_resume(repo, monkeypatch):
    # Direct registry check on external tool with idempotency_scope
    assert is_external_action("git", {"args": ["push", "origin", "HEAD"]}) is True
    assert is_external_action("git", {"args": ["status"]}) is False
    assert is_external_action("gh_create_pr", {"title": "feat"}) is True
    assert is_external_action("read_file", {"path": "README.md"}) is False

    calls = {"count": 0}
    gate = ApprovalGate(mode="ask", callback=lambda *_a: ApprovalDecision(True, scope="once"))
    ctx = ToolContext(repo, PathGuard(repo), gate)
    ctx.state["idempotency_scope"] = "chief-abc123:step2"
    reg = build_tools(ctx)

    # Replace gh_create_pr handler with safe offline counter double
    spec = next(s for s in reg.specs() if s.name == "gh_create_pr")

    def fake_create_pr(args):
        calls["count"] += 1
        return f"PR #42 created: {args.get('title')}"

    reg.register(
        "gh_create_pr",
        spec.description,
        spec.input_schema,
        fake_create_pr,
        risk=spec.risk,
        readonly_subcommands=spec.readonly_subcommands,
        readonly_actions=spec.readonly_actions,
        approval_context=spec.approval_context,
    )

    out1, err1 = reg.call("gh_create_pr", {"title": "Nightly market report"})
    assert not err1 and "PR #42 created" in out1
    assert calls["count"] == 1

    # Simulate restart/resume of the SAME mission step calling the same external action again
    gate2 = ApprovalGate(mode="ask", callback=lambda *_a: ApprovalDecision(False, reason="should not even need re-run"))
    ctx2 = ToolContext(repo, PathGuard(repo), gate2)
    ctx2.state["idempotency_scope"] = "chief-abc123:step2"
    reg2 = build_tools(ctx2)
    reg2.register(
        "gh_create_pr",
        spec.description,
        spec.input_schema,
        fake_create_pr,
        risk=spec.risk,
        readonly_subcommands=spec.readonly_subcommands,
        readonly_actions=spec.readonly_actions,
        approval_context=spec.approval_context,
    )
    out2, err2 = reg2.call("gh_create_pr", {"title": "Nightly market report"})
    assert not err2
    assert "idempotent skip" in out2.lower()
    assert calls["count"] == 1  # External side effect never repeated!

    ledger = IdempotencyLedger(repo)
    key = compute_idempotency_key("chief-abc123:step2", "gh_create_pr", {"title": "Nightly market report"})
    assert ledger.lookup(key)["status"] == "completed"


def test_simulated_crash_mid_mission_resumes_from_last_completed_step_without_repeating(repo):
    _enable_chief(repo)
    _write_autonomy(repo, level="L2", max_depth=0)
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")

    step_calls = {"step1": 0, "step2": 0, "step3": 0}
    external_side_effects = {"count": 0}

    class CrashAwareProvider(FakeProvider):
        def __init__(self, name: str, crash_on_second_step: bool = True) -> None:
            super().__init__(name, [])
            self.crash_on_second_step = crash_on_second_step

        def complete(self, system, messages, tools=None, **kwargs):
            first_user = messages[0].content if messages else ""
            txt = first_user if isinstance(first_user, str) else json.dumps(first_user, default=str)
            has_tool_result = any(
                isinstance(m.content, list)
                and any(getattr(b, "type", "") == "tool_result" or hasattr(b, "tool_use_id") for b in m.content)
                for m in messages
            )
            if "Your step: step1" in txt:
                step_calls["step1"] += 1
                return finish_completion("step1 completed cleanly")
            if "Your step: step2" in txt:
                if not has_tool_result:
                    step_calls["step2"] += 1
                    return tool_completion([("write_file", {"path": "step2.txt", "content": "checkpointed"})])
                if self.crash_on_second_step:
                    raise RuntimeError("Simulated power loss mid-step2")
                return finish_completion("step2 finished after recovery")
            if "Your step: step3" in txt:
                step_calls["step3"] += 1
                return finish_completion("step3 finished")
            return finish_completion("done")

    # Configure fake providers for all configured agents so fallback never masks the crash
    cfg = load_config(repo)
    cfg.settings.workspace = repo
    cfg.settings.prefer_local = False
    crash_providers = fake_registry(
        *(CrashAwareProvider(name, crash_on_second_step=True) for name in cfg.agents),
        config=cfg,
    )

    chief1 = ChiefOfStaff(
        repo,
        config=cfg,
        providers=crash_providers,
        approve_callback=lambda _m: True,
        tool_approval_callback=lambda *_a: ApprovalDecision(True, scope="once"),
        auto_yes=True,
    )
    steps = [
        MissionStep(id="step1", title="step1", role="research", agent="gemini", read_only=True, instruction="Step 1"),
        MissionStep(id="step2", title="step2", role="backend", agent="gemini", depends_on=["step1"], instruction="Step 2"),
        MissionStep(id="step3", title="step3", role="docs", agent="gemini", depends_on=["step2"], read_only=True, instruction="Step 3"),
    ]
    mission = chief1.plan("Three step crash test", steps=steps)

    # Pre-record an external idempotent action in step2 before simulated process death
    idem_key = compute_idempotency_key(f"{mission.id}:step2", "gh_create_pr", {"title": "Report PR"})
    IdempotencyLedger(repo).record(idem_key, scope=f"{mission.id}:step2", tool="gh_create_pr", result_preview="PR #99")

    # Simulate execution crashing during step2: step1 finishes and persists checkpoint, step2 dies mid-flight
    crashed = chief1.execute(mission, auto_approve=True)
    assert crashed.step("step1").status == "done"
    assert crashed.last_completed_step_id == "step1"
    assert step_calls["step1"] == 1
    assert step_calls["step2"] == 1
    assert step_calls["step3"] == 0

    # Force persisted state to look like an abrupt process kill mid-step2 while status=='running'
    crashed.status = "running"
    crashed.step("step2").status = "running"
    save_mission(repo, crashed)
    _ACTIVE_RUNTIMES.clear()

    # Now simulate process restart with a fresh ChiefOfStaff instance
    recovered_providers = fake_registry(
        *(CrashAwareProvider(name, crash_on_second_step=False) for name in cfg.agents),
        config=cfg,
    )
    chief2 = ChiefOfStaff(
        repo,
        config=cfg,
        providers=recovered_providers,
        approve_callback=lambda _m: True,
        tool_approval_callback=lambda *_a: ApprovalDecision(True, scope="once"),
        auto_yes=True,
    )
    # First test recover_missions(auto_resume=False) marks orphaned running mission interrupted cleanly
    recovered_list = chief2.recover_missions(auto_resume=False)
    assert any(m.id == mission.id and m.status == "interrupted" for m in recovered_list)
    assert load_mission(repo, mission.id).step("step1").status == "done"
    assert load_mission(repo, mission.id).step("step2").status == "pending"

    # Also verify external side-effect tool in step2 scope is skipped idempotently after restart
    def should_not_run(_args):
        external_side_effects["count"] += 1
        return "SHOULD NOT REPEAT"

    ctx_chk = ToolContext(repo, PathGuard(repo), ApprovalGate(mode="ask"))
    ctx_chk.state["idempotency_scope"] = f"{mission.id}:step2"
    reg_chk = build_tools(ctx_chk)
    pr_spec = next(s for s in reg_chk.specs() if s.name == "gh_create_pr")
    reg_chk.register("gh_create_pr", pr_spec.description, pr_spec.input_schema, should_not_run, risk=pr_spec.risk)
    skip_msg, skip_err = reg_chk.call("gh_create_pr", {"title": "Report PR"})
    assert not skip_err and "idempotent skip" in skip_msg.lower()
    assert external_side_effects["count"] == 0

    # Resume mission -> picks up at step2 & step3 without re-running step1!
    resumed = chief2.resume(mission.id)
    assert resumed.status == "done"
    assert resumed.resume_count == 1
    assert step_calls["step1"] == 1  # Never re-executed!
    assert step_calls["step2"] == 2
    assert step_calls["step3"] == 1
    assert (repo / "step2.txt").read_text(encoding="utf-8") == "checkpointed"


# --------------------------------------------------------------------------- #
# 4. Watchdogs: stuck step, infinite tool loop, runaway cost + notifications
# --------------------------------------------------------------------------- #


def test_stuck_step_watchdog_stops_mission_and_notifies(repo):
    _enable_chief(repo)
    _write_autonomy(
        repo,
        level="L2",
        watchdog={"enabled": True, "step_timeout_seconds": 0.15, "poll_seconds": 0.03, "notify_on_trigger": True},
        notifications={"enabled": True, "presence": "away", "away_channel": "desktop"},
    )
    release_hung = threading.Event()
    notices: list[tuple[str, str]] = []

    class HangingProvider(FakeProvider):
        def complete(self, system, messages, tools=None, **kwargs):
            release_hung.wait(timeout=3.0)
            return finish_completion("late")

    cfg = load_config(repo)
    cfg.settings.workspace = repo
    cfg.settings.prefer_local = False
    providers = fake_registry(
        HangingProvider("gemini", []),
        HangingProvider("hermes", []),
        HangingProvider("deepseek", []),
        config=cfg,
    )
    router = NotificationRouter(repo, desktop_notifier=lambda t, m: notices.append((t, m)) or True)
    chief = ChiefOfStaff(
        repo,
        config=cfg,
        providers=providers,
        approve_callback=lambda _m: True,
        notifications=router,
        step_timeout_seconds=0.15,
        watchdog_poll_seconds=0.03,
    )
    try:
        steps = [MissionStep(id="hang", title="hang", role="research", agent="gemini", read_only=True, instruction="Wait forever")]
        t0 = time.monotonic()
        mission = chief.run("Hang test", steps=steps, auto_approve=True)
        elapsed = time.monotonic() - t0
    finally:
        release_hung.set()

    assert elapsed < 2.0  # Watchdog aborted without hanging on blocked provider
    assert mission.status == "failed"
    assert mission.watchdog_reason == "stuck_step"
    assert "stuck step" in mission.error.lower()
    assert any(entry.get("event") == "watchdog.triggered" and entry.get("kind") == "stuck_step" for entry in mission.logs)
    assert notices and "stuck_step" in notices[0][0]
    why_answer = answer_status_question(repo, "why did that fail?", mission_id=mission.id)
    assert "stuck_step" in why_answer or "watchdog" in why_answer.lower()


def test_infinite_loop_and_runaway_cost_watchdogs_stop_and_notify(repo):
    _enable_chief(repo, estimated_rates={"gemini": {"input_per_million": 100.0, "output_per_million": 100.0}})
    _write_autonomy(
        repo,
        level="L2",
        watchdog={"enabled": True, "max_identical_tool_calls": 3, "notify_on_trigger": True},
        notifications={"enabled": True, "presence": "away", "away_channel": "desktop"},
    )
    (repo / "README.md").write_text("# Hi\n", encoding="utf-8")
    loop_notices: list[tuple[str, str]] = []
    router = NotificationRouter(repo, desktop_notifier=lambda t, m: loop_notices.append((t, m)) or True)

    # 4a. Infinite identical tool call loop
    repeated = [tool_completion([("read_file", {"path": "README.md"})]) for _ in range(6)]
    cfg, providers = _make_providers(repo, {"gemini": repeated, "hermes": repeated, "deepseek": repeated})
    chief = ChiefOfStaff(
        repo,
        config=cfg,
        providers=providers,
        approve_callback=lambda _m: True,
        notifications=router,
        max_identical_tool_calls=3,
    )
    loop_steps = [MissionStep(id="looper", title="looper", role="research", agent="gemini", read_only=True, instruction="Loop")]
    m_loop = chief.run("Loop test", steps=loop_steps, auto_approve=True)
    assert m_loop.status == "failed"
    assert m_loop.watchdog_reason == "infinite_loop"
    assert "infinite loop" in m_loop.error.lower()
    assert loop_notices and "infinite_loop" in loop_notices[0][0]

    # 4b. Runaway cost cap mid-step
    cost_notices: list[tuple[str, str]] = []
    cost_router = NotificationRouter(repo, desktop_notifier=lambda t, m: cost_notices.append((t, m)) or True)
    expensive = [
        Completion(
            content=[ToolCall(id="call_cost_1", name="read_file", input={"path": "README.md"})],
            stop_reason="tool_use",
            model="fake-model",
            input_tokens=5_000,
            output_tokens=5_000,
        ),
        Completion(
            content=[ToolCall(id="call_cost_2", name="finish", input={"summary": "should never reach"})],
            stop_reason="tool_use",
            model="fake-model",
            input_tokens=5_000,
            output_tokens=5_000,
        ),
    ]
    cfg2, providers2 = _make_providers(repo, {"gemini": expensive, "hermes": expensive, "deepseek": expensive})
    chief_cost = ChiefOfStaff(
        repo,
        config=cfg2,
        providers=providers2,
        approve_callback=lambda _m: True,
        notifications=cost_router,
    )
    cost_steps = [MissionStep(id="spender", title="spender", role="research", agent="gemini", read_only=True, instruction="Spend")]
    m_cost = chief_cost.run("Cost watchdog test", steps=cost_steps, cost_cap_usd=0.05, auto_approve=True)
    assert m_cost.status == "aborted_cost_cap"
    assert m_cost.watchdog_reason == "runaway_cost"
    assert any(e.get("event") == "watchdog.triggered" and e.get("kind") == "runaway_cost" for e in m_cost.logs)
    assert cost_notices and "runaway_cost" in cost_notices[0][0]


def test_disk_stuck_mission_check_detects_stale_running_mission(repo):
    _enable_chief(repo)
    _write_autonomy(repo, level="L2", watchdog={"enabled": True, "step_timeout_seconds": 30.0})
    chief = ChiefOfStaff(repo)
    m = chief.plan("Stale running mission")
    m.status = "running"
    m.started_at = 1000.0
    m.steps[0].status = "running"
    m.steps[0].started_at = 1000.0
    m.steps[0].last_progress_at = 1000.0
    save_mission(repo, m)

    stopped = check_stuck_missions(repo, now=1060.0)
    assert len(stopped) == 1
    assert stopped[0]["mission_id"] == m.id
    reloaded = load_mission(repo, m.id)
    assert reloaded.status == "failed"
    assert reloaded.watchdog_reason == "stuck_step"


# --------------------------------------------------------------------------- #
# 5. Self-monitoring, HUD/Server APIs, and Mission History + Replay
# --------------------------------------------------------------------------- #


def test_self_monitoring_history_replay_and_api_endpoints(repo):
    fastapi = pytest.importorskip("fastapi")
    assert fastapi is not None
    from fastapi.testclient import TestClient

    from nexus.hud.app import create_app

    _enable_chief(repo, estimated_rates={"gemini": {"input_per_million": 1.0, "output_per_million": 2.0}})
    _write_autonomy(repo, level="L2", scheduler={"enabled": True, "timezone": "Asia/Dhaka"})
    (repo / "notes.txt").write_text("initial\n", encoding="utf-8")

    # Run a real workspace tool mission so SessionStore records a trace linked on MissionStep
    step_completions = [
        Completion(
            content=[ToolCall(id="call_write_1", name="write_file", input={"path": "report.txt", "content": "ok\n"})],
            stop_reason="tool_use",
            model="fake-model",
            input_tokens=500,
            output_tokens=200,
        ),
        Completion(
            content=[ToolCall(id="call_finish_1", name="finish", input={"summary": "Created report.txt"})],
            stop_reason="tool_use",
            model="fake-model",
            input_tokens=300,
            output_tokens=100,
        ),
    ]
    cfg, providers = _make_providers(repo, {"gemini": step_completions, "hermes": step_completions, "deepseek": step_completions})
    chief = ChiefOfStaff(
        repo,
        config=cfg,
        providers=providers,
        approve_callback=lambda _m: True,
        tool_approval_callback=lambda *_a: ApprovalDecision(True, scope="once"),
        auto_yes=True,
    )
    m_ok = chief.run(
        "Create report",
        steps=[MissionStep(id="write_step", title="write", role="backend", agent="gemini", instruction="Write report.txt")],
        auto_approve=True,
    )
    assert m_ok.status == "done"
    assert m_ok.step("write_step").trace_id
    assert (repo / "report.txt").read_text(encoding="utf-8") == "ok\n"

    # Also enqueue a Jarvis job & add a schedule so monitoring reflects all metrics
    state = ServerState(repo, quiet=True)
    studio = state.workbench.studio
    from nexus.jarvis.queue import Queue

    Queue(studio).enqueue("Pending queued check")
    Scheduler(repo, studio=studio).add("every day at 8 run checks")

    snap = collect_self_monitoring(repo, studio=studio)
    assert snap["queue_depth"]["total"] >= 1
    assert snap["cost_today"]["usd"] > 0
    assert snap["error_rate"]["total_finished"] >= 1
    assert 0.0 <= snap["error_rate"]["rate"] <= 1.0
    assert snap["scheduler"]["active"] == 1

    # History & Replay reusing nexus/evaluation/replay.py
    hist = chief.history()
    assert any(item["id"] == m_ok.id and m_ok.step("write_step").trace_id in item["trace_ids"] for item in hist)

    inspected = chief.replay(m_ok.id, mode="inspect")
    assert inspected["effects"] is False
    assert inspected["steps"][0]["traces"][0]["state"] == "finished"

    why_data = chief.replay(m_ok.id, mode="why")
    assert why_data["effects"] is False
    assert "decisions" in why_data["steps"][0]["traces"][0]

    # Test safe re-execution of the write_file step via replay.reexecute
    events = inspected["steps"][0]["traces"][0]["events"]
    write_idx = next(i for i, e in enumerate(events) if e["kind"] == "tool_call" and e["data"]["tool"] == "write_file")
    (repo / "report.txt").write_text("changed\n", encoding="utf-8")
    reexec = chief.replay(
        m_ok.id,
        step_id="write_step",
        trace_step=write_idx,
        mode="reexecute",
        approval_callback=lambda *_a: ApprovalDecision(True, scope="once"),
    )
    assert reexec["effects"] is True
    assert reexec["tool"] == "write_file"
    assert (repo / "report.txt").read_text(encoding="utf-8") == "ok\n"

    # Verify FastAPI HUD endpoints + Mission.summary()
    token = "hud-task7-token-" + "z" * 32
    with TestClient(create_app(studio, token, allowed_hosts=["testserver"])) as client:
        headers = {"Authorization": f"Bearer {token}"}
        hud_state = client.get("/api/state", headers=headers).json()
        assert "monitoring" in hud_state
        assert hud_state["monitoring"]["queue_depth"]["total"] >= 1

        mon_res = client.get("/api/monitoring", headers=headers)
        assert mon_res.status_code == 200
        assert mon_res.json()["cost_today"]["usd"] > 0

        sched_res = client.get("/api/schedules", headers=headers)
        assert sched_res.status_code == 200
        assert len(sched_res.json()["items"]) == 1

        replay_res = client.get(f"/api/chief/missions/{m_ok.id}/replay?mode=why", headers=headers)
        assert replay_res.status_code == 200
        assert replay_res.json()["effects"] is False

    # Verify Mission.summary() includes monitoring
    summary = state.mission.summary(force=True)
    assert summary["monitoring"]["queue_depth"]["total"] >= 1

    # Verify stdlib nexus/server.py HTTP routes directly
    from io import BytesIO
    from types import SimpleNamespace

    from nexus.server import make_handler

    srv_state = ServerState(repo, token="srv-task7-tok", quiet=True)
    handler_cls = make_handler(srv_state)

    def _invoke(method: str, path: str, body: dict | None = None):
        raw_body = json.dumps(body).encode("utf-8") if body is not None else b""
        out = BytesIO()
        h = handler_cls.__new__(handler_cls)
        h.rfile = BytesIO(raw_body)
        h.wfile = out
        h.server = SimpleNamespace(server_address=("127.0.0.1", 8787))
        h.requestline = f"{method} {path} HTTP/1.1"
        h.request_version = "HTTP/1.1"
        h.command = method
        h.path = path
        h.headers = {
            "Host": "127.0.0.1:8787",
            "X-Nexus-Token": "srv-task7-tok",
            "Content-Length": str(len(raw_body)),
        }
        h.client_address = ("127.0.0.1", 55555)
        h._session_cookie = None
        if method == "GET":
            h.do_GET()
        else:
            h.do_POST()
        resp = out.getvalue().decode("utf-8", errors="replace")
        head, _, payload = resp.partition("\r\n\r\n")
        status_code = int(head.splitlines()[0].split()[1])
        return status_code, json.loads(payload)

    sc, srv_mon = _invoke("GET", "/api/monitoring")
    assert sc == 200
    assert srv_mon["queue_depth"]["total"] >= 1

    sc, srv_scheds = _invoke("GET", "/api/schedules")
    assert sc == 200
    assert len(srv_scheds["items"]) == 1

    sc, srv_added = _invoke("POST", "/api/schedules", {"action": "add", "expression": "every day at 9 run audit"})
    assert sc == 201
    added_id = srv_added["id"]

    sc, srv_paused = _invoke("POST", "/api/schedules", {"action": "pause", "id": added_id})
    assert sc == 200 and srv_paused["enabled"] is False
    sc, srv_resumed = _invoke("POST", "/api/schedules", {"action": "resume", "id": added_id})
    assert sc == 200 and srv_resumed["enabled"] is True
    sc, srv_removed = _invoke("POST", "/api/schedules", {"action": "remove", "id": added_id})
    assert sc == 200 and srv_removed["removed"] is True

    sc, srv_hist = _invoke("GET", "/api/chief/history")
    assert sc == 200
    assert any(item["id"] == m_ok.id for item in srv_hist["items"])

    sc, srv_replay = _invoke("GET", f"/api/chief/missions/{m_ok.id}/replay?mode=why")
    assert sc == 200
    assert srv_replay["effects"] is False

    sc, srv_bad_replay = _invoke("GET", f"/api/chief/missions/{m_ok.id}/replay?mode=reexecute")
    assert sc == 400

    # Demo mode refuses mutating schedule POST
    srv_state.demo = True
    sc, _ = _invoke("POST", "/api/schedules", {"action": "add", "expression": "every day at 9 run audit"})
    assert sc == 409


# --------------------------------------------------------------------------- #
# 6. Daemon lifecycle integration & CLI smoke tests
# --------------------------------------------------------------------------- #


def test_daemon_tick_once_and_startup_reconciliation_preserve_l1(repo):
    _enable_chief(repo)
    studio = ServerState(repo, quiet=True).workbench.studio
    daemon = Daemon(studio)

    # Default L1 + scheduler.enabled=false: tick_once does not fire schedules
    Scheduler(repo, studio=studio).add("in 1 minute verify queue")
    future = time.time() + 120
    tick_off = daemon.tick_once(now=future)
    assert tick_off["scheduled"] == []
    assert studio.items("jarvis-job") == []

    # Enable scheduler only (runtime remains false, level remains L1): schedule admits awaiting_approval job, never executes!
    _write_autonomy(repo, level="L1", scheduler={"enabled": True})
    tick_on = daemon.tick_once(now=future)
    assert len(tick_on["scheduled"]) == 1
    jobs = studio.items("jarvis-job")
    assert len(jobs) == 1
    assert jobs[0]["status"] == "awaiting_approval"
    assert daemon.status()["execution_enabled"] is False

    # Startup reconciliation marks orphaned running Chief missions interrupted safely without auto-executing under L1
    chief = ChiefOfStaff(repo, studio=studio)
    orphan = chief.plan("Orphaned during reboot")
    orphan.status = "running"
    orphan.steps[0].status = "running"
    save_mission(repo, orphan)
    reconciled = daemon._reconcile_chief_on_startup(daemon.queue.policy())
    assert any(m.id == orphan.id and m.status == "interrupted" for m in reconciled)
    assert load_mission(repo, orphan.id).step(orphan.steps[0].id).status == "pending"


def test_cli_schedule_history_and_monitoring_commands(repo, capsys):
    _enable_chief(repo)
    assert main(["chief", "-C", str(repo), "schedule", "add", "every day at 7 run market report", "--json"]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["kind"] == "cron"
    assert added["cron_expr"] == "0 7 * * *"

    assert main(["chief", "-C", str(repo), "monitoring", "--json"]) == 0
    mon = json.loads(capsys.readouterr().out)
    assert "queue_depth" in mon and "error_rate" in mon

    assert main(["jarvis", "-C", str(repo), "monitoring"]) == 0
    jmon = json.loads(capsys.readouterr().out)
    assert jmon["scheduler"]["total"] == 1

    assert main(["chief", "-C", str(repo), "schedule", "pause", added["id"], "--json"]) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["enabled"] is False

    assert main(["chief", "-C", str(repo), "history", "--json"]) == 0
    hist = json.loads(capsys.readouterr().out)
    assert "items" in hist
