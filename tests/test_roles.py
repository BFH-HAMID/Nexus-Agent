"""Phase 3: the role registry itself is data - test the data."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLES_FILE = REPO_ROOT / "roles.yaml"
AGENTS_FILE = REPO_ROOT / "agents.yaml"


@pytest.fixture(scope="module")
def roles_yaml() -> dict:
    return yaml.safe_load(ROLES_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def agents_yaml() -> dict:
    return yaml.safe_load(AGENTS_FILE.read_text(encoding="utf-8"))


def test_roles_file_exists_and_is_a_mapping(roles_yaml):
    assert isinstance(roles_yaml, dict)
    assert isinstance(roles_yaml["roles"], dict)


def test_phase3_target_of_100_plus_roles(roles_yaml):
    assert len(roles_yaml["roles"]) >= 100, f"only {len(roles_yaml['roles'])} roles"


def test_every_role_has_the_required_fields(roles_yaml):
    for name, role in roles_yaml["roles"].items():
        assert role.get("title"), f"{name}: missing title"
        assert role.get("category"), f"{name}: missing category"
        assert role.get("persona"), f"{name}: missing persona"
        assert "agent" in role, f"{name}: missing agent key"
        assert role["persona"].startswith("You are"), f"{name}: persona should start with 'You are'"


def test_role_names_are_valid_slugs(roles_yaml):
    import re

    for name in roles_yaml["roles"]:
        assert re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", name), f"invalid role name: {name}"


def test_agents_referenced_by_roles_exist(roles_yaml, agents_yaml):
    known = set(agents_yaml["agents"]) | {""}
    for name, role in roles_yaml["roles"].items():
        assert role.get("agent", "") in known, f"{name}: unknown agent '{role['agent']}'"


def test_keywords_are_lowercase_and_non_empty(roles_yaml):
    for name, role in roles_yaml["roles"].items():
        for kw in role.get("keywords") or []:
            text = kw["text"] if isinstance(kw, dict) else kw
            weight = kw.get("weight", 1) if isinstance(kw, dict) else 1
            assert isinstance(text, str) and text.strip(), f"{name}: empty keyword"
            assert text == text.lower(), f"{name}: keyword '{text}' must be lowercase"
            assert float(weight) > 0, f"{name}: bad weight for '{text}'"


def test_aliases_do_not_collide_with_role_names(roles_yaml):
    names = set(roles_yaml["roles"])
    seen: dict[str, str] = {}
    for name, role in roles_yaml["roles"].items():
        for alias in role.get("aliases") or []:
            assert alias not in names, f"{name}: alias '{alias}' shadows a real role"
            assert alias not in seen, f"{name}: alias '{alias}' already used by {seen.get(alias)}"
            seen[alias] = name


def test_roadmap_model_mapping(roles_yaml):
    """The mapping table from the roadmap must hold."""
    roles = roles_yaml["roles"]
    expected = {
        "frontend": "claude",
        "backend": "claude",
        "database": "claude",
        "mobile": "claude",
        "data-science": "claude",
        "security-audit": "claude",
        "devops": "hermes",
        "marketing": "hermes",
        "trading": "hermes",
        "qa": "hermes",
        "docs": "hermes",
        "pentesting": "ollama-uncensored",
        "osint": "ollama-uncensored",
    }
    for role, agent in expected.items():
        assert role in roles, f"roadmap role '{role}' is missing"
        assert roles[role]["agent"] == agent, f"{role} should route to {agent}, got {roles[role]['agent']}"


def test_offensive_roles_are_the_uncensored_ones(roles_yaml):
    uncensored = {n for n, r in roles_yaml["roles"].items() if r.get("agent") == "ollama-uncensored"}
    assert {"pentesting", "osint"} <= uncensored
    # defensive/business roles must never end up on the uncensored model
    for name in ("frontend", "backend", "qa", "docs", "finance", "legal-draft", "design"):
        assert name not in uncensored


def test_general_is_the_fallback_and_inherits_the_default_agent(roles_yaml, agents_yaml):
    general = roles_yaml["roles"]["general"]
    assert general["agent"] == ""
    assert not general.get("keywords")
    assert agents_yaml["default_agent"] == "claude"


def test_pentesting_persona_keeps_the_authorization_guardrail(roles_yaml):
    persona = roles_yaml["roles"]["pentesting"]["persona"].lower()
    assert "authorized" in persona
    assert "refuse" in persona
    osint_persona = roles_yaml["roles"]["osint"]["persona"].lower()
    assert "authorized" in osint_persona and "refuse" in osint_persona


def test_generator_and_yaml_stay_in_sync():
    """CI guard: roles.yaml must be regenerable from scripts/build_roles.py."""
    out = REPO_ROOT / "roles.yaml.check"
    try:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "build_roles.py"), "--out", str(out)],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stderr
        assert out.read_text(encoding="utf-8") == ROLES_FILE.read_text(encoding="utf-8"), (
            "roles.yaml is out of sync with scripts/build_roles.py"
        )
    finally:
        out.unlink(missing_ok=True)


def test_categories_cover_the_roadmap(roles_yaml):
    categories = {r["category"] for r in roles_yaml["roles"].values()}
    assert {"development", "security", "business", "creative", "data-ai", "qa", "docs", "general"} <= categories


def test_load_config_registers_every_role(repo):
    from nexus.config import load_config

    cfg = load_config(repo)
    assert len(cfg.roles) >= 100
    assert "general" in cfg.roles.roles
    assert cfg.role("pentesting").agent == "ollama-uncensored"
    assert cfg.agent_for_role(cfg.role("pentesting")).tool_mode == "text"
    # project-local roles merge on top of the bundled ones
    (repo / ".nexus").mkdir(exist_ok=True)
    (repo / ".nexus" / "roles.yaml").write_text(
        "roles:\n  repo-historian:\n    title: Historian\n    category: docs\n"
        "    agent: hermes\n    persona: You are the repo historian.\n    keywords: [convention]\n",
        encoding="utf-8",
    )
    cfg2 = load_config(repo)
    assert len(cfg2.roles) == len(cfg.roles) + 1
    assert cfg2.role("repo-historian").agent == "hermes"
    assert cfg2.role("pentesting").agent == "ollama-uncensored"
