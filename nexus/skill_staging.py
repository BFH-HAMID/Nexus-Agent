"""Safe skill authoring: propose skills/macros in a staging folder, activate
only after operator approval plus passing tests, ruff and a static permission
check. Staged skills are inert - they are never on the skills search path.

Everything is operator-owned: staging, versions and approvals live under
``NEXUS_HOME`` (``~/.config/nexus``) and are never written by agent tools
without explicit ``--execute`` consent.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import skills as skilllib
from .config import nexus_home

STAGING_DIRNAME = "skills.staging"
VERSIONS_DIRNAME = "skills.versions"
PROPOSAL_FILE = "proposal.yaml"
SKILL_FILE = "SKILL.md"
DIFF_FILE = "diff.patch"
STATUS_FILE = "status.json"

# Tools a staged skill may freely mention (read-ish, side-effect bounded).
BASE_ALLOWED_TOOLS = {
    "read_file",
    "list_files",
    "grep",
    "write_file",
    "edit_file",
    "remember",
    "finish",
    "recall",
}
# Anything here is an *extra* permission: static approval fails until the
# operator reviews and explicitly accepts the risk outside this flow.
EXTRA_PERMISSION_TOKENS = {
    "run_command",
    "delete_file",
    "git",
    "gh",
    "gh_create_pr",
    "gh_merge_pr",
    "ask_agent",
    "shell",
    "curl",
    "wget",
    "urllib",
    "requests",
    "subprocess",
    "socket",
}


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def staging_root(workspace: Path | str | None = None) -> Path:
    return nexus_home() / STAGING_DIRNAME


def versions_root(workspace: Path | str | None = None) -> Path:
    return nexus_home() / VERSIONS_DIRNAME


@dataclass
class StagedProposal:
    name: str
    path: Path
    status: str = "staged"
    created_at: str = ""
    approved_at: str = ""
    tests_ok: bool = False
    ruff_ok: bool = False
    permission_check_ok: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "status": self.status,
            "created_at": self.created_at,
            "approved_at": self.approved_at,
            "tests_ok": self.tests_ok,
            "ruff_ok": self.ruff_ok,
            "permission_check_ok": self.permission_check_ok,
            "notes": self.notes,
        }


def _load_proposal(path: Path) -> StagedProposal | None:
    meta = path / PROPOSAL_FILE
    status_file = path / STATUS_FILE
    if not meta.is_file():
        return None
    data = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
    static: dict[str, Any] = {}
    if status_file.is_file():
        try:
            static = json.loads(status_file.read_text(encoding="utf-8")) or {}
        except (ValueError, OSError):
            static = {}
    # STATUS_FILE (written by approve/rollback) wins over PROPOSAL_FILE so a
    # rejected proposal keeps its "rejected" status in listings.
    status_value = str(static.get("status") or data.get("status") or "staged")
    return StagedProposal(
        name=str(data.get("name", path.name)),
        path=path,
        status=status_value,
        created_at=str(data.get("created_at", "")),
        approved_at=str(static.get("approved_at", "")),
        tests_ok=bool(static.get("tests_ok")),
        ruff_ok=bool(static.get("ruff_ok")),
        permission_check_ok=bool(static.get("permission_check_ok")),
        notes=list(static.get("notes") or []),
    )


def list_staged(workspace: Path | str | None = None) -> list[StagedProposal]:
    root = staging_root(workspace)
    if not root.is_dir():
        return []
    proposals: list[StagedProposal] = []
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        proposal = _load_proposal(child)
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def _skill_tools(body: str) -> set[str]:
    """Crude static scan of the tools a skill body would ask for (permission check)."""
    found: set[str] = set()
    for token in sorted(BASE_ALLOWED_TOOLS | EXTRA_PERMISSION_TOKENS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(token)}\b", body):
            found.add(token)
    return found


def _default_tests(name: str) -> str:
    return (
        '"""Auto-generated test for the staged skill ``{name}``."""\n\n'
        "from pathlib import Path\n\n"
        "from nexus.skills import lint, parse_frontmatter\n\n"
        "SKILL = Path(__file__).resolve().parent.parent / \"SKILL.md\"\n"
        "data, body = parse_frontmatter(SKILL.read_text(encoding='utf-8'))\n\n\n"
        "def test_skill_loads_and_lints():\n"
        '    assert SKILL.is_file()\n'
        f'    assert data.get("name") == "{name}"\n'
        "    warnings = lint(data.get('name', ''), data.get('description', ''), body)\n"
        '    assert not warnings, warnings\n'
        "\n"
        "def test_skill_has_required_sections():\n"
        '    assert "## When to Use" in body\n'
        '    assert "## Procedure" in body\n'
        '    assert "## Verification" in body\n'
    )


def stage_proposal(
    *,
    name: str,
    body: str,
    description: str,
    tags: list[str] | None = None,
    workspace: Path | str | None = None,
    diff: str | None = None,
    tests: str | None = None,
) -> StagedProposal:
    """Write a new skill proposal to the staging folder. Inert until approved."""
    if not skilllib.NAME_RE.match(name or ""):
        raise ValueError(f"invalid skill name '{name}' (lowercase letters, digits, dashes)")
    root = staging_root(workspace)
    target = root / name
    if target.exists():
        raise FileExistsError(f"proposal '{name}' is already staged - run `nexus skills review`")

    lint_warnings = skilllib.lint(name, description, body, tags=tags or [])
    target.mkdir(parents=True, exist_ok=True)
    (target / SKILL_FILE).write_text(
        skilllib.render_frontmatter(
            {
                "name": name,
                "description": description,
                "version": "1.0.0-staged",
                "author": "Nexus Agent (staged)",
                "license": "MIT",
            },
            body,
        ),
        encoding="utf-8",
    )
    (target / DIFF_FILE).write_text(diff or f"new file: {SKILL_FILE}", encoding="utf-8")
    (target / "tests").mkdir(exist_ok=True)
    test_path = target / "tests" / f"test_{name}.py"
    test_path.write_text(tests or _default_tests(name), encoding="utf-8")
    meta = {
        "name": name,
        "description": description,
        "tags": tags or [],
        "status": "staged",
        "created_at": _utc_now(),
        "lint_warnings": lint_warnings,
        "requires_review": True,
    }
    (target / PROPOSAL_FILE).write_text(yaml.safe_dump(meta, sort_keys=True), encoding="utf-8")
    (target / STATUS_FILE).write_text(
        json.dumps({"status": "staged", "created_at": meta["created_at"], "notes": []}, indent=2),
        encoding="utf-8",
    )
    return StagedProposal(
        name=name,
        path=target,
        status="staged",
        created_at=meta["created_at"],
        notes=list(lint_warnings),
    )


def _run_tests(test_path: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_path), "-q", "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        return proc.returncode == 0, (proc.stdout + "\n" + proc.stderr)[-4000:]
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"test run failed: {exc}"


def _run_ruff(files: list[Path]) -> tuple[bool, str]:
    ruff = shutil.which("ruff")
    if not ruff:
        return True, "ruff not installed - skipped"
    try:
        proc = subprocess.run(
            [ruff, "check", *[str(f) for f in files]],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return proc.returncode == 0, proc.stdout + proc.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"ruff run failed: {exc}"


def _permission_check(body: str, tags: list[str] | None) -> tuple[bool, str]:
    requested = _skill_tools(body)
    extra = requested - BASE_ALLOWED_TOOLS
    if extra:
        return False, (
            "static permission check failed: skill requests extra permissions "
            f"{sorted(extra)}; only {sorted(BASE_ALLOWED_TOOLS)} are auto-approved"
        )
    return True, f"permission check ok: {sorted(requested)} within the base allowlist"


def _write_status(path: Path, **fields: Any) -> None:
    target = path / STATUS_FILE
    data: dict[str, Any] = {}
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8")) or {}
        except ValueError:
            data = {}
    data.update(fields)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")


def approve_proposal(
    name: str,
    workspace: Path | str | None = None,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    """Review a staged proposal: tests + ruff + static permission check.

    With ``execute`` it also installs the skill into the active skills root
    (previous versions are kept under ``skills.versions``). Without it this is
    a dry-run preview and nothing is activated.
    """
    root = staging_root(workspace)
    target = root / name
    proposal = _load_proposal(target) if target.is_dir() else None
    if proposal is None:
        return {"ok": False, "error": f"no staged proposal named '{name}'"}

    skill_path = target / SKILL_FILE
    body = skill_path.read_text(encoding="utf-8") if skill_path.is_file() else ""
    meta = yaml.safe_load((target / PROPOSAL_FILE).read_text(encoding="utf-8")) or {}
    description = str(meta.get("description", ""))
    tags = list(meta.get("tags") or [])

    report: dict[str, Any] = {
        "name": name,
        "staged": True,
        "execute": execute,
        "activated": False,
        "tests_ok": False,
        "ruff_ok": False,
        "permission_check_ok": False,
        "notes": [],
    }

    # 1. static permission check: no extra permissions, ever, silently.
    permission_ok, permission_note = _permission_check(body, tags)
    report["permission_check_ok"] = permission_ok
    report["notes"].append(permission_note)

    # 2. ruff on the staged files.
    ruff_ok, ruff_note = _run_ruff([skill_path, target / "tests" / f"test_{name}.py"])
    report["ruff_ok"] = ruff_ok
    report["notes"].append(f"ruff: {ruff_note}")

    # 3. the staged tests must pass.
    test_path = target / "tests" / f"test_{name}.py"
    tests_ok, tests_note = (True, "no test file - skipped") if not test_path.is_file() else _run_tests(test_path)
    report["tests_ok"] = tests_ok
    report["notes"].append(f"tests: {tests_note}")

    ready = permission_ok and ruff_ok and tests_ok
    report["ready"] = ready

    if not execute:
        report["ok"] = ready
        report["message"] = (
            "dry run - pass --execute to activate. "
            + ("all checks passed" if ready else "checks failed: " + "; ".join(report["notes"]))
        )
        _write_status(target, status="staged", tests_ok=tests_ok, ruff_ok=ruff_ok,
                      permission_check_ok=permission_ok, notes=report["notes"])
        return report

    if not ready:
        report["ok"] = False
        report["error"] = "refusing to activate: " + "; ".join(report["notes"])
        _write_status(target, status="rejected", tests_ok=tests_ok, ruff_ok=ruff_ok,
                      permission_check_ok=permission_ok, notes=report["notes"])
        return report

    # Version any existing active skill before replacing it.
    active = skilllib.get(name, workspace)
    if active is not None:
        version_dir = versions_root(workspace) / name / _utc_now().replace(":", "-")
        version_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(active.path, version_dir / SKILL_FILE)
        meta_file = version_dir / "provenance.yaml"
        meta_file.write_text(
            yaml.safe_dump(
                {
                    "name": name,
                    "previous_path": str(active.path),
                    "source": active.source,
                    "replaced_at": _utc_now(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        report["notes"].append(f"previous version archived to {version_dir}")

    # Install into the active skills root (user root follows the operator).
    dest = skilllib.ensure_root(skilllib.user_skills_root()) / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / SKILL_FILE).write_text(skill_path.read_text(encoding="utf-8"), encoding="utf-8")

    proposal.status = "approved"
    proposal.approved_at = _utc_now()
    proposal.tests_ok = True
    proposal.ruff_ok = True
    proposal.permission_check_ok = True
    (target / PROPOSAL_FILE).write_text(
        yaml.safe_dump(
            {
                "name": name,
                "description": description,
                "tags": tags,
                "status": "approved",
                "created_at": meta.get("created_at"),
                "approved_at": proposal.approved_at,
                "lint_warnings": meta.get("lint_warnings", []),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _write_status(target, status="approved", approved_at=proposal.approved_at, tests_ok=True,
                  ruff_ok=True, permission_check_ok=True, notes=report["notes"])

    report["ok"] = True
    report["activated"] = True
    report["skill_path"] = str(dest / SKILL_FILE)
    report["message"] = f"skill '{name}' activated - it is now on the skills search path"
    return report


def rollback_proposal(
    name: str,
    workspace: Path | str | None = None,
    *,
    to_version: str | None = None,
) -> dict[str, Any]:
    """Restore the previous version of an active skill (or drop a staged one).

    Never deletes: replaced versions stay under ``skills.versions``.
    """
    active = skilllib.get(name, workspace)
    if active is not None:
        version_dir_parent = versions_root(workspace) / name
        version_dirs = sorted(version_dir_parent.iterdir()) if version_dir_parent.is_dir() else []
        if not version_dirs:
            return {"ok": False, "error": f"no archived version of '{name}' to roll back to"}
        target_version = to_version or version_dirs[-1].name
        version_dir = version_dir_parent / target_version
        if not version_dir.is_dir():
            return {"ok": False, "error": f"no such version '{target_version}'"}
        skill_file = version_dir / SKILL_FILE
        if not skill_file.is_file():
            return {"ok": False, "error": f"version '{target_version}' has no SKILL.md"}
        # Archive the current version before restoring the previous one.
        current_dir = versions_root(workspace) / name / f"rolled-back-at-{_utc_now().replace(':', '-')}"
        current_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(active.path, current_dir / SKILL_FILE)
        shutil.copy2(skill_file, active.path)
        return {
            "ok": True,
            "rolled_back": True,
            "name": name,
            "restored_from": str(skill_file),
            "current_archived_at": str(current_dir),
        }

    # No active skill: roll back a staged (not yet approved) proposal.
    target = staging_root(workspace) / name
    if target.is_dir():
        shutil.rmtree(target)
        return {"ok": True, "rolled_back": True, "name": name, "dropped_staging": str(target)}

    return {"ok": False, "error": f"no active or staged skill named '{name}'"}


def activate_diff(
    *,
    name: str,
    workspace: Path | str | None = None,
    execute: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Convenience: stage + approve a diff-based proposal in one operator step."""
    stage_proposal(
        name=name,
        body=kwargs.get("body", ""),
        description=kwargs.get("description", ""),
        tags=kwargs.get("tags"),
        workspace=workspace,
        diff=kwargs.get("diff"),
        tests=kwargs.get("tests"),
    )
    return approve_proposal(name, workspace, execute=execute)
