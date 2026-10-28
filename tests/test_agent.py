"""The agent loop, pipelines, plan mode and context management."""

from __future__ import annotations

import pytest

from nexus.agent import NexusAgent, render_system_prompt
from nexus.config import load_config
from nexus.types import Message, ProviderError, TextBlock, ToolCall, ToolResultBlock

from .conftest import FakeProvider, emit_to, fake_registry, finish_completion, text_completion, tool_completion


def make_agent(repo, script, *, gate_mode="auto", config=None, extra_providers=(), **kwargs):
    cfg = config or load_config(repo)
    provider = FakeProvider("gemini", script)
    providers = fake_registry(provider, *extra_providers, config=cfg)
    bucket: list[dict] = []
    agent = NexusAgent(
        cfg,
        workspace=repo,
        providers=providers,
        emit=emit_to(bucket),
        auto_yes=(gate_mode == "auto"),
        **kwargs,
    )
    if gate_mode == "plan":
        agent.plan_mode = True
        agent.gate.mode = "plan"
    agent.events = bucket  # type: ignore[attr-defined]
    agent.fake = provider  # type: ignore[attr-defined]
    return agent


# --------------------------------------------------------------------------- #
# routing -> persona injection
# --------------------------------------------------------------------------- #


def test_persona_and_agent_are_injected_per_role(repo):
    agent = make_agent(repo, [finish_completion("done")])
    result = agent.run("write a react component with tailwind")
    assert result.role == "frontend"
    assert result.agent == "gemini"
    system = agent.fake.calls[0]["system"]
    assert "senior Frontend Engineer" in system
    assert "# Active role: frontend" in system
    assert "React/Next.js" in system
    assert str(repo) in system


def test_uncensored_role_switches_agent(repo):
    uncensored = FakeProvider("openrouter-uncensored", [finish_completion("scan planned")], tool_mode="text")
    agent = make_agent(repo, [finish_completion("unused")], extra_providers=[uncensored])
    result = agent.run("run nmap against my own lab and escalate privileges")
    assert result.role == "pentesting"
    assert result.agent == "openrouter-uncensored"
    assert uncensored.calls, "the uncensored provider was never used"
    system = uncensored.calls[0]["system"]
    assert "offensive-security specialist" in system
    assert "authorized penetration testing" in system
    events = [e for e in agent.events if e["type"] == "agent"]  # type: ignore[attr-defined]
    assert any(e.get("agent") == "openrouter-uncensored" and e.get("role") == "pentesting" for e in events)
    assert any(e.get("tool_mode") == "text" for e in events)  # no native tool template on dolphin


def test_forced_role_skips_routing(repo):
    agent = make_agent(repo, [finish_completion("ok")])
    result = agent.run("write react components", role="qa")
    assert result.role == "qa"
    assert result.method == "manual"


def test_sticky_role_is_used_when_routing_is_unsure(repo):
    agent = make_agent(repo, [finish_completion("ok")])
    agent.set_role("database")
    agent.auto_route = False
    result = agent.run("do the thing")
    assert result.role == "database"


def test_general_fallback_for_vague_tasks(repo):
    agent = make_agent(repo, [finish_completion("ok")])
    result = agent.run("please handle the situation accordingly")
    assert result.role == "general"


# --------------------------------------------------------------------------- #
# the loop itself
# --------------------------------------------------------------------------- #


def test_tool_loop_runs_tools_until_finish(repo):
    script = [
        tool_completion([("read_file", {"path": "app/main.py"})], text="reading first"),
        tool_completion([("write_file", {"path": "app/main.py", "content": "def add(a, b):\n    return a + b  # typed\n"})]),
        finish_completion("updated add()", ["run the tests"]),
    ]
    agent = make_agent(repo, script)
    result = agent.run("improve app/main.py")
    assert result.ok
    assert result.tool_calls == 3
    assert result.turns == 3
    assert result.stopped_reason == "finish"
    assert result.summary == "updated add()"
    assert result.followups == ["run the tests"]
    assert result.changed_files == ["app/main.py"]
    assert (repo / "app" / "main.py").read_text().endswith("# typed\n")


def test_loop_stops_when_the_model_just_talks(repo):
    agent = make_agent(repo, [text_completion("Here is the answer, no tools needed.")])
    result = agent.run("what does this repo do?")
    assert result.stopped_reason == "end_turn"
    assert result.tool_calls == 0
    assert "Here is the answer" in result.text


def test_max_turns_is_respected(repo):
    script = [tool_completion([("run_command", {"command": "echo hi"})])] * 10
    agent = make_agent(repo, script)
    result = agent.run("loop forever", max_turns=3)
    assert result.turns == 3
    assert "max_turns" in result.stopped_reason


def test_unknown_tool_does_not_kill_the_loop(repo):
    agent = make_agent(repo, [
        tool_completion([("teleport", {"where": "mars"})]),
        finish_completion("recovered"),
    ])
    result = agent.run("do something")
    assert result.ok
    assert result.summary == "recovered"
    error_events = [e for e in agent.events if e["type"] == "tool_result" and e.get("is_error")]  # type: ignore[attr-defined]
    assert error_events


def test_repeated_tool_errors_get_a_nudge_and_then_stop(repo):
    script = [tool_completion([("read_file", {"path": "missing.py"})])] * 12
    agent = make_agent(repo, script)
    result = agent.run("read the missing file over and over")
    assert "too many tool errors" in result.stopped_reason
    system_nudges = [
        block for message in result.messages if isinstance(message.content, list)
        for block in message.content if isinstance(block, TextBlock) and "three tool errors" in block.text
    ]
    assert system_nudges, "no nudge was injected after repeated failures"


def test_provider_error_is_reported_not_raised(repo):
    agent = make_agent(repo, [ProviderError("anthropic 529: overloaded", retryable=False)])
    result = agent.run("do something")
    assert not result.ok
    assert "provider error" in result.stopped_reason
    assert any(e["type"] == "error" for e in agent.events)  # type: ignore[attr-defined]


def test_fallback_provider_completes_the_run(repo):

    cfg = load_config(repo)
    cfg.agents["gemini"].fallback = "deepseek"
    claude = FakeProvider("gemini", [ProviderError("claude is down", retryable=True)], fallback="deepseek")
    hermes = FakeProvider("deepseek", [
        tool_completion([("write_file", {"path": "README.md", "content": "# Updated README\n"})]),
        finish_completion("hermes finished it"),
    ])
    agent = NexusAgent(cfg, workspace=repo, providers=fake_registry(claude, hermes, config=cfg),
                       auto_yes=True, emit=lambda e: None)
    result = agent.run("write a README section")
    assert result.ok
    assert result.agent == "deepseek"
    assert result.summary == "hermes finished it"


def test_read_only_role_restricts_the_tool_surface(repo):
    agent = make_agent(repo, [finish_completion("plan ready")])
    ctx, registry, specs = agent.tools_for(agent.config.role("backend"), read_only=True)
    names = {s.name for s in specs}
    assert "write_file" not in names and "run_command" not in names and "edit_file" not in names
    assert {"read_file", "list_files", "grep"} <= names


def test_plan_mode_denies_writes(repo):
    agent = make_agent(repo, [
        tool_completion([("write_file", {"path": "x.py", "content": "x"})]),
        finish_completion("planned instead"),
    ], gate_mode="plan")
    agent.run("change the code", read_only=True)
    assert not (repo / "x.py").exists()
    denied = [e for e in agent.events if e["type"] == "tool_result" and e.get("is_error")]  # type: ignore[attr-defined]
    assert denied


def test_role_tool_whitelist_is_honoured(repo):
    cfg = load_config(repo)
    cfg.roles.get("docs").tools = ["read_file", "list_files", "finish"]
    cfg.roles.get("docs").agent = "gemini"
    agent = make_agent(repo, [finish_completion("ok")], config=cfg)
    agent.run("write docs", role="docs")
    tools = agent.fake.calls[0]["tools"]
    assert set(tools) == {"read_file", "list_files", "finish"}
    # a tool outside the whitelist is not even offered
    ctx, registry, specs = agent.tools_for(cfg.roles.get("docs"))
    assert not registry.has("write_file")


# --------------------------------------------------------------------------- #
# memory (Phase 7)
# --------------------------------------------------------------------------- #


def test_role_memory_is_injected_into_the_system_prompt(repo):
    (repo / ".nexus" / "memory").mkdir(parents=True)
    (repo / ".nexus" / "memory" / "backend.md").write_text(
        "# backend memory\n\n- [2026-01-01] We use UUIDv7 primary keys.\n", encoding="utf-8"
    )
    agent = make_agent(repo, [finish_completion("ok")])
    agent.run("add an api endpoint", role="backend")
    system = agent.fake.calls[0]["system"]
    assert "UUIDv7" in system
    assert "Your role memory (backend)" in system


def test_remember_tool_persists_for_the_next_session(repo):
    agent = make_agent(repo, [
        tool_completion([("remember", {"text": "this repo uses uv, not pip"})]),
        finish_completion("noted"),
    ])
    agent.run("note the package manager", role="backend")
    assert (repo / ".nexus" / "memory" / "backend.md").exists()

    agent2 = make_agent(repo, [finish_completion("ok")])
    agent2.run("add a dependency", role="backend")
    assert "uses uv" in agent2.fake.calls[0]["system"]


def test_memory_can_be_disabled(repo):
    cfg = load_config(repo)
    cfg.settings.memory_enabled = False
    agent = make_agent(repo, [finish_completion("ok")], config=cfg)
    agent.run("add an api endpoint", role="backend")
    assert "Your role memory" not in agent.fake.calls[0]["system"]


# --------------------------------------------------------------------------- #
# ask_agent (Phase 6 primitive)
# --------------------------------------------------------------------------- #


def test_ask_agent_consults_another_specialist(repo):
    from nexus.config import AgentConfig

    cfg = load_config(repo)
    cfg.agents["qa-bot"] = AgentConfig(name="qa-bot", provider="anthropic", model="qa-model", api_key="t")
    cfg.roles.get("qa").agent = "qa-bot"
    qa = FakeProvider("qa-bot", [text_completion("QA says: cover the 404 path and the empty list.")])
    agent = make_agent(repo, [
        tool_completion([("ask_agent", {"role": "qa", "question": "what should I test for /users?"})]),
        finish_completion("tests planned"),
    ], extra_providers=[qa], config=cfg)
    result = agent.run("plan the /users endpoint", role="backend")
    assert result.ok
    assert qa.calls, "the consulted agent was never called"
    assert "no tools" in qa.calls[0]["system"].lower()
    tool_result = [
        block for message in result.messages if isinstance(message.content, list)
        for block in message.content
        if getattr(block, "type", "") == "tool_result" and "QA says" in getattr(block, "content", "")
    ]
    assert tool_result, "the consulted answer never came back to the caller"


def test_ask_agent_cannot_recurse(repo):
    hermes = FakeProvider("deepseek", [text_completion("answer")])
    agent = make_agent(repo, [finish_completion("ok")], extra_providers=[hermes])
    ctx = agent.build_context(agent.active_role, depth=1)
    from nexus.tools import build_tools

    registry = build_tools(ctx, role="backend", include_ask_agent=False)
    assert not registry.has("ask_agent"), "nested ask_agent must not be offered"
    # and the agent itself never offers it to a consulted specialist
    _ctx, _reg, specs = agent.tools_for(agent.config.role("backend"), depth=1)
    assert "ask_agent" not in {s.name for s in specs}


# --------------------------------------------------------------------------- #
# context management
# --------------------------------------------------------------------------- #


def test_compaction_trims_old_tool_results(repo):
    agent = make_agent(repo, [])
    agent.config.settings.context_char_budget = 2000
    messages = [
        Message(role="assistant", content=[ToolCall(id="c1", name="read_file", input={"path": "a"})]),
        Message(role="user", content=[ToolResultBlock("c1", "x" * 5000)]),
        Message(role="assistant", content=[TextBlock(text="recent")]),
    ]
    agent._maybe_compact(messages)
    total = sum(m.char_len() for m in messages)
    assert total < 3000
    assert "compacted" in str(messages[1].content[0].content)


def test_compaction_is_a_noop_under_budget(repo):
    agent = make_agent(repo, [])
    messages = [Message(role="user", content="short")]
    agent._maybe_compact(messages)
    assert messages[0].content == "short"


# --------------------------------------------------------------------------- #
# chat sessions
# --------------------------------------------------------------------------- #


def test_chat_turns_keep_history(repo):
    agent = make_agent(repo, [text_completion("first answer"), text_completion("second answer")])
    first = agent.chat_turn("first question please")
    assert first.text == "first answer"
    assert len(agent.history) == 2
    second = agent.chat_turn("and now the second one")
    assert second.text == "second answer"
    assert len(agent.history) == 4
    sent = agent.fake.calls[1]["messages"]
    assert any(getattr(m, "content", None) == "first question please" for m in sent)


def test_reset_clears_everything(repo):
    agent = make_agent(repo, [text_completion("x")])
    agent.chat_turn("please continue")
    agent.set_role("qa")
    agent.reset()
    assert agent.history == [] and agent.sticky_role is None
    assert agent.session_usage.calls == 0


def test_session_usage_accumulates(repo):
    agent = make_agent(repo, [text_completion("a"), text_completion("b")])
    agent.chat_turn("one")
    agent.chat_turn("two")
    assert agent.session_usage.calls == 2
    assert agent.session_usage.input_tokens == 20


# --------------------------------------------------------------------------- #
# system prompt rendering
# --------------------------------------------------------------------------- #


def test_render_system_prompt_sections(repo, config):
    role = config.role("pentesting")
    prompt = render_system_prompt(
        role=role,
        agent_label="openrouter-uncensored / dolphin-mistral-24b-venice-edition",
        workspace=repo,
        memory_text="- lab host is 10.0.0.5",
        tools_hint="- read_file: read a file",
        plan_mode=True,
        extra="Extra rule.",
    )
    assert "You are Nexus" in prompt
    assert "# Active role: pentesting" in prompt
    assert "offensive-security specialist" in prompt
    assert "dolphin-mistral-24b-venice-edition" in prompt
    assert "lab host is 10.0.0.5" in prompt
    assert "PLAN MODE" in prompt
    assert "Extra rule." in prompt


def test_events_are_emitted_in_order(repo):
    agent = make_agent(repo, [
        tool_completion([("read_file", {"path": "README.md"})]),
        finish_completion("done"),
    ])
    agent.run("add a rest api endpoint")   # keyword route -> no classifier events
    kinds = [e["type"] for e in agent.events]  # type: ignore[attr-defined]
    assert kinds[0] in {"role", "status"}
    assert "role" in kinds and "tool_call" in kinds and "tool_result" in kinds
    assert kinds.index("tool_call") < kinds.index("tool_result")


# --------------------------------------------------------------------------- #
# pipelines (Phase 6)
# --------------------------------------------------------------------------- #


def test_ship_feature_pipeline_hands_off_between_roles(repo):
    cfg = load_config(repo)
    cfg.pipelines["ship-feature"].steps = cfg.pipelines["ship-feature"].steps[:3]  # build -> test -> review
    backend = FakeProvider("gemini", [
        # step 1: backend (gemini) writes the code, then finishes
        tool_completion([("write_file", {"path": "app/api.py", "content": "# new api\n"})]),
        finish_completion("implemented /users rate limiting"),
        # step 3: security-audit reads the actual handoff file before review.
        tool_completion([("read_file", {"path": "app/api.py"})]),
        finish_completion("security review clean"),
    ])
    hermes = FakeProvider("deepseek", [
        tool_completion([("read_file", {"path": "app/api.py"})]),
        finish_completion("reviewed test targets; tests not executed"),
    ])
    cfg.roles.get("qa").agent = "deepseek"
    agent = NexusAgent(cfg, workspace=repo, providers=fake_registry(backend, hermes, config=cfg),
                       auto_yes=True, emit=lambda e: None)
    results = agent.run_pipeline("ship-feature", "add rate limiting to the public API")
    assert [r.role for r in results] == ["backend", "qa", "security-audit"]
    assert all(r.ok for r in results)
    # the QA step received the backend handoff note
    qa_prompt = hermes.calls[0]["messages"][-1].content
    assert "implemented /users rate limiting" in qa_prompt
    assert "step 2/3" in qa_prompt
    # and the security step received the QA note
    sec_prompt = backend.calls[-2]["messages"][-1].content
    assert "reviewed test targets; tests not executed" in sec_prompt


def test_pipeline_stops_when_a_step_fails(repo):
    cfg = load_config(repo)
    from nexus.config import Pipeline

    cfg.pipelines["mini"] = Pipeline(name="mini", steps=[
        {"role": "backend", "instruction": "build"},
        {"role": "qa", "instruction": "test"},
    ])
    failing = FakeProvider("gemini", [ProviderError("down", retryable=False)])
    hermes = FakeProvider("deepseek", [finish_completion("should not run")])
    agent = NexusAgent(cfg, workspace=repo, providers=fake_registry(failing, hermes, config=cfg),
                       auto_yes=True, emit=lambda e: None)
    results = agent.run_pipeline("mini", "task")
    assert len(results) == 1
    assert not results[0].ok
    assert hermes.calls == []


def test_unknown_pipeline_raises(repo):
    agent = make_agent(repo, [])
    with pytest.raises(KeyError):
        agent.run_pipeline("nope", "task")


def test_read_only_pipeline_step_cannot_write(repo):
    cfg = load_config(repo)
    from nexus.config import Pipeline

    cfg.pipelines["audit"] = Pipeline(name="audit", steps=[
        {"role": "security-audit", "instruction": "look, do not touch", "read_only": True},
    ])
    provider = FakeProvider("gemini", [
        tool_completion([("write_file", {"path": "evil.py", "content": "x"})]),
        finish_completion("reported findings"),
    ])
    agent = NexusAgent(cfg, workspace=repo, providers=fake_registry(provider, config=cfg), auto_yes=True,
                       emit=lambda e: None)
    results = agent.run_pipeline("audit", "review auth")
    assert results[0].ok
    assert not (repo / "evil.py").exists()


@pytest.mark.parametrize('reply', [
    'Here are the files created or changed: ```java\n// Implement later\n```',
    'All done, tests pass and pushed to GitHub.',
])
def test_coding_prose_without_execution_retries_then_blocks(repo, reply):
    agent = make_agent(repo, [text_completion(reply)] * 5)
    result = agent.run('Implement vector search and push to GitHub', role='general')
    assert not result.ok and result.execution['state'] == 'blocked'
    assert result.turns == 3 and result.tool_calls == 0
    assert result.changed_files == []
    assert 'no successful workspace tool' in result.stopped_reason
    assert not (repo / 'src').exists()
    assert '```tool' in agent.fake.calls[1]['messages'][-1].content
    assert 'wrong workspace' in agent.fake.calls[1]['messages'][-1].content


def test_coding_agent_recovers_from_prose_using_real_tools(repo):
    agent = make_agent(repo, [
        text_completion('I will change app/main.py.'),
        tool_completion([('read_file', {'path': 'app/main.py'})]),
        tool_completion([('write_file', {'path': 'app/main.py', 'content': '# actual edit\n'})]),
        finish_completion('Updated the file; tests not run'),
    ])
    result = agent.run('Fix app/main.py', role='general')
    assert result.ok and result.execution['state'] == 'unverified'
    assert result.execution['successful_workspace_tools'] == 2
    assert result.changed_files == ['app/main.py']
    assert (repo / 'app/main.py').read_text() == '# actual edit\n'


def test_finish_only_cannot_certify_implementation(repo):
    agent = make_agent(repo, [finish_completion('done')] * 5)
    result = agent.run('Implement vector search', role='general')
    assert not result.ok and result.turns == 3
    assert result.execution['state'] == 'blocked'
    assert any(e['type'] == 'tool_result' and e.get('is_error') for e in agent.events)


def test_failed_and_denied_tools_do_not_count_as_execution(repo):
    agent = make_agent(repo, [
        tool_completion([('read_file', {'path': 'missing.java'})]),
        finish_completion('done'), finish_completion('done'), finish_completion('done'),
    ])
    result = agent.run('Implement missing.java', role='general')
    assert not result.ok and result.execution['successful_workspace_tools'] == 0
    assert result.execution['state'] == 'blocked'


def test_plan_and_advice_still_allow_plain_text(repo):
    for task, readonly in [('Explain how to implement vector search', False),
                           ('Implement vector search', True), ('Write a short example', False)]:
        agent = make_agent(repo, [text_completion('Here is the plan, no files changed.')])
        result = agent.run(task, role='general', read_only=readonly)
        assert result.ok and result.turns == 1


def test_failed_command_is_not_success_even_after_finish(repo):
    import shlex
    import sys
    command = shlex.join([sys.executable, '-c', 'raise SystemExit(3)'])
    agent = make_agent(repo, [tool_completion([('run_command', {'command': command})]), finish_completion('all passed')])
    result = agent.run('Run the tests', role='general')
    assert not result.ok and result.execution['state'] == 'blocked'
    assert any(e['type'] == 'tool_result' and e.get('is_error') for e in agent.events)


def test_failed_command_followed_by_success_can_recover(repo):
    import shlex
    import sys
    fail = shlex.join([sys.executable, '-c', 'raise SystemExit(3)'])
    success = shlex.join([sys.executable, '-c', 'print("real recovery")'])
    agent = make_agent(repo, [tool_completion([('run_command', {'command': fail})]),
        tool_completion([('run_command', {'command': success})]), finish_completion('recovered')])
    result = agent.run('Run the checks', role='general')
    assert result.ok and result.execution['successful_workspace_tools'] == 1


def test_max_turn_limit_cannot_report_success(repo):
    agent = make_agent(repo, [tool_completion([('read_file', {'path': 'README.md'})])] * 3)
    result = agent.run('Fix the bug', role='general', max_turns=2)
    assert not result.ok and 'max_turns' in result.stopped_reason


def test_short_limit_never_adds_extra_calls(repo):
    agent = make_agent(repo, [text_completion('Done')])
    result = agent.run('Implement vector search', role='general', max_turns=1)
    assert not result.ok and result.turns == 1 and len(agent.fake.calls) == 1


@pytest.mark.parametrize('task', ['egula fixed koro', 'code likhe push koro', 'বাগ ঠিক কর'])
def test_local_language_imperatives_do_not_accept_prose_as_execution(repo, task):
    agent = make_agent(repo, [text_completion('Done')] * 3)
    result = agent.run(task, role='general')
    assert not result.ok and result.execution['state'] == 'blocked'


def test_real_remote_tool_success_is_execution_activity(repo, monkeypatch):
    from nexus import tools
    monkeypatch.setattr(tools, 'tool_gh', lambda *a, **k: 'scripted successful GitHub transport, not a live mutation')
    agent = make_agent(repo, [tool_completion([('gh', {'args': ['pr', 'list']})]), finish_completion('listed actual PRs')])
    result = agent.run('Run gh pr list', role='general')
    assert result.ok and result.execution['successful_workspace_tools'] == 1
