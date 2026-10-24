"""Demo mode: the whole loop, offline, with zero API keys."""

from __future__ import annotations

from pathlib import Path

from nexus.agent import NexusAgent
from nexus.config import load_config
from nexus.demo import DEMO_NOTE_PATH, patch_agent_for_demo
from nexus.tools import ApprovalDecision


def demo_agent(repo: Path, *, auto_yes: bool = True, callback=None, plan: bool = False):
    agent = NexusAgent(load_config(repo), workspace=repo, emit=lambda _e: None,
                       auto_yes=auto_yes, approval_callback=callback)
    if plan:
        agent.plan_mode = True
        agent.gate.mode = "plan"
    patch_agent_for_demo(agent)
    return agent


def test_demo_run_routes_and_writes(repo):
    agent = demo_agent(repo)
    result = agent.run("add pagination to the users api")
    assert result.ok
    assert result.role == "backend"
    assert result.agent == "gemini"
    assert result.tool_calls >= 3
    assert (repo / DEMO_NOTE_PATH).is_file()
    assert "demo" in (repo / DEMO_NOTE_PATH).read_text().lower()


def test_demo_run_records_role_memory(repo):
    agent = demo_agent(repo)
    agent.run("write a react component")
    memory = repo / ".nexus" / "memory" / "frontend.md"
    assert memory.is_file()
    assert "demo run" in memory.read_text()


def test_demo_still_respects_the_approval_gate(repo):
    agent = demo_agent(repo, auto_yes=False,
                       callback=lambda spec, args, risk, preview: ApprovalDecision(False, reason="nope"))
    result = agent.run("add an api endpoint")
    assert result.ok                      # the run survives a denial
    assert not (repo / DEMO_NOTE_PATH).exists()


def test_demo_plan_mode_writes_nothing(repo):
    agent = demo_agent(repo, plan=True)
    result = agent.run("add an api endpoint", read_only=True)
    assert result.ok
    assert not (repo / DEMO_NOTE_PATH).exists()
    assert not (repo / ".nexus").exists()


def test_demo_pipeline_runs_every_step(repo):
    agent = demo_agent(repo)
    results = agent.run_pipeline("bug-hunt", "the /users endpoint returns 500")
    assert [r.role for r in results] == ["debugging", "backend", "qa"]
    assert all(r.ok for r in results)


def test_demo_ask_agent_needs_no_model(repo):
    agent = demo_agent(repo)
    result = agent.run("add an api endpoint")
    assert result.ok
    assert result.tool_calls >= 4


def test_demo_chat_turn_keeps_history(repo):
    agent = demo_agent(repo)
    agent.chat_turn("add an api endpoint")
    assert len(agent.history) == 2
    agent.chat_turn("now document it")
    assert len(agent.history) == 4


def test_demo_cli_end_to_end(repo, capsys):
    import nexus.cli as cli

    args = cli.normalize_args(__import__("argparse").Namespace(
        workspace=str(repo), role=None, yes=True, plan=False, allow_outside=False,
        allow_dangerous=False, json=True, providers_factory=None, emit=None, max_turns=0,
        read_only=False, no_route=False, message="", keep_open=False, demo=True,
        list=False, search=None, show=None, category=None, force=False, name=None,
        task=["add a rate limiter to the api"],
    ))
    code = cli.cmd_task(args)
    assert code == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["role"] == "backend"
    assert (repo / DEMO_NOTE_PATH).is_file()
