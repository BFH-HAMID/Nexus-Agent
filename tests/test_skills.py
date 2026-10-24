"""Skills: procedural memory ported from Nous Research's hermes-agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus import skills as skilllib


def _make(tmp_path: Path, name: str = "ship-it", **kwargs) -> Path:
    defaults = dict(
        name=name,
        description="Ship a small change end to end.",
        body="# Ship It Skill\n\n## When to Use\nA change is ready.\n\n## Procedure\n1. run tests\n",
        tags=["release", "git"],
        category="learned",
        root=tmp_path,
    )
    defaults.update(kwargs)
    return skilllib.create_skill(**defaults)


def test_create_roundtrip_and_frontmatter(tmp_path):
    path = _make(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: ship-it" in text
    skill = skilllib.load_skill(path, root=tmp_path)
    assert skill is not None
    assert skill.name == "ship-it"
    assert skill.description == "Ship a small change end to end."
    assert skill.tags == ["release", "git"]
    assert skill.category == "learned"
    assert "## Procedure" in skill.body


def test_create_rejects_bad_names_and_duplicates(tmp_path):
    with pytest.raises(ValueError):
        _make(tmp_path, name="Not A Name")
    _make(tmp_path)
    with pytest.raises(FileExistsError):
        _make(tmp_path)


def test_description_is_capped_at_sixty_chars(tmp_path):
    long = "x" * 200 + "."
    path = _make(tmp_path, description=long)
    skill = skilllib.load_skill(path, root=tmp_path)
    assert len(skill.description) <= skilllib.DESCRIPTION_LIMIT == 60


def test_lint_flags_the_authoring_rules():
    warnings = skilllib.lint("Bad Name", "", "", tags=[])
    joined = " ".join(warnings)
    assert "name must be lowercase" in joined
    assert "description is required" in joined

    warnings = skilllib.lint("fine-name", "y" * 80, "# body")
    assert any("max 60" in w for w in warnings)
    assert any("period" in w for w in warnings)

    good = (
        "# Good Skill\n\n## When to Use\nYou want the thing.\n\n## Procedure\n1. step one\n"
        "2. step two\n\n## Verification\n1. it works\n"
    )
    assert skilllib.lint("fine-name", "One clear sentence.", good) == []


def test_discover_select_and_prompt_section(tmp_path):
    _make(tmp_path, name="deploy-docker", description="Deploy the app with Docker Compose.",
          tags=["docker"], body="# Deploy\n\n## Procedure\n1. docker compose up -d\n")
    _make(tmp_path, name="write-changelog", description="Write a changelog from git log.", tags=["docs"])

    found = skilllib.discover(roots=[tmp_path])
    assert {s.name for s in found} == {"deploy-docker", "write-changelog"}
    selected = skilllib.select(found, "deploy the docker stack")
    assert selected and selected[0].name == "deploy-docker"

    section = skilllib.render_prompt_section(selected, found)
    assert "# Skills (procedural memory)" in section
    assert "deploy-docker" in section
    assert "skill_manage" in section


def test_prompt_description_is_shortened(tmp_path):
    path = tmp_path / "long-desc" / skilllib.SKILL_FILE
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: long-desc\ndescription: " + "a" * 80 + "\n---\n\n# Body\n",
        encoding="utf-8",
    )
    skill = skilllib.load_skill(path, root=tmp_path)
    assert len(skill.prompt_description) <= skilllib.DESCRIPTION_LIMIT
    assert skill.prompt_description.endswith("…")


def test_patch_skill_changes_body_and_description(tmp_path):
    path = _make(tmp_path)
    skilllib.patch_skill(path, body="# New body\n\n## Procedure\n1. no\n", description="Shorter.")
    text = path.read_text(encoding="utf-8")
    assert "description: Shorter." in text
    assert "# New body" in text
    assert "## When to Use" not in text


def test_usage_tracking_and_curator(tmp_path):
    path = _make(tmp_path)
    skill = skilllib.load_skill(path, root=tmp_path)
    skill.record_use()
    skill.record_use()
    again = skilllib.load_skill(path, root=tmp_path)
    assert again.usage()["uses"] == 2

    report = skilllib.curator_report(workspace=None, roots=[tmp_path], stale_days=-1)
    assert report["total"] == 1
    assert report["stale"], report
    archived = skilllib.archive_stale(workspace=None, roots=[tmp_path], stale_days=-1)
    assert archived == ["ship-it"]
    assert (tmp_path / ".archive" / "ship-it" / skilllib.SKILL_FILE).exists()
    assert not path.exists()


def test_delete_skill(tmp_path):
    _make(tmp_path)
    assert skilllib.delete_skill("ship-it", root=tmp_path)
    assert not (tmp_path / "learned" / "ship-it").exists()
    assert not skilllib.delete_skill("ship-it", root=tmp_path)


HERMES_TREE_SKILL = """---
name: hermes-imported
description: Imported from a Hermes skills tree.
version: 1.2.0
author: Nous Research
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [portable, skills]
    category: automation
---

# Hermes Imported Skill

## When to Use
You want to prove imports work.

## Procedure
1. read references/notes.md
"""


def test_import_tree_copies_bodies_and_support_files(tmp_path):
    source = tmp_path / "upstream" / "automation" / "hermes-imported"
    (source / "references").mkdir(parents=True)
    (source / "references" / "notes.md").write_text("deep notes", encoding="utf-8")
    (source / skilllib.SKILL_FILE).write_text(HERMES_TREE_SKILL, encoding="utf-8")
    broken = tmp_path / "upstream" / "empty"
    broken.mkdir()

    dest = tmp_path / "dest"
    count, notes = skilllib.import_tree(tmp_path / "upstream", dest_root=dest, category_prefix="hermes")
    assert count == 1
    imported = dest / "hermes-automation" / "hermes-imported"
    assert (imported / skilllib.SKILL_FILE).exists()
    assert (imported / "references" / "notes.md").read_text(encoding="utf-8") == "deep notes"
    skill = skilllib.load_skill(imported / skilllib.SKILL_FILE, root=dest, source="hermes")
    assert skill.name == "hermes-imported"
    assert skill.provenance.get("imported_from"), skill.provenance
    assert any("empty" in note for note in notes) or notes == []

    # a second import without force keeps the existing copy
    count2, _ = skilllib.import_tree(tmp_path / "upstream", dest_root=dest, category_prefix="hermes")
    assert count2 == 0


def test_bundled_skills_ship_with_the_repo():
    bundled = skilllib.bundled_skills_root()
    assert bundled.is_dir(), bundled
    names = {p.parent.name for p in bundled.rglob(skilllib.SKILL_FILE)}
    assert {"add-a-role", "add-an-agent", "verify-before-commit"} <= names
