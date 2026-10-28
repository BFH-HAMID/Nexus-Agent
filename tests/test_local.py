"""Local Ollama / Hermes lanes: optional agents, no keys, clean fallbacks.

These tests pin the workflow the README promises:

* `hermes`, `hermes-big`, `ollama`, `ollama-uncensored` are ordinary agents that
  need **no API key**;
* they are `optional: true`, so an offline machine is a warning, not a failure;
* when the local server is down the call falls through to the hosted provider;
* `NEXUS_PREFER_LOCAL=1` points the whole roster (and the router) at them.
"""

from __future__ import annotations

import json

import pytest

from nexus.config import apply_prefer_local, load_config
from nexus.types import Message, ProviderError

LOCAL = ("hermes", "hermes-big", "ollama", "ollama-uncensored")
HOSTED_FREE = ("hermes-free", "openrouter-fast")


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", LOCAL)
def test_local_agents_need_no_api_key(repo, name):
    config = load_config(repo)
    cfg = config.agents[name]
    assert cfg.optional is True
    assert cfg.key_family is None                      # nothing to prompt for / store
    assert cfg.resolved_api_key == "ollama"            # the shim's placeholder, inline
    assert "localhost:11434" in cfg.effective_base_url
    assert cfg.fallback in config.agents


def test_local_agent_models_and_tool_modes(repo):
    config = load_config(repo)
    assert config.agents["hermes"].model == "hermes3:8b"
    assert config.agents["hermes"].tool_mode == "native"
    assert config.agents["hermes-big"].model == "hermes3:70b"
    assert config.agents["ollama"].model == "llama3.1:8b"
    # dolphin-mixtral has no tool template -> prompt-level JSON contract
    assert config.agents["ollama-uncensored"].tool_mode == "text"


def test_local_agents_fall_back_to_hosted_agents(repo):
    config = load_config(repo)
    assert config.agents["hermes"].fallback == "hermes-free"
    assert config.agents["hermes-big"].fallback == "hermes-free"
    assert config.agents["ollama-uncensored"].fallback == "openrouter-uncensored"
    assert config.agents["ollama"].fallback == "gemini"


def test_default_roster_stays_hosted(repo):
    config = load_config(repo)
    assert config.settings.prefer_local is False
    assert config.local_agents == []
    assert config.role("backend").agent == "gemini"
    assert config.role("pentesting").agent == "openrouter-uncensored"
    assert config.role("qa").agent == "deepseek"


def test_prefer_local_env_points_roles_and_router_at_local(repo, monkeypatch):
    monkeypatch.setenv("NEXUS_PREFER_LOCAL", "1")
    config = load_config(repo)

    assert config.settings.prefer_local is True
    assert set(config.local_agents) <= set(LOCAL)
    assert config.role("backend").agent == "hermes"          # code tiers -> hermes
    assert config.role("architecture").agent == "hermes-big"  # big tier
    assert config.role("pentesting").agent == "ollama-uncensored"
    assert config.role("qa").agent == "hermes"
    assert config.settings.router_agent == "ollama"          # classifier stays local too
    # every agent a role points at really exists
    assert {r.agent for r in config.roles.roles.values()} <= set(config.agents)


def test_prefer_local_falls_back_to_hosted_when_roles_are_already_local(repo, monkeypatch):
    monkeypatch.setenv("NEXUS_PREFER_LOCAL", "1")
    config = load_config(repo)
    cfg = config.agent_for_role(config.role("backend"))
    assert cfg.name == "hermes"
    assert config.agents[cfg.fallback].key_family == "openrouter"   # hosted free Hermes


def test_apply_prefer_local_is_a_noop_without_local_agents(repo):
    config = load_config(repo)
    config.agents = {name: cfg for name, cfg in config.agents.items() if name not in LOCAL}
    used = apply_prefer_local(config)
    assert used == []
    assert config.role("backend").agent == "gemini"


# --------------------------------------------------------------------------- #
# runtime behaviour
# --------------------------------------------------------------------------- #


class _LocalTransport:
    """Stands in for `ollama serve` speaking the OpenAI shim."""

    def __init__(self, status: int = 200):
        self.status = status

    def get(self, url, headers=None, timeout=None):
        from types import SimpleNamespace

        return SimpleNamespace(status_code=self.status, text="", json=lambda: {"data": [{"id": "hermes3:8b"}]})

    def post(self, url, json=None, headers=None, timeout=None):
        from types import SimpleNamespace

        payload = {
            "model": json.get("model"),
            "choices": [{"message": {"role": "assistant", "content": "local answer"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        }
        return SimpleNamespace(status_code=self.status, text="", json=lambda: payload)


def test_remote_optional_box_may_be_keyless(repo):
    """A self-hosted vLLM on the LAN is not loopback: `optional` allows it."""
    from nexus.providers import OpenAICompatProvider

    config = load_config(repo)
    cfg = config.agents["hermes"]
    cfg.api_key = None
    cfg.base_url = "http://gpu-box.internal:8000/v1"
    provider = OpenAICompatProvider(cfg, transport=_LocalTransport(500))
    provider._require_key()                    # no raise: optional endpoint
    ok, detail = provider.health()
    assert ok is False and "no API key" not in detail


def test_local_provider_works_without_any_key(repo, isolated_store=None):
    from nexus.providers import OpenAICompatProvider

    config = load_config(repo)
    cfg = config.agents["hermes"]
    cfg.api_key = None                       # even with no inline key...
    cfg.base_url = "http://localhost:11434/v1"
    provider = OpenAICompatProvider(cfg, transport=_LocalTransport())
    completion = provider.complete("sys", [Message(role="user", content="hi")])
    assert completion.text == "local answer"
    assert provider.is_local is True         # ...localhost is allowed
    ok, detail = provider.health()
    assert ok is True


def test_local_agents_are_never_reported_as_missing_keys(repo):
    config = load_config(repo)
    assert config.agents["ollama-uncensored"].key_family is None
    missing = {cfg.name for cfg in config.agents_missing_keys()}
    assert missing.isdisjoint(LOCAL)
    assert {"gemini", "deepseek", "openrouter"} <= missing          # hosted, no keys in tests
    assert set(config.key_families()) == {"gemini", "deepseek", "openrouter"}


def test_a_dead_local_server_falls_back_to_the_hosted_agent(repo, events, monkeypatch):
    """hermes down -> deepseek answers, and the user sees why."""
    from nexus.agent import NexusAgent
    from tests.conftest import FakeProvider, fake_registry, finish_completion, tool_completion

    cfg = load_config(repo)
    cfg.roles.get("backend").agent = "hermes"

    dead_local = FakeProvider(
        "hermes",
        [ProviderError("cannot reach http://localhost:11434/v1 (ConnectError)", retryable=True)],
        fallback="deepseek",
        healthy=False,
        health_detail="no local server at http://localhost:11434/v1",
    )
    hosted = FakeProvider("deepseek", [
        tool_completion([("write_file", {"path": "app/endpoint.py", "content": "# fixture endpoint\n"})]),
        finish_completion("finished on deepseek"),
    ])
    agent = NexusAgent(cfg, workspace=repo, providers=fake_registry(dead_local, hosted, config=cfg),
                       emit=lambda e: events.append(e.to_json()), auto_yes=True,
                       approval_callback=lambda *a, **k: None)

    result = agent.run("add an endpoint", role="backend")
    assert result.ok
    assert result.agent == "deepseek"
    assert result.summary == "finished on deepseek"
    warnings = [e for e in events if e["type"] == "agent" and e.get("level") == "warn"]
    assert warnings and "hermes" in warnings[0]["agent"]


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #


def run_cli(argv, capsys):
    import nexus.cli as cli

    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out + out.err


def test_cli_agents_shows_the_optional_local_lanes(repo, capsys):
    code, out = run_cli(["agents", "-C", str(repo)], capsys)
    assert code == 0
    assert "hermes3:8b" in out and "dolphin-mixtral:latest" in out
    assert "optional : yes" in out
    assert "(not needed" in out                  # local endpoints have no api key line


def test_doctor_treats_offline_local_models_as_warnings(repo, capsys):
    code, out = run_cli(["doctor", "-C", str(repo), "--json"], capsys)
    payload = {check["name"]: check for check in json.loads(out)}
    assert payload["agent:hermes"]["ok"] is None          # '?' instead of '✗'
    assert "optional" in payload["agent:hermes"]["hint"]
    assert payload["agent:gemini"]["ok"] is False          # hosted, still needs a key


def test_doctor_exit_code_ignores_optional_checks(capsys):
    """`?` rows (offline local models) must never make doctor fail."""
    import argparse

    from nexus.cli import _render_doctor

    checks = [
        {"name": "config", "ok": True, "detail": "ok", "hint": ""},
        {"name": "agent:hermes", "ok": None, "detail": "no local server", "hint": "ollama serve"},
    ]
    code = _render_doctor(checks, argparse.Namespace(json=False))
    out = capsys.readouterr().out
    assert code == 0
    assert "? agent:hermes" in out
    assert "all good." in out


# --------------------------------------------------------------------------- #
# Hermes & free agents, wired but not hidden
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", HOSTED_FREE)
def test_hosted_free_agents_exist_and_fall_back(repo, name):
    config = load_config(repo)
    cfg = config.agents[name]
    assert cfg.provider == "openrouter"
    assert cfg.key_family == "openrouter"
    assert cfg.fallback in config.agents
    assert cfg.stream is True


def test_openrouter_defaults_to_the_free_router(repo):
    config = load_config(repo)
    assert config.agents["openrouter"].model == "openrouter/free"
    assert config.agents["hermes-free"].model.endswith(":free")


def test_local_roles_are_part_of_the_team(repo):
    config = load_config(repo)
    for role_name, agent_name in (
        ("local-worker", "hermes"),
        ("local-heavy", "hermes-big"),
        ("uncensored-local", "ollama-uncensored"),
        ("local-chat", "ollama"),
    ):
        role = config.role(role_name)
        assert role.agent == agent_name
        assert config.agent_for_role(role).name == agent_name
    # and they are routable by keyword, not only reachable through /role
    from nexus.router import Router

    router = Router(config)
    decision = router.keyword_route("use hermes offline for this refactor")
    assert decision is not None and decision.role.name in {"local-worker", "local-heavy"}
    decision = router.keyword_route("run the uncensored local model on this sample")
    assert decision is not None and decision.role.name == "uncensored-local"
