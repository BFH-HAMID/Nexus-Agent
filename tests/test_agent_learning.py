"""Agent wiring for skills, global memory and session history."""

from __future__ import annotations

import json

from nexus import skills as skilllib
from nexus.agent import NexusAgent
from nexus.config import load_config

from .conftest import FakeProvider, emit_to, fake_registry, finish_completion, tool_completion

BODY = (
    "# Paginate An Endpoint Skill\n\n## When to Use\nA list endpoint returns everything.\n\n"
    "## Procedure\n1. add limit/offset\n2. clamp the page size\n\n## Verification\n1. curl twice\n"
)


def make_agent(repo, script, *, settings=None, **kwargs):
    cfg = load_config(repo)
    if settings is not None:
        for key, value in settings.items():
            setattr(cfg.settings, key, value)
    provider = FakeProvider("gemini", script)
    providers = fake_registry(provider, config=cfg)
    agent = NexusAgent(
        cfg,
        workspace=repo,
        providers=providers,
        emit=emit_to([]),
        auto_yes=True,
        **kwargs,
    )
    agent.fake = provider  # type: ignore[attr-defined]
    return agent


def test_skill_index_lands_in_the_system_prompt(repo):
    skilllib.create_skill(
        name="paginate-endpoint",
        description="Add cursor pagination to a list endpoint.",
        body=BODY,
        tags=["pagination", "api"],
    )
    agent = make_agent(repo, [finish_completion("done")])
    agent.run("add cursor pagination to the users endpoint", role="backend")
    system = agent.fake.calls[0]["system"]  # type: ignore[attr-defined]
    assert "# Skills (procedural memory)" in system
    assert "paginate-endpoint" in system
    assert "skill_view" in system


def test_skills_can_be_disabled(repo):
    skilllib.create_skill(name="hidden-skill", description="Should not appear.", body=BODY)
    agent = make_agent(repo, [finish_completion("done")], settings={"skills_enabled": False})
    agent.run("add cursor pagination to the users endpoint", role="backend")
    assert "# Skills (procedural memory)" not in agent.fake.calls[0]["system"]  # type: ignore[attr-defined]


def test_global_memory_is_injected(repo):
    agent = make_agent(repo, [finish_completion("done")])
    agent.memory_store.add("this repo deploys with fly.io", "memory")
    agent.memory_store.add("answers in Bangla first", "user")
    agent.run("add an endpoint", role="backend")
    system = agent.fake.calls[0]["system"]  # type: ignore[attr-defined]
    assert "this repo deploys with fly.io" in system
    assert "answers in Bangla first" in system
    assert "# User profile" in system


def test_pinned_skill_is_inlined_and_then_cleared(repo):
    skilllib.create_skill(name="paginate-endpoint", description="Cursor pagination.", body=BODY)
    agent = make_agent(repo, [finish_completion("done"), finish_completion("done")])
    agent.inject_skill("paginate-endpoint")
    agent.run("do the thing", role="backend")
    first = agent.fake.calls[0]["system"]  # type: ignore[attr-defined]
    assert "# Active skill: paginate-endpoint" in first
    assert "add limit/offset" in first
    agent.run("do the thing", role="backend")
    assert "# Active skill: paginate-endpoint" not in agent.fake.calls[1]["system"]  # type: ignore[attr-defined]


def test_inject_unknown_skill_raises(repo):
    agent = make_agent(repo, [finish_completion("done")])
    try:
        agent.inject_skill("nope")
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("KeyError expected")


def test_session_is_recorded_and_recallable(repo):
    agent = make_agent(repo, [finish_completion("pagination done")])
    result = agent.run("add cursor pagination to the users endpoint", role="backend")
    assert result.session_id
    assert agent.sessions.stats()["sessions"] >= 1
    hits = agent.sessions.search("pagination")
    assert hits and hits[0]["id"] == result.session_id


def test_learning_runs_after_a_non_trivial_success(repo):
    agent = make_agent(repo, [
        tool_completion([("read_file", {"path": "app/main.py"})]),
        tool_completion([("grep", {"pattern": "def add"})]),
        tool_completion([("read_file", {"path": "app/api.py"})]),
        tool_completion([("read_file", {"path": "README.md"})]),
        finish_completion("explained"),
        # the learning review
        json.dumps({
            "skill": {"name": "explain-this-repo", "description": "Explain a repo in three steps.",
                      "body": BODY, "tags": ["onboarding"]},
            "memory": ["app/ holds the FastAPI service"], "profile": [],
        }),
    ], settings={"learning_agent": "gemini", "learning": True})
    agent.learning.should_review = lambda _result: True  # type: ignore[assignment]
    result = agent.run("explain this repo", role="backend")
    assert result.learned.get("skill") == "explain-this-repo"
    assert skilllib.get("explain-this-repo", repo) is not None
    assert agent.memory_store.entries("memory") == ["app/ holds the FastAPI service"]


def test_learning_skips_trivial_runs(repo):
    agent = make_agent(repo, [finish_completion("done")])
    result = agent.run("say hi", role="backend")
    assert result.learned == {}


def test_learning_failure_never_breaks_the_run(repo):
    agent = make_agent(repo, [finish_completion("done")])
    agent.learning.should_review = lambda _result: True  # type: ignore[assignment]

    def boom(*_a, **_k):
        raise RuntimeError("reviewer down")

    agent.learning.review = boom  # type: ignore[assignment]
    result = agent.run("say hi", role="backend")
    assert result.ok
    assert result.learned == {}


def test_skill_tools_are_registered(repo):
    agent = make_agent(repo, [finish_completion("done")])
    ctx, registry, specs = agent.tools_for(agent.active_role)
    names = {spec.name for spec in specs}
    assert {"skill_view", "skill_manage", "memory", "recall"} <= names

    text, error = registry.call("skill_manage", {
        "action": "create",
        "name": "from-the-agent",
        "description": "A skill the agent wrote itself.",
        "body": BODY,
        "tags": ["self"],
    })
    assert not error, text
    assert skilllib.get("from-the-agent", repo) is not None

    text, error = registry.call("skill_view", {"name": "from-the-agent"})
    assert not error and "add limit/offset" in text

    text, error = registry.call("skill_view", {})
    assert not error and "from-the-agent" in text


def test_memory_and_recall_tools(repo):
    agent = make_agent(repo, [finish_completion("done")])
    ctx, registry, _specs = agent.tools_for(agent.active_role)

    text, error = registry.call("memory", {"action": "add", "text": "use uv, not pip"})
    assert not error, text
    assert agent.memory_store.entries("memory") == ["use uv, not pip"]

    text, error = registry.call("memory", {"action": "list", "section": "memory"})
    assert not error and "uv" in text

    errors = 0
    for index in range(12):
        _text, error = registry.call("memory", {"action": "add", "text": f"entry {index} " + "y" * 400})
        errors += int(error)
    assert errors  # over the cap -> a tool error, not a crash

    from nexus.types import Message, TextBlock

    class Result:
        pass

    result = Result()
    result.text = "run the tests with pytest"
    result.role = "qa"
    result.agent = "fake"
    result.turns = 1
    result.tool_calls = 1
    result.summary = "ran the suite"
    result.ok = True
    result.duration_s = 0.1
    result.messages = [Message(role="user", content=[TextBlock(text="run the tests")])]
    agent.sessions.record(result, "run the tests")

    text, error = registry.call("recall", {"query": "pytest"})
    assert not error and "qa" in text


def test_read_only_roles_keep_skill_view_but_not_manage(repo):
    agent = make_agent(repo, [finish_completion("done")])
    _ctx, registry, specs = agent.tools_for(agent.config.role("qa"), read_only=True)
    names = {spec.name for spec in specs}
    assert "skill_view" in names
    assert "skill_manage" not in names
    assert "memory" not in names
