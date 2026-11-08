"""Offline tests for the opt-in Chief-of-Staff orchestrator (nexus/chief.py)."""

from __future__ import annotations

import json
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from nexus.chief import (
    ChiefDisabledError,
    ChiefError,
    ChiefOfStaff,
    Mission,
    MissionStep,
    compute_step_cost,
    detect_mission_control_intent,
    detect_mission_intent,
    load_chief_config,
    load_mission,
    select_agent_for_step,
    validate_dag,
)
from nexus.config import load_config
from nexus.permissions import Permissions
from nexus.tools import ApprovalDecision, ApprovalGate, PathGuard, ToolContext, build_tools
from nexus.types import Completion, Provider, ProviderError, ToolCall
from tests.conftest import fake_registry, finish_completion, text_completion, tool_completion


def _allow_safe_workspace_tools(spec, _args, _risk, _preview):
    return ApprovalDecision(spec.name in {"read_file", "list_files", "write_file", "edit_file", "grep", "finish"})


class _ThreadSafeDispatchProvider(Provider):
    """Thread-safe FakeProvider adapter for concurrent multi-branch missions."""

    kind = "fake"

    def __init__(self, name: str, handler, *, model: str = "fake-model", fallback: str | None = None) -> None:
        self.name = name
        self.model = model
        self.tool_mode = "native"
        self.supports_tools = True
        self.fallback = fallback
        self._handler = handler
        self._lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []

    def complete(self, system, messages, tools=None, *, max_tokens=None, temperature=None, model=None):
        call = {
            "system": system,
            "messages": list(messages),
            "tools": [t.name for t in (tools or [])],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "model": model,
        }
        with self._lock:
            self.calls.append(call)
        return self._handler(call, self)

    def health(self):
        return True, "ok"


def _enable_chief(
    repo: Path,
    *,
    easy_agent: str = "",
    medium_agent: str = "",
    hard_agent: str = "",
    extra_yaml: str = "",
) -> Path:
    cfg = repo / "chief.yaml"
    cfg.write_text(
        "schema: 1\n"
        "enabled: true\n"
        "concurrency: 3\n"
        "max_retries: 2\n"
        "retry_backoff_seconds: 0.0\n"
        "max_cost_usd: 2.0\n"
        "critic_enabled: true\n"
        "require_approval_for_high_risk: true\n"
        "model_routing:\n"
        f"  easy_agent: {json.dumps(easy_agent)}\n"
        f"  medium_agent: {json.dumps(medium_agent)}\n"
        f"  hard_agent: {json.dumps(hard_agent)}\n"
        "estimated_rates:\n"
        "  gemini: {input_per_million: 1.25, output_per_million: 5.0}\n"
        "  gemini-big: {input_per_million: 1.25, output_per_million: 5.0}\n"
        "  gemini-flash: {input_per_million: 0.10, output_per_million: 0.40}\n"
        "  deepseek: {input_per_million: 0.27, output_per_million: 1.10}\n"
        + extra_yaml,
        encoding="utf-8",
    )
    return cfg


def _set_autonomy(repo: Path, level: str = "L1", max_depth: int = 3) -> None:
    (repo / "autonomy.yaml").write_text(
        f"schema: 1\n"
        f"level: {level}\n"
        f"max_queue: 20\n"
        f"max_depth: {max_depth}\n"
        f"cooldown_seconds: 60\n"
        f"ttl_seconds: 86400\n"
        f"max_reserved_cost: 1.0\n",
        encoding="utf-8",
    )


def _registry_with(handlers: dict[str, Any], *, default_handler: Any = None, fallback_map: dict[str, str | None] | None = None):
    cfg = load_config()
    tier_agents = ["gemini", "gemini-flash", "gemini-big", "deepseek", "deepseek-pro", "openrouter", "openrouter-fast"]
    providers = []
    seen: set[str] = set()
    for name, fn in handlers.items():
        model = cfg.agents[name].model if name in cfg.agents else "fake-model"
        fb = (fallback_map or {}).get(name)
        providers.append(_ThreadSafeDispatchProvider(name, fn, model=model, fallback=fb))
        seen.add(name)
    if default_handler is not None:
        for name in tier_agents:
            if name not in seen and name in cfg.agents:
                providers.append(
                    _ThreadSafeDispatchProvider(
                        name,
                        default_handler,
                        model=cfg.agents[name].model,
                        fallback=(fallback_map or {}).get(name),
                    )
                )
    return fake_registry(*providers, config=cfg)


def _has_tool_result(messages: list[Any]) -> bool:
    for msg in messages:
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            for blk in content:
                if getattr(blk, "tool_use_id", None):
                    return True
    return False


def _user_text(messages: list[Any]) -> str:
    parts = []
    for msg in messages:
        if getattr(msg, "role", "") == "user":
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for blk in content:
                    if hasattr(blk, "text"):
                        parts.append(str(blk.text))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 1. Default OFF + operator_control + config validation
# --------------------------------------------------------------------------- #


def test_chief_default_off_and_operator_control(repo: Path):
    cfg = load_chief_config(repo)
    assert cfg.enabled is False

    chief = ChiefOfStaff(repo, providers=fake_registry())
    with pytest.raises(ChiefDisabledError, match="disabled"):
        chief.plan("Build an analytics service")

    # Read-only status still succeeds cleanly when disabled.
    st = chief.status()
    assert st["chief"]["enabled"] is False
    assert st["missions"] == []

    # chief.yaml is operator-owned and protected by Permissions + ApprovalGate.
    (repo / "chief.yaml").write_text("schema: 1\nenabled: false\n", encoding="utf-8")
    assert Permissions(repo).operator_control(repo / "chief.yaml") is True
    gate = ApprovalGate(mode="auto", callback=_allow_safe_workspace_tools)
    gate.permissions = Permissions(repo)
    tools = build_tools(ToolContext(workspace=repo, guard=PathGuard(repo), gate=gate))
    _content, is_error = tools.call("write_file", {"path": "chief.yaml", "content": "schema: 1\nenabled: true\n"})
    assert is_error is True
    assert "enabled: false" in (repo / "chief.yaml").read_text(encoding="utf-8")

    # Inline secret keys inside chief.yaml are rejected.
    (repo / "chief.yaml").write_text(
        "schema: 1\nenabled: true\napi_key: sk-1234567890abcdefghijklmnop\n",
        encoding="utf-8",
    )
    with pytest.raises(ChiefError, match="inline secrets"):
        load_chief_config(repo)


# --------------------------------------------------------------------------- #
# 2. Planning DAG validation, "Go?" gate, autonomy, and tool-approval boundary
# --------------------------------------------------------------------------- #


def test_plan_dag_summary_and_autonomy_approval_gate(repo: Path):
    _enable_chief(repo)
    _set_autonomy(repo, level="L1")

    chief = ChiefOfStaff(repo, providers=fake_registry())
    mission = chief.plan("Build an authentication API with frontend dashboard and database schema")

    assert mission.status == "planned"
    assert mission.plan_summary.startswith("I will use ")
    assert "Estimated cost" in mission.plan_summary
    assert mission.plan_summary.endswith("Go?")
    layers = validate_dag(mission.steps)
    assert len(layers) >= 2
    ids = {s.id for s in mission.steps}
    assert {"backend", "frontend", "database", "qa"}.issubset(ids)

    # Pipeline planning also builds a valid DAG.
    pipe_mission = chief.plan("Add rate limiter", pipeline="ship-feature")
    assert len(pipe_mission.steps) >= 3
    validate_dag(pipe_mission.steps)

    # Cycle detection.
    with pytest.raises(ChiefError, match="(?i)cycle"):
        validate_dag([
            MissionStep(id="a", title="a", role="general", agent="deepseek", instruction="x", depends_on=["b"]),
            MissionStep(id="b", title="b", role="general", agent="deepseek", instruction="y", depends_on=["a"]),
        ])

    # Default L1 autonomy requires explicit approval before running.
    paused = chief.execute(mission, auto_approve=False)
    assert paused.status == "awaiting_approval"
    loaded = load_mission(repo, mission.id)
    assert loaded.status == "awaiting_approval"

    # L2 auto-starts low-risk missions, but high-risk plans still wait for approval.
    _set_autonomy(repo, level="L2")
    low_m = Mission(
        id="m-low",
        goal="Write docs",
        success_criteria=["docs"],
        steps=[MissionStep(id="docs", title="docs", role="docs", agent="deepseek", instruction="doc", risk_level="low", read_only=True)],
        autonomy_level="L2",
    )
    high_m = Mission(
        id="m-high",
        goal="Deploy prod",
        success_criteria=["deploy"],
        steps=[MissionStep(id="deploy", title="deploy", role="devops", agent="gemini", instruction="deploy", risk_level="high")],
        autonomy_level="L2",
    )
    assert ChiefOfStaff.can_auto_start(low_m, chief.chief_config(), chief.autonomy_policy()) is True
    assert ChiefOfStaff.can_auto_start(high_m, chief.chief_config(), chief.autonomy_policy()) is False

    # Passing auto_yes=True NEVER weakens ApprovalGate / FORCED_REVIEW for shell commands!
    _set_autonomy(repo, level="L1")
    approval_prompts: list[str] = []

    def _gate(spec, args, risk, preview):
        approval_prompts.append(spec.name)
        from nexus.tools import ApprovalDecision

        return ApprovalDecision(False, reason="blocked by test gate")

    def _shell_handler(call, _prov):
        if _has_tool_result(call["messages"]):
            return finish_completion("finished after tool attempt")
        return tool_completion([("run_command", {"command": "echo hi"})])

    reg = _registry_with({}, default_handler=_shell_handler)
    c_strict = ChiefOfStaff(repo, providers=reg, tool_approval_callback=_gate, auto_yes=True)
    m_shell = c_strict.plan(
        "Check echo",
        steps=[{"id": "s1", "title": "s1", "role": "backend", "instruction": "Run echo command"}],
        critic_enabled=False,
    )
    res_m = c_strict.execute(m_shell, auto_approve=True)
    assert "run_command" in approval_prompts
    assert res_m.status == "escalated"
    assert "approval" in res_m.error.lower()


# --------------------------------------------------------------------------- #
# 3. Parallel branch execution & finish handoffs
# --------------------------------------------------------------------------- #


def test_parallel_branch_execution_and_finish_handoffs(repo: Path):
    _enable_chief(repo)
    barrier = threading.Barrier(2, timeout=5.0)
    seen_prompts: dict[str, str] = {}
    lock = threading.Lock()

    def _handler(call, _prov):
        text = _user_text(call["messages"])
        if _has_tool_result(call["messages"]):
            if "Your step: branch_a" in text:
                return finish_completion("branch A created alpha.txt")
            if "Your step: branch_b" in text:
                return finish_completion("branch B created beta.txt")
            return finish_completion("merged A and B into merged.txt")

        if "Your step: branch_a" in text:
            barrier.wait()
            with lock:
                seen_prompts["branch_a"] = text
            return tool_completion([("write_file", {"path": "alpha.txt", "content": "alpha\n"})])
        if "Your step: branch_b" in text:
            barrier.wait()
            with lock:
                seen_prompts["branch_b"] = text
            return tool_completion([("write_file", {"path": "beta.txt", "content": "beta\n"})])
        with lock:
            seen_prompts["merge"] = text
        return tool_completion([("write_file", {"path": "merged.txt", "content": "merged\n"})])

    reg = _registry_with({}, default_handler=_handler)
    chief = ChiefOfStaff(repo, providers=reg, tool_approval_callback=_allow_safe_workspace_tools, auto_yes=True)
    mission = chief.plan(
        "Parallel branches then merge",
        steps=[
            {"id": "branch_a", "title": "alpha", "role": "backend", "instruction": "Write alpha.txt", "phase": "build"},
            {"id": "branch_b", "title": "beta", "role": "frontend", "instruction": "Write beta.txt", "phase": "build"},
            {
                "id": "merge",
                "title": "merge",
                "role": "qa",
                "instruction": "Write merged.txt",
                "depends_on": ["branch_a", "branch_b"],
                "phase": "verify",
            },
        ],
        concurrency=2,
        critic_enabled=False,
    )
    done = chief.execute(mission, auto_approve=True)

    assert done.status == "done"
    assert (repo / "alpha.txt").read_text(encoding="utf-8") == "alpha\n"
    assert (repo / "beta.txt").read_text(encoding="utf-8") == "beta\n"
    assert (repo / "merged.txt").read_text(encoding="utf-8") == "merged\n"
    assert "branch A created alpha.txt" in seen_prompts["merge"]
    assert "branch B created beta.txt" in seen_prompts["merge"]
    assert "alpha.txt" in done.artifacts
    assert "beta.txt" in done.artifacts
    assert "merged.txt" in done.artifacts


# --------------------------------------------------------------------------- #
# 4. Bounded failure recovery: retry -> reassign -> escalate
# --------------------------------------------------------------------------- #


def test_failure_retry_then_succeeds(repo: Path):
    _enable_chief(repo, easy_agent="deepseek")
    attempts = {"n": 0}

    def _flaky(call, _prov):
        if _has_tool_result(call["messages"]):
            return finish_completion("recovered on attempt 2")
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ProviderError("transient 503")
        return tool_completion([("write_file", {"path": "ok.txt", "content": "ok\n"})])

    reg = _registry_with({}, default_handler=_flaky)
    chief = ChiefOfStaff(repo, providers=reg, tool_approval_callback=_allow_safe_workspace_tools, auto_yes=True)
    mission = chief.plan(
        "Retry recovery",
        steps=[{"id": "work", "title": "work", "role": "docs", "instruction": "Write ok.txt", "complexity": "easy"}],
        max_retries=2,
        critic_enabled=False,
    )
    res = chief.execute(mission, auto_approve=True)
    assert res.status == "done"
    assert res.steps[0].attempts == 2
    assert (repo / "ok.txt").exists()


def test_failure_retry_exhausted_reassigns_and_succeeds(repo: Path):
    _enable_chief(repo, easy_agent="deepseek", hard_agent="gemini")
    calls_by_agent = {"deepseek": 0, "gemini": 0}

    def _deepseek_fails(call, _prov):
        calls_by_agent["deepseek"] += 1
        raise ProviderError("deepseek down")

    def _gemini_succeeds(call, _prov):
        calls_by_agent["gemini"] += 1
        if _has_tool_result(call["messages"]):
            return finish_completion("gemini escalation succeeded")
        return tool_completion([("write_file", {"path": "escalated_ok.txt", "content": "fixed\n"})])

    reg = _registry_with({"deepseek": _deepseek_fails, "gemini": _gemini_succeeds}, default_handler=_gemini_succeeds)
    chief = ChiefOfStaff(repo, providers=reg, tool_approval_callback=_allow_safe_workspace_tools, auto_yes=True)
    mission = chief.plan(
        "Reassign recovery",
        steps=[{"id": "docs_step", "title": "docs", "role": "docs", "instruction": "Write escalated_ok.txt", "complexity": "easy"}],
        max_retries=1,
        critic_enabled=False,
    )
    res = chief.execute(mission, auto_approve=True)
    assert res.status == "done"
    step = res.steps[0]
    assert calls_by_agent["deepseek"] == 2  # 1 initial + 1 retry
    assert calls_by_agent["gemini"] >= 1
    assert step.agent_history == ["deepseek", "gemini"]
    assert step.agent == "gemini"
    assert (repo / "escalated_ok.txt").exists()


def test_failure_exhausts_retry_and_reassign_then_escalates(repo: Path):
    _enable_chief(repo, easy_agent="deepseek", hard_agent="gemini")
    _set_autonomy(repo, level="L1", max_depth=2)

    def _always_fail(call, _prov):
        raise ProviderError("permanent failure")

    reg = _registry_with({}, default_handler=_always_fail)
    chief = ChiefOfStaff(repo, providers=reg, auto_yes=True)
    mission = chief.plan(
        "Escalation bound",
        steps=[{"id": "s1", "title": "s1", "role": "docs", "instruction": "Write out.txt", "complexity": "easy"}],
        max_retries=1,
        critic_enabled=False,
    )
    res = chief.execute(mission, auto_approve=True)
    assert res.status == "escalated"
    assert res.steps[0].status == "escalated"
    assert res.steps[0].depth == 2
    assert res.steps[0].attempts == 6  # bounded retries across initial + 2 depth reassignments
    assert "permanent failure" in res.error


# --------------------------------------------------------------------------- #
# 5. Post-phase critic review & fix loop (bounded by max_depth)
# --------------------------------------------------------------------------- #


def test_post_phase_critic_fix_loop_and_max_depth_bound(repo: Path):
    _enable_chief(repo)
    _set_autonomy(repo, level="L1", max_depth=2)
    critic_calls = {"count": 0}

    def _worker_and_critic(call, _prov):
        text = _user_text(call["messages"])
        if "Review completed phase outputs" in text:
            critic_calls["count"] += 1
            if critic_calls["count"] == 1:
                return text_completion(json.dumps({
                    "passed": False,
                    "reason": "Missing validation check",
                    "fix_instruction": "Add validation to module.py",
                }))
            return text_completion(json.dumps({
                "passed": True,
                "reason": "Validation check verified",
                "fix_instruction": "",
            }))

        if _has_tool_result(call["messages"]):
            return finish_completion("step done")
        if "Address critic feedback" in text:
            return tool_completion([("write_file", {"path": "module.py", "content": "validated=True\n"})])
        if "Your step: qa" in text:
            return tool_completion([("read_file", {"path": "module.py"})])
        return tool_completion([("write_file", {"path": "module.py", "content": "validated=False\n"})])

    reg = _registry_with({}, default_handler=_worker_and_critic)
    chief = ChiefOfStaff(repo, providers=reg, tool_approval_callback=_allow_safe_workspace_tools, auto_yes=True)
    mission = chief.plan(
        "Critic repair loop",
        steps=[
            {"id": "backend", "title": "backend", "role": "backend", "instruction": "Write module.py", "phase": "implementation"},
            {"id": "qa", "title": "qa", "role": "qa", "instruction": "Read module.py", "depends_on": ["backend"], "phase": "verification", "read_only": True},
        ],
        critic_enabled=True,
    )
    res = chief.execute(mission, auto_approve=True)
    assert res.status == "done"
    assert (repo / "module.py").read_text(encoding="utf-8") == "validated=True\n"
    fix_steps = [s for s in res.steps if s.kind == "fix"]
    assert len(fix_steps) == 1
    assert fix_steps[0].status == "done"
    assert any(s.kind == "critic" and s.critic_passed is True for s in res.steps)

    # Now enforce max_depth=1 with repeated critic rejections -> bounded escalation.
    _set_autonomy(repo, level="L1", max_depth=1)

    def _always_reject_critic(call, _prov):
        text = _user_text(call["messages"])
        if "Review completed phase outputs" in text:
            return text_completion(json.dumps({"passed": False, "reason": "still flawed", "fix_instruction": "fix again"}))
        if _has_tool_result(call["messages"]):
            return finish_completion("wrote file")
        return tool_completion([("write_file", {"path": "bounded.txt", "content": "x\n"})])

    reg_bound = _registry_with({}, default_handler=_always_reject_critic)
    chief_bound = ChiefOfStaff(repo, providers=reg_bound, tool_approval_callback=_allow_safe_workspace_tools, auto_yes=True)
    m_bound = chief_bound.plan(
        "Bounded critic",
        steps=[{"id": "backend", "title": "backend", "role": "backend", "instruction": "Write bounded.txt", "phase": "impl"}],
        critic_enabled=True,
    )
    res_bound = chief_bound.execute(m_bound, auto_approve=True)
    assert res_bound.status == "escalated"
    assert sum(1 for s in res_bound.steps if s.kind == "fix") == 1


# --------------------------------------------------------------------------- #
# 6. Model tier routing, local_only, and hard cost-cap abort
# --------------------------------------------------------------------------- #


def test_model_tier_routing_local_only_and_cost_cap_abort(repo: Path):
    _enable_chief(repo)
    cfg = load_config(repo)
    chief_cfg = load_chief_config(repo)

    assert select_agent_for_step("docs", "easy", cfg, chief_cfg) == "gemini-flash"
    assert select_agent_for_step("backend", "hard", cfg, chief_cfg) == "gemini-big"
    assert select_agent_for_step("backend", "medium", cfg, chief_cfg) == "gemini"
    assert select_agent_for_step("docs", "easy", cfg, chief_cfg, local_only=True) == "ollama"

    # Hard USD cost cap abort between steps.
    def _expensive_step(call, _prov):
        if _has_tool_result(call["messages"]):
            return Completion(
                content=[ToolCall(id="t_fin", name="finish", input={"summary": "step 1 done"})],
                stop_reason="tool_use",
                model="gemini-2.5-pro",
                input_tokens=50_000,
                output_tokens=10_000,
            )
        return Completion(
            content=[ToolCall(id="t_wr", name="write_file", input={"path": "c1.txt", "content": "one\n"})],
            stop_reason="tool_use",
            model="gemini-2.5-pro",
            input_tokens=50_000,
            output_tokens=10_000,
        )

    reg = _registry_with({}, default_handler=_expensive_step)
    chief = ChiefOfStaff(repo, providers=reg, tool_approval_callback=_allow_safe_workspace_tools, auto_yes=True)
    mission = chief.plan(
        "Cost cap mission",
        steps=[
            {"id": "step1", "title": "s1", "role": "backend", "instruction": "Write c1.txt"},
            {"id": "step2", "title": "s2", "role": "backend", "instruction": "Write c2.txt", "depends_on": ["step1"]},
        ],
        cost_cap_usd=0.05,
        critic_enabled=False,
    )
    res = chief.execute(mission, auto_approve=True)
    assert res.status == "aborted_cost_cap"
    assert res.steps[0].status == "done"
    assert res.steps[1].status == "cancelled"
    assert not (repo / "c2.txt").exists()
    cost_val, _basis = compute_step_cost("gemini", 1_000_000, 0, cfg, chief_cfg=chief_cfg)
    assert cost_val == pytest.approx(1.25)


# --------------------------------------------------------------------------- #
# 7. Pause / Resume / Cancel + Secret redaction
# --------------------------------------------------------------------------- #


def test_pause_resume_cancel_and_secret_redaction(repo: Path):
    _enable_chief(repo)
    step1_entered = threading.Event()
    allow_step1_finish = threading.Event()

    def _pauseable_handler(call, _prov):
        text = _user_text(call["messages"])
        if _has_tool_result(call["messages"]):
            return finish_completion("step completed with secret sk-1234567890abcdefghijklmnop")
        if "Your step: step1" in text:
            step1_entered.set()
            allow_step1_finish.wait(timeout=3.0)
            return tool_completion([("write_file", {"path": "p1.txt", "content": "1\n"})])
        return tool_completion([("write_file", {"path": "p2.txt", "content": "2\n"})])

    reg = _registry_with({}, default_handler=_pauseable_handler)
    chief = ChiefOfStaff(repo, providers=reg, tool_approval_callback=_allow_safe_workspace_tools, auto_yes=True)

    # Goal containing a literal secret token is refused upfront by scan_text.
    from nexus.github_safety import GitHubSafetyError

    with pytest.raises(GitHubSafetyError):
        chief.plan("Leak sk-1234567890abcdefghijklmnop in goal")

    mission = chief.plan(
        "Pauseable mission",
        steps=[
            {"id": "step1", "title": "s1", "role": "backend", "instruction": "Write p1.txt"},
            {"id": "step2", "title": "s2", "role": "backend", "instruction": "Write p2.txt", "depends_on": ["step1"]},
        ],
        critic_enabled=False,
    )

    # Start in background, pause while step1 is executing, then allow step1 to finish cleanly.
    chief.start_background(mission.id, auto_approve=True)
    assert step1_entered.wait(timeout=3.0)
    paused = chief.pause(mission.id)
    assert paused.status == "paused"
    allow_step1_finish.set()

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        cur = load_mission(repo, mission.id)
        if cur.steps[0].status == "done" and cur.status == "paused":
            break
        threading.Event().wait(0.02)
    cur = load_mission(repo, mission.id)
    assert cur.steps[0].status == "done"
    assert cur.steps[1].status == "pending"
    assert not (repo / "p2.txt").exists()
    # Any secret surfaced in step finish summary is redacted on disk.
    assert "sk-1234567890" not in cur.steps[0].handoff_summary

    # Resume to completion.
    resumed = chief.resume(mission.id)
    assert resumed.status == "done"
    assert (repo / "p2.txt").exists()

    # Cancel a planned/awaiting mission cleanly.
    m2 = chief.plan(
        "Cancel me",
        steps=[{"id": "s1", "title": "s1", "role": "docs", "instruction": "Read README", "read_only": True}],
        critic_enabled=False,
    )
    cancelled = chief.cancel(m2.id)
    assert cancelled.status == "cancelled"
    assert cancelled.steps[0].status == "cancelled"


# --------------------------------------------------------------------------- #
# 8. CLI, HUD, Server HTTP, and Voice integration
# --------------------------------------------------------------------------- #


def test_cli_hud_server_and_voice_integration(repo: Path, capsys):
    # CLI --help works without enabling chief.yaml.
    from nexus.cli import main as cli_main

    with pytest.raises(SystemExit) as exc_info:
        cli_main(["chief", "--help"])
    assert exc_info.value.code == 0
    assert "Chief-of-Staff" in capsys.readouterr().out

    # CLI rejects mission creation while enabled: false, but status works.
    assert cli_main(["chief", "status", "-C", str(repo), "--json"]) == 0
    status_out = json.loads(capsys.readouterr().out)
    assert status_out["chief"]["enabled"] is False
    assert cli_main(["chief", "Build feature", "-C", str(repo), "--plan-only", "--json"]) == 2
    assert "disabled" in capsys.readouterr().out.lower()

    # Enable chief.yaml and test CLI plan-only + confirm GO.
    _enable_chief(repo)
    assert cli_main(["chief", "Write module docs", "-C", str(repo), "--plan-only", "--json"]) == 0
    planned_json = json.loads(capsys.readouterr().out)
    assert planned_json["status"] == "planned"
    assert "Go?" in planned_json["plan_summary"]

    # HUD.snapshot() includes 'chief'.
    from nexus.hud.core import HUD
    from nexus.server import ServerState, make_handler

    srv_state = ServerState(repo, quiet=True)
    hud = HUD(srv_state.workbench.studio)
    snap = hud.snapshot()
    assert "chief" in snap
    assert snap["chief"]["enabled"] is True
    assert len(snap["chief"]["missions"]) >= 1

    # FastAPI HUD routes (/api/chief/*) work with auth token.
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from nexus.hud.app import create_app

    app = create_app(srv_state.workbench.studio, token="chief-test-token-0123456789abcdef0123456789")
    client = TestClient(app, base_url="http://localhost")
    headers = {
        "Authorization": "Bearer chief-test-token-0123456789abcdef0123456789",
        "Origin": "http://localhost",
    }
    r_list = client.get("/api/chief/missions", headers=headers)
    assert r_list.status_code == 200
    assert len(r_list.json()["missions"]) >= 1

    r_create = client.post("/api/chief/missions", headers=headers, json={"goal": "Audit API security"})
    assert r_create.status_code == 200
    created_id = r_create.json()["id"]
    assert r_create.json()["status"] == "awaiting_approval"

    r_cancel = client.post(f"/api/chief/missions/{created_id}/cancel", headers=headers, json={})
    assert r_cancel.status_code == 200
    assert r_cancel.json()["status"] == "cancelled"

    # Server HTTP routes (nexus/server.py) handle GET/POST /api/chief/*.
    from types import SimpleNamespace

    state = ServerState(repo, token="srv-tok", quiet=True)
    handler_cls = make_handler(state)

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
            "X-Nexus-Token": "srv-tok",
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

    sc, srv_missions = _invoke("GET", "/api/chief/missions")
    assert sc == 200
    assert len(srv_missions["missions"]) >= 2
    sc2, srv_created = _invoke("POST", "/api/chief/missions", {"goal": "Build billing docs"})
    assert sc2 == 201
    sc3, srv_cancelled = _invoke("POST", f"/api/chief/missions/{srv_created['id']}/cancel", {})
    assert sc3 == 200
    assert srv_cancelled["status"] == "cancelled"

    # Voice intent + VoicePipeline Chief-of-Staff trigger & "Go?" confirmation.
    assert detect_mission_intent("chief build a REST API") == "build a REST API"
    assert detect_mission_intent("start a mission to refactor auth") == "refactor auth"
    assert detect_mission_control_intent("pause mission") == "pause"
    assert detect_mission_control_intent("mission status") == "status"

    from nexus.voice.audio_io import AudioClip, AudioOutputPlayer, float32_to_pcm16
    from nexus.voice.config import VADConfig, VoiceConfig, WakeConfig
    from nexus.voice.pipeline import VoicePipeline
    from nexus.voice.stt import TranscriptResult
    from nexus.voice.tts import TTSProvider

    class _StubSTT:
        def __init__(self, texts: list[str]) -> None:
            self.texts = list(texts)

        def transcribe_stream(self, _chunks, **_kw) -> TranscriptResult:
            return TranscriptResult(text=self.texts.pop(0), language="en")

    class _StubTTS(TTSProvider):
        name = "stub-tts"

        def __init__(self, *, workspace: Path) -> None:
            super().__init__(workspace=workspace)
            self.spoken: list[str] = []

        def _synthesize_clean(self, text: str, *, language: str, rate: float) -> AudioClip:
            self.spoken.append(text)
            return AudioClip(float32_to_pcm16([0.1] * 160), "pcm")

    class _NoopPlayer(AudioOutputPlayer):
        def play(self, clip: AudioClip, interrupt=None) -> bool:
            return False

    def _voice_ok(call, _prov):
        text = _user_text(call["messages"])
        if _has_tool_result(call["messages"]):
            return finish_completion("voice mission completed")
        if "Your step: research" in text:
            return tool_completion([("list_files", {})])
        return tool_completion([("write_file", {"path": "voice_out.txt", "content": "voice\n"})])

    reg_voice = _registry_with({}, default_handler=_voice_ok)

    speech_frame = float32_to_pcm16([0.5 if i % 2 == 0 else -0.5 for i in range(512)])
    silence_frame = b"\x00\x00" * 512
    utterance = [speech_frame] * 6 + [silence_frame] * 16

    stub_tts = _StubTTS(workspace=repo)
    pipe = VoicePipeline(
        VoiceConfig(
            enabled=True,
            wake=WakeConfig(enabled=False, engine="disabled"),
            vad=VADConfig(engine="energy", threshold=0.2, min_speech_ms=64, min_silence_ms=120),
        ),
        workspace=repo,
        stt=_StubSTT(["chief write documentation for module", "yes", "mission status"]),
        tts=stub_tts,
        player=_NoopPlayer(),
        text_handler=lambda _t: "chat fallback",
        chief_factory=lambda ws: ChiefOfStaff(
            ws,
            providers=reg_voice,
            tool_approval_callback=_allow_safe_workspace_tools,
            auto_yes=True,
        ),
    )

    turn1 = pipe.process_frames(utterance, skip_wake=True)
    assert turn1.approval_intent == "chief_plan"
    assert "Go?" in turn1.response_text
    assert any("Go?" in s for s in stub_tts.spoken)

    turn2 = pipe.process_frames(utterance, skip_wake=True)
    assert turn2.approval_intent == "chief_go"
    assert "Starting mission" in turn2.response_text

    # Wait briefly for background voice mission thread to complete.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not (repo / "voice_out.txt").exists():
        threading.Event().wait(0.02)
    assert (repo / "voice_out.txt").exists()

    turn3 = pipe.process_frames(utterance, skip_wake=True)
    assert turn3.approval_intent == "chief_status"
    assert "Mission " in turn3.response_text

    # Disabled chief.yaml blocks HTTP creation with 403 and voice mission trigger without chief_factory override.
    (repo / "chief.yaml").write_text("schema: 1\nenabled: false\n", encoding="utf-8")
    assert client.post("/api/chief/missions", headers=headers, json={"goal": "Should fail"}).status_code == 403
    sc_disabled, _ = _invoke("POST", "/api/chief/missions", {"goal": "Should fail"})
    assert sc_disabled == 403
    pipe_disabled = VoicePipeline(
        VoiceConfig(
            enabled=True,
            wake=WakeConfig(enabled=False, engine="disabled"),
            vad=VADConfig(engine="energy", threshold=0.2, min_speech_ms=64, min_silence_ms=120),
        ),
        workspace=repo,
        stt=_StubSTT(["chief build api"]),
        tts=_StubTTS(workspace=repo),
        player=_NoopPlayer(),
        text_handler=lambda _t: "chat fallback",
    )
    turn_disabled = pipe_disabled.process_frames(utterance, skip_wake=True)
    assert turn_disabled.error == "chief_disabled"
    assert "disabled" in turn_disabled.response_text.lower()

    # CLI cancel works on persisted awaiting_approval mission cleanly even while disabled.
    _enable_chief(repo)
    assert cli_main(["chief", "cancel", planned_json["id"], "-C", str(repo), "--json"]) == 0
    cancelled_cli = json.loads(capsys.readouterr().out)
    assert cancelled_cli["status"] == "cancelled"

    # Server approve without confirm='GO' rejects with 409.
    sc_unconfirmed, _ = _invoke("POST", "/api/chief/missions", {"goal": "Write guide"})
    assert sc_unconfirmed == 201
    missions_now = _invoke("GET", "/api/chief/missions")[1]["missions"]
    pending_id = next(m["id"] for m in missions_now if m["status"] == "awaiting_approval")
    sc_bad_approve, _ = _invoke("POST", f"/api/chief/missions/{pending_id}/approve", {"confirm": "NOPE"})
    assert sc_bad_approve == 409
