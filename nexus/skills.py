"""Skills: the agent's procedural memory (Hermes / agentskills.io compatible).

A skill is a directory with a ``SKILL.md`` file: YAML frontmatter (``name``,
``description``, ``version``, ``author``, ``tags``, ``platforms`` …) plus a
markdown body with when-to-use / procedure / pitfalls / verification.

It is the exact format used by `Nous Research's hermes-agent
<https://github.com/NousResearch/hermes-agent>`_ and the open
`agentskills.io <https://agentskills.io>`_ standard, so skills can be imported
from Hermes and shared back.

Roots, lowest precedence first::

    ~/.config/nexus/skills                 # user + agent-created + imported
    <workspace>/.nexus/skills              # project skills (win on name clash)

Layout inside a root: ``[<category>/]<slug>/SKILL.md`` plus optional
``references/``, ``templates/`` and ``scripts/``.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SKILL_FILE = "SKILL.md"
DESCRIPTION_LIMIT = 60          # hardline in the Hermes authoring standards
INDEX_DESC_LIMIT = 60
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_USAGE_FILE = ".usage.json"


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #


def nexus_home() -> Path:
    from .config import nexus_home as _home

    return _home()


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
BUNDLED_SKILLS = PACKAGE_ROOT / "_config" / "skills"   # shipped with the wheel


def bundled_skills_root() -> Path:
    """Skills shipped with Nexus itself (portable, read-only-ish)."""
    return BUNDLED_SKILLS if BUNDLED_SKILLS.is_dir() else REPO_ROOT / "skills"


def user_skills_root() -> Path:
    return nexus_home() / "skills"


def project_skills_root(workspace: Path | str) -> Path:
    return Path(workspace).resolve() / ".nexus" / "skills"


def skill_roots(workspace: Path | str | None = None) -> list[Path]:
    """Lowest precedence first: bundled, user, project."""
    roots = [bundled_skills_root(), user_skills_root()]
    if workspace:
        roots.append(project_skills_root(workspace))
    return roots


def ensure_root(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------- #
# frontmatter
# --------------------------------------------------------------------------- #


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``---`` YAML frontmatter from the body (tolerant of CRLF/BOM)."""
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            end = index
            break
    if end is None:
        return {}, text
    raw = "\n".join(lines[1:end])
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return data, body


def render_frontmatter(data: dict[str, Any], body: str) -> str:
    payload = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100).strip()
    return f"---\n{payload}\n---\n\n{body.strip()}\n"


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #


@dataclass
class Skill:
    name: str
    description: str = ""
    path: Path | None = None
    category: str = ""
    tags: list[str] = field(default_factory=list)
    version: str = "1.0.0"
    author: str = ""
    license: str = ""
    platforms: list[str] = field(default_factory=list)
    body: str = ""
    source: str = "local"              # local | project | hermes | agent | hub
    provenance: dict[str, Any] = field(default_factory=dict)
    root: Path | None = None           # skills root this skill was found under

    @property
    def dir(self) -> Path | None:
        return self.path.parent if self.path else None

    @property
    def prompt_description(self) -> str:
        desc = " ".join(self.description.split())
        if len(desc) > INDEX_DESC_LIMIT:
            desc = desc[: INDEX_DESC_LIMIT - 1].rstrip() + "…"
        return desc

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "tags": self.tags,
            "version": self.version,
            "platforms": self.platforms,
            "source": self.source,
            "path": str(self.path) if self.path else "",
            "chars": len(self.body),
        }

    def one_line(self, limit: int = 74) -> str:
        desc = self.prompt_description
        return f"{self.name:<32} {self.category:<14} {desc[:limit]}"

    # -- usage ---------------------------------------------------------- #
    def usage(self) -> dict[str, Any]:
        root = self.root or (self.path.parents[1] if self.path else None)
        if root is None:
            return {}
        return load_usage(root).get(self.name, {})

    def record_use(self) -> None:
        if not self.path:
            return
        root = self.root or self.path.parents[1]
        data = load_usage(root)
        entry = data.setdefault(self.name, {"uses": 0})
        entry["uses"] = int(entry.get("uses", 0)) + 1
        entry["last_used"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        try:
            ensure_root(root)
            (root / _USAGE_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass


def load_usage(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / _USAGE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def _category_of(skill_path: Path, root: Path) -> str:
    try:
        rel = skill_path.parent.relative_to(root)
    except ValueError:
        return ""
    parts = rel.parts
    return parts[0] if len(parts) > 1 else ""


def load_skill(path: Path, *, root: Path | None = None, source: str = "local") -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    data, body = parse_frontmatter(text)
    name = str(data.get("name") or path.parent.name).strip()
    if not name:
        return None
    meta = data.get("metadata") or {}
    hermes_meta = meta.get("hermes") if isinstance(meta, dict) else {}
    if not isinstance(hermes_meta, dict):
        hermes_meta = {}
    tags = data.get("tags") or hermes_meta.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    platforms = data.get("platforms") or []
    if isinstance(platforms, str):
        platforms = [platforms]
    category = str(data.get("category") or hermes_meta.get("category") or "")
    if not category and root is not None:
        category = _category_of(path, root)
    provenance = data.get("metadata", {}).get("nexus", {}) if isinstance(meta, dict) else {}
    if isinstance(provenance, dict) and provenance.get("imported_from"):
        source = str(provenance["imported_from"])   # imported skills are read-only for the agent
    return Skill(
        name=name,
        description=str(data.get("description") or ""),
        path=path,
        category=category,
        tags=[str(t) for t in tags],
        version=str(data.get("version") or "1.0.0"),
        author=str(data.get("author") or ""),
        license=str(data.get("license") or ""),
        platforms=[str(p) for p in platforms],
        body=body,
        source=source,
        provenance=provenance if isinstance(provenance, dict) else {},
        root=root,
    )


def discover(workspace: Path | str | None = None,
             roots: Iterable[Path | str] | None = None) -> list[Skill]:
    """Every skill in every root. Later roots win when names collide."""
    found: dict[str, Skill] = {}
    search = [Path(r) for r in roots] if roots is not None else skill_roots(workspace)
    for root in search:
        if not root.is_dir():
            continue
        if roots is None and root == bundled_skills_root():
            source = "bundled"
        elif workspace and root == project_skills_root(workspace):
            source = "project"
        else:
            source = "local"
        for path in sorted(root.rglob(SKILL_FILE)):
            skill = load_skill(path, root=root, source=source)
            if skill is not None:
                found[skill.name] = skill
    return sorted(found.values(), key=lambda s: (s.category, s.name))


def get(name: str, workspace: Path | str | None = None,
        roots: Iterable[Path | str] | None = None) -> Skill | None:
    for skill in discover(workspace, roots=roots):
        if skill.name == name:
            return skill
    return None


# --------------------------------------------------------------------------- #
# selection (what the model sees for this task)
# --------------------------------------------------------------------------- #


def _terms(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 3}


def score(skill: Skill, task: str) -> float:
    haystack = " ".join([skill.name, skill.description, " ".join(skill.tags), skill.category]).lower()
    words = _terms(task)
    if not words:
        return 0.0
    hits = sum(1 for w in words if w in haystack)
    score_value = float(hits)
    if skill.name.lower() in task.lower():
        score_value += 6.0
    for tag in skill.tags:
        if tag.lower() in task.lower():
            score_value += 2.0
    return score_value


def select(skills: Iterable[Skill], task: str, limit: int = 8) -> list[Skill]:
    scored = [(score(s, task), s) for s in skills]
    scored = [(value, s) for value, s in scored if value > 0]
    scored.sort(key=lambda item: (-item[0], item[1].name))
    return [s for _value, s in scored[:limit]]


def render_index(skills: Iterable[Skill], *, limit: int = 60) -> str:
    rows = []
    for skill in list(skills)[:limit]:
        tag = f" [{', '.join(skill.tags[:3])}]" if skill.tags else ""
        rows.append(f"- {skill.name}{tag}: {skill.prompt_description}")
    return "\n".join(rows)


def render_prompt_section(selected: Iterable[Skill],
                          all_skills: Iterable[Skill] | None = None) -> str:
    """The skills block injected into the system prompt.

    Hermes-style: a compact index (names + <=60 char descriptions) so the cached
    prompt stays small; the full SKILL.md is loaded on demand with
    ``skill_view`` or by running ``/<skill-name>``.
    """
    selected = list(selected)
    all_skills = list(all_skills if all_skills is not None else selected)
    if not all_skills:
        return ""
    parts = ["# Skills (procedural memory)"]
    if selected:
        parts.append(
            "Skills that look relevant to this task - load one with the `skill_view` tool before "
            "you improvise the procedure:"
        )
        parts.append(render_index(selected, limit=12))
    else:
        parts.append("No skill matched this task yet. The full library:")
        parts.append(render_index(all_skills, limit=40))
    parts.append(
        "After finishing a non-trivial task that will repeat, save the reusable procedure with "
        "`skill_manage` (action=create). If an existing skill was wrong or incomplete, fix it "
        "(action=patch) instead of writing a new one."
    )
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# authoring / editing
# --------------------------------------------------------------------------- #


def lint(name: str, description: str, body: str, *, tags: Iterable[str] = ()) -> list[str]:
    warnings: list[str] = []
    if not NAME_RE.match(name or ""):
        warnings.append("name must be lowercase letters/digits/dashes (<=64 chars)")
    if not description:
        warnings.append("description is required")
    elif len(description) > DESCRIPTION_LIMIT:
        warnings.append(f"description is {len(description)} chars (max {DESCRIPTION_LIMIT})")
    if not description.rstrip().endswith((".", "!", "?")):
        warnings.append("description should be one sentence ending with a period")
    if not body.strip():
        warnings.append("body is empty")
    if body and not body.lstrip().startswith("#"):
        warnings.append("body should start with a markdown H1")
    if len(body.splitlines()) < 8:
        warnings.append("body is very short - add When to Use / Procedure / Verification")
    return warnings


def default_body(name: str, description: str) -> str:
    title = " ".join(part.capitalize() for part in re.split(r"[-_]", name))
    return f"""# {title} Skill

{description}

## When to Use

- (the trigger phrases that should load this skill)

## Procedure

1. (the exact steps, commands and file paths)
2. (verify the result before reporting)

## Pitfalls

- (what looks broken but is not, rate limits, platform quirks)

## Verification

- (one command or check that proves it worked)
"""


def create_skill(
    *,
    name: str,
    description: str,
    body: str = "",
    tags: Iterable[str] = (),
    category: str = "",
    workspace: Path | str | None = None,
    author: str = "Nexus Agent",
    provenance: dict[str, Any] | None = None,
    overwrite: bool = False,
    root: Path | str | None = None,
) -> Path:
    """Write ``<root>/[category/]<name>/SKILL.md`` and return its path.

    Learnt skills go to the user root (``~/.config/nexus/skills``) so they follow
    you between projects; ``category="project"`` keeps them inside the workspace.
    """
    if not NAME_RE.match(name or ""):
        raise ValueError(f"invalid skill name '{name}' (lowercase letters, digits, dashes)")
    if root is not None:
        base = ensure_root(Path(root))
    elif category == "project" and workspace:
        base = ensure_root(project_skills_root(workspace))
    else:
        base = ensure_root(user_skills_root())
    directory = base / category / name if category and category != "project" else base / name
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / SKILL_FILE
    if target.exists() and not overwrite:
        raise FileExistsError(f"skill '{name}' already exists at {target}")

    frontmatter: dict[str, Any] = {
        "name": name,
        "description": " ".join(description.split())[:DESCRIPTION_LIMIT],
        "version": "1.0.0",
        "author": author,
        "license": "MIT",
    }
    if category and category not in {"project"}:
        frontmatter["category"] = category
    tag_list = [str(t) for t in tags if str(t).strip()]
    meta: dict[str, Any] = {"hermes": {"tags": tag_list}} if tag_list else {}
    if provenance:
        meta["nexus"] = {
            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            **provenance,
        }
    if meta:
        frontmatter["metadata"] = meta
    target.write_text(render_frontmatter(frontmatter, body or default_body(name, description)),
                      encoding="utf-8")
    return target


def patch_skill(path: Path | str, *, body: str | None = None, description: str | None = None,
                tags: Iterable[str] | None = None) -> Path:
    target = Path(path)
    data, old_body = parse_frontmatter(target.read_text(encoding="utf-8", errors="replace"))
    if description is not None:
        data["description"] = " ".join(description.split())[:DESCRIPTION_LIMIT]
    if tags is not None:
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        hermes_meta = meta.get("hermes") if isinstance(meta.get("hermes"), dict) else {}
        hermes_meta["tags"] = [str(t) for t in tags]
        meta["hermes"] = hermes_meta
        data["metadata"] = meta
    new_body = body if body is not None else old_body
    target.write_text(render_frontmatter(data, new_body), encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# import (Hermes / agentskills.io)
# --------------------------------------------------------------------------- #


def import_tree(source: Path | str, *, dest_root: Path | None = None,
                category_prefix: str = "hermes", force: bool = False,
                source_label: str = "hermes") -> tuple[int, list[str]]:
    """Copy every ``SKILL.md`` tree under ``source`` into the Nexus skills dir.

    Support files (``references/``, ``scripts/``, ``templates/``) travel with
    the skill. Returns (imported, notes).
    """
    src = Path(source).expanduser().resolve()
    dest = ensure_root(Path(dest_root) if dest_root else user_skills_root())
    imported = 0
    notes: list[str] = []
    if not src.is_dir():
        return 0, [f"{src} is not a directory"]
    for skill_md in sorted(src.rglob(SKILL_FILE)):
        skill = load_skill(skill_md, root=src, source=source_label)
        if skill is None:
            continue
        rel_parent = skill_md.parent.relative_to(src)
        category = category_prefix
        if len(rel_parent.parts) > 1:
            category = f"{category_prefix}-{rel_parent.parts[0]}"
        target_dir = dest / category / skill.name
        if target_dir.exists() and not force:
            notes.append(f"skip {skill.name} (already installed)")
            continue
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_md.parent, target_dir)
        # record provenance without touching the body
        target_md = target_dir / SKILL_FILE
        data, body = parse_frontmatter(target_md.read_text(encoding="utf-8", errors="replace"))
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        meta["nexus"] = {
            "imported_from": source_label,
            "imported_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "upstream_path": str(rel_parent),
        }
        data["metadata"] = meta
        target_md.write_text(render_frontmatter(data, body), encoding="utf-8")
        imported += 1
    return imported, notes


def delete_skill(name: str, *, workspace: Path | str | None = None,
                 root: Path | str | None = None) -> bool:
    skill = get(name, workspace, roots=[root] if root is not None else None)
    if skill is None or skill.dir is None:
        return False
    shutil.rmtree(skill.dir, ignore_errors=True)
    return True


# --------------------------------------------------------------------------- #
# curator (deterministic maintenance - no LLM)
# --------------------------------------------------------------------------- #


def curator_report(workspace: Path | str | None = None, *, stale_days: int = 45,
                   roots: Iterable[Path | str] | None = None,
                   include_imported: bool = True) -> dict[str, Any]:
    """Deterministic maintenance view: what is stale, unused, or archived."""
    skills = discover(workspace, roots=roots)
    if not include_imported:
        skills = [s for s in skills if s.source != "hermes"]
    now = _dt.datetime.now(_dt.timezone.utc)
    stale: list[dict[str, Any]] = []
    unused: list[dict[str, Any]] = []
    for skill in skills:
        used = skill.usage()
        row = {
            "name": skill.name,
            "description": skill.prompt_description,
            "source": skill.source,
            "uses": int(used.get("uses", 0) or 0),
            "last_used": used.get("last_used"),
            "path": str(skill.path) if skill.path else "",
        }
        if not used:
            unused.append(row)
            continue
        try:
            when = _dt.datetime.fromisoformat(str(row["last_used"]))
        except (TypeError, ValueError):
            unused.append(row)
            continue
        if (now - when).days > stale_days:
            stale.append(row)
    archived = len(list((user_skills_root() / ".archive").rglob(SKILL_FILE)))         if (user_skills_root() / ".archive").is_dir() else 0
    return {
        "total": len(skills),
        "by_source": {src: sum(1 for s in skills if s.source == src)
                      for src in sorted({s.source for s in skills})},
        "unused": unused,
        "stale": stale,
        "archived": archived,
        "stale_days": stale_days,
    }


def archive_stale(workspace: Path | str | None = None, *, stale_days: int = 45,
                  roots: Iterable[Path | str] | None = None) -> list[str]:
    """Move stale *local* skills into ``<root>/.archive`` (never delete)."""
    report = curator_report(workspace, stale_days=stale_days, roots=roots)
    moved: list[str] = []
    for row in report["stale"]:
        skill = get(row["name"], workspace, roots=roots)
        if skill is None or skill.dir is None or skill.source in {"project", "bundled", "hermes"}:
            continue
        root = skill.root or skill.dir.parent
        archive = root / ".archive"
        target = archive / skill.name
        archive.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        try:
            shutil.move(str(skill.dir), str(target))
            moved.append(skill.name)
        except OSError:
            continue
    return moved


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
