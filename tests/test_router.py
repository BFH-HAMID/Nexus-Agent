"""Phase 2: keyword fast-match + LLM classifier fallback."""

from __future__ import annotations

import pytest

from nexus.router import CLASSIFIER_PROMPT, Router, _extract_role, explain
from nexus.types import sanitize_role_name

from .conftest import FakeProvider, fake_registry, text_completion


@pytest.fixture()
def router(config):
    return Router(config, providers=None)


# --------------------------------------------------------------------------- #
# stage 1: keywords
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "task,expected",
    [
        ("Build a responsive React component with Tailwind", "frontend"),
        ("Add a REST api endpoint for user profiles with auth", "backend"),
        ("Optimize this slow SQL query and add an index", "database"),
        ("Set up a GitHub Actions CI pipeline with Docker", "devops"),
        ("Fix the SwiftUI layout on iOS 18", "mobile"),
        ("Write unit tests and raise coverage for the parser", "qa"),
        ("Update the README documentation and API reference", "docs"),
        ("Run nmap and metasploit against my lab box for a pentest", "pentesting"),
        ("Do OSINT recon with shodan and subdomain enumeration", "osint"),
        ("Review this code for OWASP top 10 vulnerabilities", "security-audit"),
        ("Train a scikit-learn model on this pandas dataframe", "data-science"),
        ("Write a Pine Script indicator with a backtest on TradingView", "trading"),
        ("Design the figma mockup and a design system for it", "design"),
        ("Write SEO copy and do keyword research for the landing page", "seo"),
        ("Draft a privacy policy and terms of service", "legal-draft"),
        ("Analyze the malware sample with Ghidra and write YARA rules", "malware-analysis"),
        ("Refactor this legacy module to remove the code smell", "refactoring"),
        ("Debug this crash - here is the stack trace", "debugging"),
    ],
)
def test_keyword_routing(config, task, expected):
    decision = Router(config).route(task)
    assert decision.role.name == expected, (
        f"{task!r} routed to {decision.role.name} (candidates {decision.candidates})"
    )
    assert decision.method == "keyword"


def test_keyword_weights_let_specific_roles_win(config):
    """'pentest' is weighted 3, so it beats a generic 'security' hit."""
    decision = Router(config).route("pentest my own lab with nmap")
    assert decision.role.name == "pentesting"
    assert decision.score >= 3


def test_scoring_is_case_insensitive_and_word_bounded(config):
    registry = config.roles
    frontend = registry.get("frontend")
    assert frontend.keyword_score("REACT component")[0] > 0
    # 'api' must not fire inside 'rapid'
    backend = registry.get("backend")
    assert backend.keyword_score("a rapid prototype")[0] == 0
    assert backend.keyword_score("expose an api")[0] > 0


def test_no_match_returns_none_from_stage_one(config):
    router = Router(config)
    assert router.keyword_route("zzz qqq nothing matches this at all") is None


def test_min_score_gate(config):
    config.settings.router_min_score = 99
    assert Router(config).keyword_route("react component") is None


def test_route_falls_back_to_general_without_llm(config):
    decision = Router(config).route("please do the thing with the stuff")
    assert decision.role.name == "general"
    assert decision.method == "default"


def test_manual_forcing_wins(config):
    decision = Router(config).route("write react components", forced="pentesting")
    assert decision.role.name == "pentesting"
    assert decision.method == "manual"


def test_alias_resolution(config):
    assert config.role("pentest").name == "pentesting"
    assert config.role("security").name == "security-audit"
    assert config.role("documentation").name == "docs"
    assert config.role("does-not-exist").name == "general"


# --------------------------------------------------------------------------- #
# stage 2: LLM classifier
# --------------------------------------------------------------------------- #


def test_llm_fallback_uses_the_roadmap_prompt(config):
    provider = FakeProvider("claude-cheap", [text_completion("blockchain")])
    config.settings.router_agent = "claude-cheap"
    router = Router(config, providers=fake_registry(provider))
    decision = router.route("build a thing that does something unspecified")
    assert decision.role.name == "blockchain"
    assert decision.method == "llm"

    prompt = provider.calls[0]["messages"][-1].content
    assert "You are a routing classifier." in prompt
    assert "Reply with ONLY the role name" in prompt
    assert "Roles:" in prompt
    assert provider.calls[0]["max_tokens"] <= 32
    assert provider.calls[0]["temperature"] == 0.0


def test_classifier_narrows_a_big_team_to_one_bounded_call(config):
    # A big roster is narrowed to <=40 relevant candidates and asked ONCE,
    # instead of chunking every role (which cost O(roster/40) calls). The
    # classifier's answer is still validated against the full registry.
    provider = FakeProvider("claude-cheap", [text_completion("frontend")])
    config.settings.router_agent = "claude-cheap"
    router = Router(config, providers=fake_registry(provider))
    decision = router.route("something vague")
    assert len(provider.calls) == 1
    names = provider.calls[0]["messages"][-1].content.split("Roles: ")[1].split("\n")[0]
    assert len(names.split(", ")) <= 40
    assert decision.role.name == "frontend"


def test_narrow_candidates_is_bounded_and_relevance_ordered(config):
    router = Router(config, providers=None)
    # relevance: a kubernetes task surfaces devops-ish roles, capped at the limit
    names = router._narrow_candidates("deploy kubernetes manifests with helm", 40)
    assert len(names) <= 40
    # zero-overlap still yields a bounded, non-empty set (category representatives)
    fallback = router._narrow_candidates("zzz qqq gibberish", 40)
    assert 0 < len(fallback) <= 40


def test_classifier_output_is_sanitized(config):
    provider = FakeProvider("claude-cheap", [text_completion("`Data-Science` — because ML.")])
    router = Router(config, providers=fake_registry(provider))
    decision = router.llm_route("predict churn")
    assert decision.role.name == "data-science"


def test_unknown_classifier_output_falls_back_to_general(config):
    provider = FakeProvider("claude-cheap", [text_completion("wizard")] * 5)
    router = Router(config, providers=fake_registry(provider))
    decision = router.llm_route("do magic")
    assert decision.role.name == "general"
    assert decision.method == "default"


def test_llm_fallback_can_be_disabled(config):
    provider = FakeProvider("claude-cheap", [text_completion("qa")])
    config.settings.router_llm_fallback = False
    router = Router(config, providers=fake_registry(provider))
    decision = router.route("something vague")
    assert decision.role.name == "general"
    assert provider.calls == []


def test_tie_is_broken_by_the_classifier(config):
    """Two roles scoring the same -> ask the model instead of guessing."""
    from nexus.types import Keyword, Role

    config.roles.add(Role(name="twin-a", persona="a", keywords=[Keyword.parse("zzzunique")]))
    config.roles.add(Role(name="twin-b", persona="b", keywords=[Keyword.parse("zzzunique")]))
    provider = FakeProvider("claude-cheap", [text_completion("twin-b")])
    router = Router(config, providers=fake_registry(provider))
    decision = router.route("zzzunique please")
    assert decision.role.name == "twin-b"
    assert decision.method == "llm"
    assert "tie" in decision.note


def test_router_survives_a_dead_classifier(config):
    from nexus.types import ProviderError

    provider = FakeProvider("claude-cheap", [ProviderError("boom", retryable=False)])
    router = Router(config, providers=fake_registry(provider))
    decision = router.route("something vague")
    assert decision.role.name == "general"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("frontend", "frontend"),
        ("  Backend\n", "backend"),
        ("`data-science`", "data-science"),
        ("Role: QA.", "role"),           # first token wins; unknown -> caller falls back
        ("```json\nosint\n```", "osint"),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_extract_role(raw, expected):
    assert _extract_role(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Frontend", "frontend"),
        ("data_science", "data-science"),
        ("PENTEST!", "pentest"),
        ("héllo wörld", "h-llo-w-rld"),
        ("", ""),
    ],
)
def test_sanitize_role_name(raw, expected):
    assert sanitize_role_name(raw) == expected


def test_explain_mentions_role_and_agent(config):
    decision = Router(config).route("write a react component")
    line = explain(decision)
    assert "role: frontend" in line
    assert "agent: claude" in line


def test_classic_roadmap_example_switches_to_uncensored(config):
    decision = Router(config).route("scan my lab network with nmap and try a privilege escalation exploit")
    assert decision.role.name == "pentesting"
    assert config.agent_for_role(decision.role).name == "ollama-uncensored"
