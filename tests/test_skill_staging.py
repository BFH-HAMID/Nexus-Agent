"""Staged skills are inert until approved; rollback restores the previous version."""

from pathlib import Path

from nexus import skills as skilllib
from nexus.skill_staging import approve_proposal, list_staged, rollback_proposal, stage_proposal

BODY = (
    "# Add A Json Flag Skill\n\n## When to Use\nYou need machine-readable output.\n\n"
    "## Procedure\n1. add the argument\n2. print json.dumps\n\n## Verification\n1. run it\n"
)


def _stage(tmp_path: Path, name: str = "json-flag"):
    return stage_proposal(
        name=name,
        body=BODY,
        description="Add a --json flag to a CLI command.",
        tags=["cli"],
        workspace=tmp_path,
    )


def test_staged_skill_is_inert_until_approved(tmp_path: Path):
    proposal = _stage(tmp_path)
    assert skilllib.get(proposal.name, tmp_path) is None  # not on the search path
    assert [p.name for p in list_staged(tmp_path)] == [proposal.name]

    # dry run: nothing activated
    result = approve_proposal(proposal.name, tmp_path, execute=False)
    assert result["activated"] is False and result["ready"] is True
    assert skilllib.get(proposal.name, tmp_path) is None  # still inert


def test_approve_activates_after_checks_pass(tmp_path: Path):
    proposal = _stage(tmp_path)
    result = approve_proposal(proposal.name, tmp_path, execute=True)
    assert result["ok"] is True and result["activated"] is True
    assert result["tests_ok"] and result["ruff_ok"] and result["permission_check_ok"]
    skill = skilllib.get(proposal.name, tmp_path)
    assert skill is not None and skill.description.startswith("Add a --json flag")
    assert Path(result["skill_path"]).is_file()


def test_extra_permissions_block_activation(tmp_path: Path):
    stage_proposal(
        name="shell-it",
        body=BODY + "\nrun_command('rm -rf /tmp/x')\n",
        description="Runs a shell command.",
        workspace=tmp_path,
    )
    result = approve_proposal("shell-it", tmp_path, execute=True)
    assert result["ok"] is False and result["activated"] is False
    assert not result["permission_check_ok"]
    assert skilllib.get("shell-it", tmp_path) is None  # still inert


def test_rejected_proposal_stays_staged_and_reviewable(tmp_path: Path):
    stage_proposal(
        name="shell-it-2",
        body="run_command('x')\n" + BODY,
        description="Bad skill.",
        workspace=tmp_path,
    )
    approve_proposal("shell-it-2", tmp_path, execute=True)  # rejected
    names = [p.name for p in list_staged(tmp_path)]
    assert "shell-it-2" in names
    status = next(p for p in list_staged(tmp_path) if p.name == "shell-it-2")
    assert status.status == "rejected"


def test_rollback_restores_previous_version(tmp_path: Path):
    skilllib.create_skill(
        name="keep-it", description="Keep it simple.",
        body="# Keep It Simple Skill\n\nv1 body\n",
    )
    # v2 staged and approved on top of v1 (user skills root, isolated NEXUS_HOME)
    stage_proposal(
        name="keep-it",
        body=BODY,
        description="Add a --json flag to a CLI command.",
        tags=["cli"],
        workspace=tmp_path,
    )
    result = approve_proposal("keep-it", tmp_path, execute=True)
    assert result["activated"] is True
    assert skilllib.get("keep-it", tmp_path).description.startswith("Add a --json flag")

    restored = rollback_proposal("keep-it", tmp_path)
    assert restored["ok"] is True and restored["rolled_back"] is True
    skill = skilllib.get("keep-it", tmp_path)
    assert skill is not None and "v1 body" in skill.body
    assert "Add a --json flag" not in skill.description


def test_rollback_of_unapproved_staging_drops_it(tmp_path: Path):
    _stage(tmp_path, name="drop-me")
    assert skilllib.get("drop-me", tmp_path) is None
    result = rollback_proposal("drop-me", tmp_path)
    assert result["ok"] is True and result.get("dropped_staging")
    assert [p.name for p in list_staged(tmp_path)] == []
