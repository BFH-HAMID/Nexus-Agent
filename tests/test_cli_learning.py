"""CLI surfaces for skills, memory, recall, the Hermes bridge and self-update."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

import nexus.cli as cli
import nexus.hermes as hermes
from nexus import skills as skilllib
from nexus.sessions import SessionStore

from .conftest import FakeProvider, fake_registry, finish_completion


@pytest.fixture(autouse=True)
def _no_color(monkeypatch):
    monkeypatch.setattr(cli, "COLORS", cli.C(enabled=False))


def run(argv, **kwargs) -> tuple[int, str]:
    buffer = io.StringIO()
    old = sys.stdout
    sys.stdout = buffer
    try:
        code = cli.main(argv)
    finally:
        sys.stdout = old
    return code, buffer.getvalue()


BODY = (
    "# Sample Skill\n\n## When to Use\nWhen you want a sample.\n\n## Procedure\n1. do it\n"
    "2. check it\n\n## Verification\n1. it worked\n"
)


def _skill(name: str = "sample-skill", **kwargs) -> Path:
    return skilllib.create_skill(
        name=name, description="A sample skill for the CLI tests.", body=BODY, tags=["test"], **kwargs
    )


# --------------------------------------------------------------------------- #
# nexus skills
# --------------------------------------------------------------------------- #


def test_skills_list_shows_bundled_and_local():
    code, out = run(["skills"])
    assert code == 0
    assert "add-a-role" in out  # ships with the repo
    _skill()
    code, out = run(["skills"])
    assert "sample-skill" in out


def test_skills_json_and_search():
    _skill()
    code, out = run(["skills", "--json"])
    assert code == 0
    rows = json.loads(out)
    assert any(row["name"] == "sample-skill" for row in rows)

    code, out = run(["skills", "--search", "sample"])
    assert code == 0 and "sample-skill" in out
    assert "add-a-role" not in out


def test_skills_show_unknown_and_known():
    _skill()
    code, out = run(["skills", "show", "sample-skill"])
    assert code == 0 and "## Procedure" in out
    code, out = run(["skills", "show", "nope"])
    assert code == 1


def test_skills_delete():
    _skill()
    code, out = run(["skills", "delete", "sample-skill"])
    assert code == 0 and "deleted" in out
    code, out = run(["skills", "delete", "sample-skill"])
    assert code == 1


def test_skills_path():
    code, out = run(["skills", "path"])
    assert code == 0 and "skills" in out


def test_skills_curate_reports_and_applies():
    _skill()
    code, out = run(["skills", "curate", "--stale-days", "-1"])
    assert code == 0 and "curator" in out
    code, out = run(["skills", "curate", "--stale-days", "-1", "--apply"])
    assert code == 0 and "archived" in out


def test_skills_import_offline_uses_a_local_checkout(monkeypatch, tmp_path):
    upstream = tmp_path / "hermes" / "skills" / "demo"
    upstream.mkdir(parents=True)
    (upstream / "SKILL.md").write_text(
        "---\nname: demo-imported\ndescription: Imported for the CLI test.\n---\n\n# Demo\n", encoding="utf-8"
    )
    monkeypatch.setenv("NEXUS_HERMES_CHECKOUT", str(tmp_path / "hermes"))
    code, out = run(["skills", "import", "--offline"])
    assert code == 0, out
    assert "imported 1" in out
    assert skilllib.get("demo-imported") is not None


# --------------------------------------------------------------------------- #
# nexus memory / recall
# --------------------------------------------------------------------------- #


def test_memory_add_list_remove_json():
    code, out = run(["memory", "add", "the API lives in app/api.py"])
    assert code == 0
    code, out = run(["memory"])
    assert "app/api.py" in out
    code, out = run(["memory", "add", "prefers terse answers", "--user"])
    assert code == 0
    code, out = run(["memory", "--json"])
    assert json.loads(out)["user"]["entries"] == ["prefers terse answers"]
    code, out = run(["memory", "remove", "api.py"])
    assert code == 0


def test_memory_import_from_a_hermes_home(tmp_path):
    source = tmp_path / "hermes"
    source.mkdir()
    (source / "MEMORY.md").write_text("imported fact", encoding="utf-8")
    code, out = run(["memory", "import", "--source", str(source)])
    assert code == 0 and "imported 1" in out


def test_recall_lists_and_searches():
    store = SessionStore()
    store.record(_result(), "add a rate limiter")
    code, out = run(["recall"])
    assert code == 0 and "rate limiter" in out
    code, out = run(["recall", "limiter"])
    assert code == 0 and "rate limiter" in out
    code, out = run(["recall", "--json"])
    assert isinstance(json.loads(out), list)


def _result():
    from nexus.agent import RunResult
    from nexus.types import Message, TextBlock

    return RunResult(
        text="limiter added", role="backend", agent="fake", method="keyword", turns=2, tool_calls=3,
        summary="token bucket on /api", messages=[Message(role="user", content=[TextBlock(text="limits")])],
    )


# --------------------------------------------------------------------------- #
# nexus hermes / update
# --------------------------------------------------------------------------- #


def test_hermes_status(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_HERMES_CHECKOUT", str(tmp_path / "missing"))
    monkeypatch.setattr(hermes, "find_cli", lambda: None)
    code, out = run(["hermes"])
    assert code == 0
    assert "hermes-agent bridge" in out
    assert "not cloned" in out


def test_hermes_run_without_a_cli(monkeypatch):
    monkeypatch.setattr(hermes, "find_cli", lambda: None)
    code, out = run(["hermes", "run", "hello"])
    assert code == 1
    assert "not installed" in out


def test_hermes_sync_imports_skills_and_memories(monkeypatch, tmp_path):
    checkout = tmp_path / "hermes"
    skill_dir = checkout / "skills" / "cat" / "synced"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: synced-skill\ndescription: Synced from upstream.\n---\n\n# Synced\n", encoding="utf-8"
    )
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text("upstream remembered this", encoding="utf-8")
    monkeypatch.setenv("NEXUS_HERMES_CHECKOUT", str(checkout))
    monkeypatch.setattr(hermes, "ensure_checkout", lambda **_kw: (checkout, ""))
    code, out = run(["hermes", "sync", "--memories", str(memories)])
    assert code == 0, out
    assert "synced 1 skills and 1 memory entries" in out


def test_update_cli_routes_safe_options(monkeypatch, tmp_path):
    from nexus import updater

    seen = {}
    def update(root, **kwargs):
        seen.update(kwargs)
        return True, ['Check only: no installation.']
    monkeypatch.setattr(updater, 'update', update)
    code, out = run(['update', '--check', '--extras', 'hud,mcp'])
    assert code == 0 and 'no installation' in out
    assert seen['check'] is True and seen['extras'] == 'hud,mcp'


def test_update_cli_reports_failure(monkeypatch):
    from nexus import updater

    monkeypatch.setattr(updater, 'update', lambda *a, **k: (False, ['Local changes found.']))
    code, out = run(['update', '--check'])
    assert code == 1 and 'Local changes' in out


# --------------------------------------------------------------------------- #
# slash commands
# --------------------------------------------------------------------------- #


def test_slash_commands_in_the_repl(repo, capsys):
    from nexus.agent import NexusAgent
    from nexus.config import load_config

    cfg = load_config(repo)
    provider = FakeProvider("gemini", [finish_completion("ok")])
    agent = NexusAgent(cfg, workspace=repo, providers=fake_registry(provider, config=cfg),
                       emit=lambda _e: None, auto_yes=True)
    _skill("repl-skill")

    cli.handle_slash(agent, "/skills")
    out = capsys.readouterr().out
    assert "repl-skill" in out

    cli.handle_slash(agent, "/skill repl-skill")
    out = capsys.readouterr().out
    assert "## Procedure" in out

    cli.handle_slash(agent, "/repl-skill add a detail")
    assert agent.pending_skill is None  # consumed by the run
    capsys.readouterr()

    cli.handle_slash(agent, "/memory global")
    assert "MEMORY.md" in capsys.readouterr().out

    cli.handle_slash(agent, "/recall")
    capsys.readouterr()

    cli.handle_slash(agent, "/curate")
    assert "curator" in capsys.readouterr().out

    cli.handle_slash(agent, "/nope-not-a-skill")
    assert "unknown command" in capsys.readouterr().out


def test_slash_skill_can_run_a_task(repo, capsys):
    from nexus.agent import NexusAgent
    from nexus.config import load_config

    cfg = load_config(repo)
    provider = FakeProvider("gemini", [finish_completion("did the thing")])
    agent = NexusAgent(cfg, workspace=repo, providers=fake_registry(provider, config=cfg),
                       emit=lambda _e: None, auto_yes=True)
    _skill("repl-skill")
    cli.handle_slash(agent, "/skill repl-skill do the thing")
    out = capsys.readouterr().out
    assert "did the thing" in out
    system = provider.calls[0]["system"]
    assert "# Active skill: repl-skill" in system
