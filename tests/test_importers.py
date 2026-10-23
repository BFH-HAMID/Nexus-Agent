"""Tests for `nexus addagent` - importing agents from a GitHub repo as roles.

Discovery/conversion run against local fixture dirs, so nothing here touches the
network. A final group validates the committed roles.d packs that ship in the
repo (the five the user asked to import).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

import nexus.importers as imp
from nexus.config import load_config
from nexus.types import sanitize_role_name


# --------------------------------------------------------------------------- #
# URL parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url,expected",
    [
        ("obra/superpowers", ("obra", "superpowers", None, None, None)),
        ("https://github.com/obra/superpowers", ("obra", "superpowers", None, None, None)),
        ("https://github.com/obra/superpowers.git", ("obra", "superpowers", None, None, None)),
        ("git@github.com:morluto/rea.git", ("morluto", "rea", None, None, None)),
        (
            "https://github.com/affaan-m/ecc/tree/main/skills",
            ("affaan-m", "ecc", "main", "skills", None),
        ),
        (
            "https://github.com/obra/superpowers/blob/main/skills/x/SKILL.md",
            ("obra", "superpowers", "main", None, "skills/x/SKILL.md"),
        ),
    ],
)
def test_parse_github_url(url, expected):
    assert imp.parse_github_url(url) == expected


def test_parse_github_url_rejects_junk():
    with pytest.raises(imp.ImportError_):
        imp.parse_github_url("https://example.com/not/github")
    with pytest.raises(imp.ImportError_):
        imp.parse_github_url("")


# --------------------------------------------------------------------------- #
# frontmatter + helpers
# --------------------------------------------------------------------------- #


def test_read_frontmatter_splits_meta_and_body():
    meta, body = imp.read_frontmatter("---\nname: x\ndescription: y\n---\n\nHello body\n")
    assert meta == {"name": "x", "description": "y"}
    assert body.strip() == "Hello body"


def test_read_frontmatter_no_frontmatter():
    meta, body = imp.read_frontmatter("just text\n")
    assert meta == {}
    assert body == "just text\n"


def test_humanize_and_keywords():
    assert imp.humanize("academic-anthropologist") == "Academic Anthropologist"
    kws = imp.make_keywords("ai-chess-agent", "AI Chess Agent", "plays chess against a human")
    assert "chess" in kws
    assert "ai-chess-agent" in kws
    # stopwords are dropped
    assert "agent" not in kws and "the" not in kws


# --------------------------------------------------------------------------- #
# discovery fixtures (one per strategy)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def skill_repo(tmp_path: Path) -> Path:
    """superpowers/ecc/rea style: skills/<name>/SKILL.md with frontmatter."""
    root = tmp_path / "skillrepo"
    (root / "skills" / "brainstorming").mkdir(parents=True)
    (root / "skills" / "brainstorming" / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: brainstorming
            description: Use before any creative work to find out what to build.
            ---

            # Brainstorming

            You are very good at building. Ask why before anything gets built.
            Lay out approaches, write the design doc, get approval.
            """
        ),
        encoding="utf-8",
    )
    # a docs/ mirror that must be ignored
    (root / "docs" / "es" / "skills" / "brainstorming").mkdir(parents=True)
    (root / "docs" / "es" / "skills" / "brainstorming" / "SKILL.md").write_text(
        "---\nname: brainstorming\ndescription: espanol\n---\n\n# Lluvia de ideas\n\nTexto en espanol.\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def persona_repo(tmp_path: Path) -> Path:
    """agency-agents style: <category>/<agent>.md with name+description."""
    root = tmp_path / "personarepo"
    (root / "academic").mkdir(parents=True)
    (root / "academic" / "academic-anthropologist.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: Anthropologist
            description: Expert in cultural systems, rituals and kinship.
            emoji: X
            ---

            # Anthropologist Agent Personality

            You are Anthropologist, a cultural anthropologist with fieldwork sensibility.
            You think in systems of meaning, not checklists of exotic traits.
            """
        ),
        encoding="utf-8",
    )
    # a description-only command stub (no name) must be skipped
    (root / "commands").mkdir()
    (root / "commands" / "aside.md").write_text(
        "---\ndescription: Answer a quick side question without losing context.\n---\n\n# Aside\n\nAsk a question mid-task.\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def app_repo(tmp_path: Path) -> Path:
    """awesome-llm-apps style: <agents-dir>/<app>/{README.md, code}."""
    root = tmp_path / "apprepo"
    app = root / "starter_ai_agents" / "ai_chess_agent"
    app.mkdir(parents=True)
    (app / "README.md").write_text(
        "# AI Chess Agent\n\nAn agent that plays chess against a human using an LLM.\n",
        encoding="utf-8",
    )
    (app / "main.py").write_text("print('chess')\n", encoding="utf-8")
    # a numbered tutorial lesson under a crash-course dir must be skipped
    lesson = root / "ai_agent_framework_crash_course" / "1_starter_agent"
    lesson.mkdir(parents=True)
    (lesson / "README.md").write_text("# Lesson 1\n\nA starter lesson.\n", encoding="utf-8")
    (lesson / "main.py").write_text("print('lesson')\n", encoding="utf-8")
    # a nested structural README (backend/agents) must not become a role
    nested = root / "starter_ai_agents" / "big_app" / "backend" / "agents"
    nested.mkdir(parents=True)
    (nested / "README.md").write_text("# Agents\n\nInternal agents dir.\n", encoding="utf-8")
    (nested / "x.py").write_text("x = 1\n", encoding="utf-8")
    return root


def test_discover_skills_ignores_docs_mirrors(skill_repo):
    found = imp.discover_agents(skill_repo, "skillrepo")
    assert len(found) == 1
    assert found[0].slug == "brainstorming"
    assert found[0].kind == "skill"
    assert "docs/es" not in found[0].source


def test_discover_personas_requires_name(persona_repo):
    found = imp.discover_agents(persona_repo, "personarepo")
    slugs = {a.slug for a in found}
    assert "academic-anthropologist" in slugs
    # the description-only command stub is not an agent
    assert "aside" not in slugs
    anthro = next(a for a in found if a.slug == "academic-anthropologist")
    assert anthro.category == "academic"
    assert anthro.title == "Anthropologist"


def test_discover_apps_skips_lessons_and_structural_dirs(app_repo):
    found = imp.discover_agents(app_repo, "apprepo")
    slugs = {a.slug for a in found}
    assert "ai-chess-agent" in slugs
    # numbered lesson under a crash-course container is skipped
    assert not any(s.startswith("1-") or "starter-agent" == s for s in slugs)
    # nested structural dir "agents" is not a role
    assert "agents" not in slugs
    chess = next(a for a in found if a.slug == "ai-chess-agent")
    assert chess.kind == "app"
    assert "chess" in chess.persona.lower()


# --------------------------------------------------------------------------- #
# conversion
# --------------------------------------------------------------------------- #


def test_convert_dedupes_and_protects_taken_names(skill_repo):
    agents = imp.discover_agents(skill_repo, "skillrepo")
    # 'brainstorming' already taken by a curated core role -> suffixed
    roles, skipped = imp.convert(agents, taken={"brainstorming"}, repo_slug="skillrepo")
    assert len(roles) == 1
    assert roles[0]["name"] == "brainstorming-skillrepo"
    assert "brainstorming" in roles[0]["aliases"]
    assert skipped == 0


def test_convert_maps_security_to_uncensored():
    raw = imp.RawAgent(
        slug="pentest-helper", title="Pentest Helper", description="authorized pentesting",
        persona="You are an authorized penetration testing assistant.", category="security",
        source="security/pentest-helper.md", kind="persona", repo="r",
    )
    roles, _ = imp.convert([raw], taken=set(), repo_slug="r")
    assert roles[0]["agent"] == "ollama-uncensored"


def test_convert_forced_agent_overrides():
    raw = imp.RawAgent(
        slug="x", title="X", description="d", persona="You are X " * 20,
        category="security", source="x.md", kind="persona", repo="r",
    )
    roles, _ = imp.convert([raw], taken=set(), repo_slug="r", forced_agent="hermes")
    assert roles[0]["agent"] == "hermes"


def test_convert_limit_skips_excess():
    raws = [
        imp.RawAgent(
            slug=f"role-{i}", title=f"Role {i}", description="a specialist agent " * 3,
            persona="You are a specialist. " * 6, category="c", source=f"{i}.md",
            kind="persona", repo="r",
        )
        for i in range(3)
    ]
    roles, skipped = imp.convert(raws, taken=set(), repo_slug="r", limit=2)
    assert len(roles) == 2
    assert skipped == 1


# --------------------------------------------------------------------------- #
# pack rendering + roles.d loading
# --------------------------------------------------------------------------- #


def test_render_pack_is_valid_yaml_and_loads(skill_repo, tmp_path):
    agents = imp.discover_agents(skill_repo, "skillrepo")
    roles, _ = imp.convert(agents, taken=set(), repo_slug="skillrepo")
    text = imp.render_pack_yaml("skillrepo", "https://github.com/o/skillrepo", roles)
    data = yaml.safe_load(text)
    assert "roles" in data and "brainstorming" in data["roles"]
    entry = data["roles"]["brainstorming"]
    assert entry["persona"] and entry["keywords"] and entry["category"] == "skills"

    # a workspace-local pack is picked up by load_config
    ws = tmp_path / "ws"
    (ws / ".nexus" / "roles.d").mkdir(parents=True)
    (ws / ".nexus" / "roles.d" / "skillrepo.yaml").write_text(text, encoding="utf-8")
    cfg = load_config(ws)
    assert "brainstorming" in cfg.roles
    assert cfg.role("brainstorming").category == "skills"


def test_import_repo_dry_run_writes_nothing(skill_repo, tmp_path, monkeypatch):
    # point fetch at the local fixture by stubbing fetch_repo
    monkeypatch.setattr(imp, "fetch_repo", lambda *a, **k: skill_repo)
    ws = tmp_path / "ws"
    ws.mkdir()
    result = imp.import_repo(
        "https://github.com/o/skillrepo", workspace=ws, dest_kind="project", dry_run=True
    )
    assert result.imported >= 1
    assert result.dry_run is True
    assert not (ws / ".nexus" / "roles.d" / "skillrepo.yaml").exists()


def test_import_repo_writes_pack_and_is_idempotent_with_force(skill_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(imp, "fetch_repo", lambda *a, **k: skill_repo)
    ws = tmp_path / "ws"
    ws.mkdir()
    r1 = imp.import_repo("https://github.com/o/skillrepo", workspace=ws, dest_kind="project")
    pack = ws / ".nexus" / "roles.d" / "skillrepo.yaml"
    assert r1.imported >= 1 and pack.exists()
    # second run without --force refuses to clobber
    r2 = imp.import_repo("https://github.com/o/skillrepo", workspace=ws, dest_kind="project")
    assert any("already exists" in n for n in r2.notes)
    # --force overwrites cleanly
    r3 = imp.import_repo("https://github.com/o/skillrepo", workspace=ws, dest_kind="project", force=True)
    assert r3.imported >= 1 and pack.exists()


# --------------------------------------------------------------------------- #
# the committed packs that ship in the repo
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLES_D = REPO_ROOT / "roles.d"
EXPECTED_PACKS = {"superpowers", "agency-agents", "ecc", "rea", "awesome-llm-apps"}


def test_committed_packs_exist():
    assert ROLES_D.is_dir(), "roles.d/ should ship the imported packs"
    present = {p.stem for p in ROLES_D.glob("*.yaml")}
    assert EXPECTED_PACKS <= present, f"missing packs: {EXPECTED_PACKS - present}"


def test_committed_packs_are_valid_and_unique():
    seen: set[str] = set()
    total = 0
    for pack in sorted(ROLES_D.glob("*.yaml")):
        data = yaml.safe_load(pack.read_text(encoding="utf-8")) or {}
        roles = data.get("roles") or {}
        assert roles, f"{pack.name} has no roles"
        for name, cfg in roles.items():
            slug = sanitize_role_name(name)
            assert slug == name, f"{pack.name}: bad role name {name!r}"
            assert isinstance(cfg, dict)
            assert (cfg.get("persona") or "").strip(), f"{pack.name}/{name}: empty persona"
            assert isinstance(cfg.get("keywords") or [], list)
            assert cfg.get("category"), f"{pack.name}/{name}: no category"
            key = name
            assert key not in seen, f"duplicate role name across packs: {name}"
            seen.add(key)
            total += 1
    # the five repos contribute a large roster
    assert total >= 800, f"expected 800+ imported roles, got {total}"


def test_committed_packs_merge_with_core_without_collision():
    # Build the registry straight from core + packs. (load_config is isolated
    # from the committed roles.d by an autouse fixture, so merge explicitly.)
    from nexus.config import build_registry, expand_env

    core_path = REPO_ROOT / "roles.yaml"
    core = yaml.safe_load(core_path.read_text(encoding="utf-8")) or {}
    layers = [(expand_env(core), str(core_path))]
    for pack in sorted(ROLES_D.glob("*.yaml")):
        data = yaml.safe_load(pack.read_text(encoding="utf-8")) or {}
        layers.append((expand_env(data), str(pack)))
    registry = build_registry(layers)

    core_names = {sanitize_role_name(str(c.get("name", n))) for n, c in (core.get("roles") or {}).items()}
    all_names = set(registry.names())
    # every curated core role survives the merge, and imports never clobber it
    assert core_names <= all_names
    assert len(registry) >= len(core_names) + 800
    # a known imported role resolves with its pack category
    assert registry.get("academic-anthropologist").category == "academic"
