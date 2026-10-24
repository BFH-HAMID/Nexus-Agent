"""Import AI agents / skills from a GitHub repository into Nexus roles.

``nexus addagent <github-url>`` (or ``/addagent`` in the REPL) fetches a repo,
discovers agent definitions inside it, converts each one into a Nexus role and
writes a drop-in YAML pack under a ``roles.d/`` directory that the config loader
merges automatically. No code changes, exactly like adding a role by hand.

Three discovery strategies, tried together and de-duplicated:

* **skills** - ``**/SKILL.md`` / ``**/AGENT.md`` with YAML frontmatter
  (``name`` + ``description``) and a markdown body. This is the format used by
  obra/superpowers, affaan-m/ecc and morluto/rea.
* **personas** - any ``.md`` with ``name`` + ``description`` frontmatter whose
  body reads like a persona. This is msitarzewski/agency-agents
  (``<category>/<agent>.md``).
* **apps** - a directory under an ``*agent*`` / ``*skill*`` / ``rag`` path that
  holds a ``README.md`` plus code. This is shubhamsaboo/awesome-llm-apps, where
  every example app becomes a specialist role.

Everything is heuristic and offline-testable: ``discover_agents`` and
``convert`` operate on a local directory, so the suite never touches network.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .types import sanitize_role_name

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

MAX_PERSONA_CHARS = 6000
MAX_DESCRIPTION_CHARS = 400
MAX_KEYWORDS = 14

SKIP_FILENAMES = {
    "readme", "contributing", "license", "licence", "changelog", "code_of_conduct",
    "security", "authors", "codeowners", "issue_template", "pull_request_template",
    "gemini", "agents_md", "claude_md", "release-notes", "release_notes",
}
SKIP_DIR_SEGMENTS = {
    "node_modules", ".git", "dist", "build", "out", "vendor", "tests", "test",
    "__pycache__", ".venv", "venv", "references", "templates", "scripts",
    "assets", "media", "img", "images", ".github", "fixtures", "coverage",
    # docs/ trees are documentation (and often localized mirrors of skills/,
    # e.g. affaan-m/ecc docs/es/skills/*) - not distinct agents
    "docs", "doc", "website", "blog", "examples", "sample", "samples",
    # deprecated / migration leftovers are not live agents
    "legacy", "legacy-command-shims", "deprecated", "archived", "archive",
    "shims", "old", "attic",
}
# directory names that signal "agents live here" (used by the app strategy and
# to boost persona discovery)
AGENT_DIR_RE = re.compile(r"(agents?|skills?|personas?|roles?|characters?|subagents?|rag)", re.I)
# course/tutorial containers: their numbered sub-dirs are lessons, not agents.
# Kept narrow on purpose so real collections like `rag_tutorials/` survive.
TUTORIAL_DIR_RE = re.compile(r"(crash[_-]?course|bootcamp|workshop|(^|[_-])lessons?([_-]|$))", re.I)
# structural dir names that are never a single agent (kills nested READMEs like
# `.../backend/agents/README.md` becoming a bogus role named "agents")
GENERIC_APP_SLUGS = {
    "agents", "agent", "skills", "skill", "app", "apps", "backend", "frontend",
    "client", "server", "src", "ui", "api", "main", "index", "core", "common",
    "shared", "utils", "lib", "libs", "components", "services", "models", "data",
    "public", "static", "demo", "demos", "example", "examples", "test", "tests",
    "docs", "assets", "media", "files", "root", "base", "local", "internal",
    "external", "misc", "other", "new", "old", "tmp", "temp", "build", "dist",
    "readme", "config", "configs", "scripts", "tools", "team", "teams",
}
# files that are canonical single-agent definitions
SKILL_FILENAMES = {"skill.md", "agent.md"}
CODE_EXT = {".py", ".ts", ".js", ".tsx", ".jsx", ".ipynb", ".go", ".rs", ".java", ".rb"}
# categories/names that should ride the uncensored local model, like the core
# pentesting/osint roles do
SECURITY_RE = re.compile(
    r"(pentest|pen-test|penetration|osint|malware|reverse-engineer|reverse engineer|"
    r"exploit|vulnerab|security|ctf|bug-bounty|bug bounty|red-team|red team|forensic|threat)",
    re.I,
)
STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "are", "that", "this", "from", "into",
    "use", "using", "when", "what", "how", "any", "all", "our", "its", "their", "them",
    "will", "can", "should", "must", "about", "over", "under", "between", "each", "every",
    "other", "some", "more", "most", "best", "new", "one", "two", "app", "apps", "agent",
    "agents", "ai", "llm", "llms", "gpt", "tool", "tools", "build", "builds", "building",
    "create", "creates", "creating", "make", "makes", "making", "get", "gets", "getting",
    "team", "teams", "system", "systems", "via", "per", "based", "powered", "free", "open",
    "source", "complete", "specialized", "expert", "experts", "proven", "deliverables",
    "personality", "processes", "fingertips", "md", "readme", "skill", "skills",
}


# --------------------------------------------------------------------------- #
# data classes
# --------------------------------------------------------------------------- #


@dataclass
class RawAgent:
    """One discovered agent, before it becomes a Nexus role."""

    slug: str
    title: str
    description: str
    persona: str
    category: str
    source: str                 # path inside the repo
    kind: str                   # "skill" | "persona" | "app"
    repo: str = ""

    def fingerprint(self) -> str:
        body = re.sub(r"\s+", " ", (self.persona or self.description or "")[:400]).strip().lower()
        return f"{self.slug}::{body[:200]}"


@dataclass
class ImportResult:
    repo: str
    url: str
    dest: Path | None
    roles: list[dict[str, Any]] = field(default_factory=list)
    discovered: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def imported(self) -> int:
        return len(self.roles)


class ImportError_(RuntimeError):
    """Raised for user-facing import problems (bad URL, empty repo, ...)."""


# --------------------------------------------------------------------------- #
# URL parsing + fetching
# --------------------------------------------------------------------------- #


def parse_github_url(url: str) -> tuple[str, str, str | None, str | None, str | None]:
    """Return ``(owner, repo, ref, subdir, single_file)`` from a GitHub URL.

    Accepts::

        owner/repo
        https://github.com/owner/repo
        https://github.com/owner/repo.git
        https://github.com/owner/repo/tree/<ref>[/<subdir>]
        https://github.com/owner/repo/blob/<ref>/<file>.md
        git@github.com:owner/repo.git
    """
    text = (url or "").strip()
    if not text:
        raise ImportError_("empty repository URL")

    ref: str | None = None
    subdir: str | None = None
    single: str | None = None

    # scp-style git@github.com:owner/repo.git
    m = re.match(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?/?$", text)
    if m:
        return m.group(1), m.group(2), None, None, None

    # bare owner/repo
    m = re.match(r"^([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", text)
    if m and "." not in m.group(1):
        return m.group(1), m.group(2), None, None, None

    if not re.match(r"^https?://", text):
        text = "https://" + text

    m = re.match(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/(.*))?$", text)
    if not m:
        raise ImportError_(f"not a GitHub URL: {url}")
    owner, repo, rest = m.group(1), m.group(2), (m.group(3) or "").strip("/")
    if rest:
        parts = rest.split("/")
        if parts[0] in {"tree", "blob", "edit"} and len(parts) >= 2:
            ref = parts[1]
            tail = parts[2:]
            if parts[0] == "blob" and tail:
                single = "/".join(tail)
            elif tail:
                subdir = "/".join(tail)
        else:
            subdir = rest
    return owner, repo, ref, subdir, single


def _run(cmd: Sequence[str], cwd: Path | None = None, timeout: float = 300.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            list(cmd), cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, text=True,
        )
        return proc.returncode, proc.stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return 1, str(exc)


def _download_tarball(owner: str, repo: str, ref: str | None, dest: Path) -> Path | None:
    """Fallback fetch via codeload.github.com (no git required)."""
    refs = [f"refs/heads/{ref}", ref, "refs/heads/main", "refs/heads/master"] if ref else [
        "refs/heads/main", "refs/heads/master", "HEAD"
    ]
    for candidate in dict.fromkeys(r for r in refs if r):
        url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/{candidate}"
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 (https, fixed host)
                data = resp.read()
        except Exception:  # noqa: BLE001
            continue
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                tar.extractall(dest)  # noqa: S202 (trusted source, sanitized below)
        except (tarfile.TarError, OSError):
            continue
        children = [p for p in dest.iterdir() if p.is_dir()]
        if len(children) == 1:
            return children[0]
        return dest
    return None


def fetch_repo(owner: str, repo: str, ref: str | None, workdir: Path, log: Any = None) -> Path:
    """Clone (shallow) into ``workdir`` and return the repo root path."""
    def _say(msg: str) -> None:
        if log:
            log(msg)

    clone_dir = workdir / repo
    if shutil.which("git"):
        cmd = ["git", "clone", "--depth", "1", "--quiet"]
        if ref:
            cmd += ["--branch", ref]
        cmd += [f"https://github.com/{owner}/{repo}.git", str(clone_dir)]
        _say(f"cloning {owner}/{repo}" + (f"@{ref}" if ref else "") + " ...")
        code, out = _run(cmd, timeout=600.0)
        if code == 0 and clone_dir.is_dir():
            return clone_dir
        _say(f"git clone failed ({out.strip()[:160]}); trying tarball")
    else:
        _say("git not found; trying tarball")

    got = _download_tarball(owner, repo, ref, workdir)
    if got is None:
        raise ImportError_(f"could not fetch {owner}/{repo} (git clone and tarball both failed)")
    return got


# --------------------------------------------------------------------------- #
# markdown / frontmatter helpers
# --------------------------------------------------------------------------- #


def read_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split leading ``---`` YAML frontmatter from the body."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in {"---", "..."}:
            end = i
            break
    if end is None:
        return {}, text
    raw = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:]).strip("\n")
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, body


def humanize(slug: str) -> str:
    """``academic-anthropologist`` -> ``Academic Anthropologist``."""
    text = re.sub(r"[-_]+", " ", slug or "").strip()
    text = re.sub(r"\s+", " ", text)
    words = [w if w.isupper() else w.capitalize() for w in text.split(" ")]
    return " ".join(words)


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"[ \t]+\n", "\n", (text or "").strip())
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0].rstrip() + "\n\n[... truncated on import]"
    return text.strip()


def make_keywords(*sources: str) -> list[str]:
    """Derive routing keywords from a name/title/description/body blob."""
    blob = " ".join(s for s in sources if s).lower()
    blob = re.sub(r"[^a-z0-9+#.\- ]+", " ", blob)
    tokens = [t for t in re.split(r"[\s]+", blob) if t]
    out: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> None:
        term = term.strip("-_.")
        if len(term) < 3 or term in STOPWORDS or term in seen:
            return
        if not re.search(r"[a-z0-9]", term):
            return
        seen.add(term)
        out.append(term)

    # multiword technical terms first (e.g. "machine learning", "next.js")
    for m in re.finditer(r"[a-z0-9+#]+(?:[.\-][a-z0-9+#]+)+", blob):
        _add(m.group(0))
    for tok in tokens:
        _add(tok)
    # a few bigrams for phrase matching (skip compounds already captured)
    joined = [t for t in tokens if t not in STOPWORDS and len(t) >= 3 and "-" not in t and "." not in t]
    for a, b in zip(joined, joined[1:], strict=False):
        if len(out) >= MAX_KEYWORDS:
            break
        if a == b:
            continue
        _add(f"{a} {b}")
    return out[:MAX_KEYWORDS]


# --------------------------------------------------------------------------- #
# discovery strategies
# --------------------------------------------------------------------------- #


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace(os.sep, "/")
    except ValueError:
        return path.name


def _skip(rel_parts: Sequence[str]) -> bool:
    return any(part.lower() in SKIP_DIR_SEGMENTS or part.startswith(".") and part not in {".agents"}
               for part in rel_parts[:-1])


def _category_for(rel: str, repo_slug: str) -> str:
    parts = [p for p in rel.split("/") if p]
    if len(parts) <= 1:
        return repo_slug
    # drop leading dot-dirs like ".agents" / ".claude"
    while len(parts) > 1 and parts[0].startswith("."):
        parts = parts[1:]
    if len(parts) <= 1:
        return repo_slug
    # category = the top grouping directory (skills/, agents/, academic/, ...)
    cat = parts[0]
    return sanitize_role_name(re.sub(r"[_\s]+", "-", cat)) or repo_slug


def _is_noise_file(rel: str) -> bool:
    stem = Path(rel).stem.lower().replace("-", "_")
    return stem in SKIP_FILENAMES


def discover_skills(root: Path, repo_slug: str) -> list[RawAgent]:
    """Strategy A: ``**/SKILL.md`` and ``**/AGENT.md``."""
    found: list[RawAgent] = []
    for path in root.rglob("*.md"):
        if path.name.lower() not in SKILL_FILENAMES:
            continue
        rel = _rel(root, path)
        parts = rel.split("/")
        if _skip(parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body = read_frontmatter(text)
        name = str(meta.get("name") or "").strip()
        desc = str(meta.get("description") or "").strip()
        slug_src = name or (parts[-2] if len(parts) >= 2 else path.stem)
        slug = sanitize_role_name(re.sub(r"[_\s]+", "-", slug_src.lower()))
        if not slug:
            continue
        persona = _clean(body or desc, MAX_PERSONA_CHARS)
        if not persona:
            continue
        found.append(
            RawAgent(
                slug=slug,
                title=name or humanize(slug),
                description=_clean(desc, MAX_DESCRIPTION_CHARS),
                persona=persona,
                category=_category_for(rel, repo_slug),
                source=rel,
                kind="skill",
                repo=repo_slug,
            )
        )
    return found


def discover_personas(root: Path, repo_slug: str) -> list[RawAgent]:
    """Strategy B: ``.md`` files with ``name`` + ``description`` frontmatter."""
    found: list[RawAgent] = []
    for path in root.rglob("*.md"):
        rel = _rel(root, path)
        parts = rel.split("/")
        if path.name.lower() in SKILL_FILENAMES or _skip(parts) or _is_noise_file(rel):
            continue
        # keep it cheap: only look where agents plausibly live
        if not any(AGENT_DIR_RE.search(p) for p in parts[:-1]) and len(parts) < 2:
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not head.startswith("---"):
            continue
        meta, body = read_frontmatter(head)
        name = str(meta.get("name") or meta.get("title") or "").strip()
        desc = str(meta.get("description") or "").strip()
        # Require an explicit name/title: a real agent declares one. This keeps
        # agency-agents personas and ecc agents/ but drops description-only
        # slash-command stubs (ecc commands/) that are not specialists.
        if not name or len(body) < 80:
            continue
        slug_src = path.stem if not name else re.sub(r"[_\s]+", "-", name.lower())
        # prefer the file stem when it is more specific (agency-agents style)
        stem_slug = sanitize_role_name(re.sub(r"[_\s]+", "-", path.stem.lower()))
        name_slug = sanitize_role_name(slug_src)
        slug = stem_slug if len(stem_slug) >= len(name_slug) else name_slug
        if not slug:
            continue
        persona = _clean(body, MAX_PERSONA_CHARS)
        found.append(
            RawAgent(
                slug=slug,
                title=name or humanize(path.stem),
                description=_clean(desc, MAX_DESCRIPTION_CHARS),
                persona=persona,
                category=_category_for(rel, repo_slug),
                source=rel,
                kind="persona",
                repo=repo_slug,
            )
        )
    return found


def discover_apps(root: Path, repo_slug: str) -> list[RawAgent]:
    """Strategy C: example-app directories (README + code) under agent paths."""
    found: list[RawAgent] = []
    for readme in root.rglob("README.md"):
        rel = _rel(root, readme)
        parts = rel.split("/")
        if _skip(parts) or len(parts) < 2:
            continue
        if not any(AGENT_DIR_RE.search(p) for p in parts[:-1]):
            continue
        # skip course/tutorial containers - their sub-dirs are lessons, not agents
        if any(TUTORIAL_DIR_RE.search(p) for p in parts[:-1]):
            continue
        app_dir = readme.parent
        try:
            siblings = [p.name for p in app_dir.iterdir()]
        except OSError:
            continue
        has_code = any(
            Path(s).suffix.lower() in CODE_EXT or s.lower() in {"requirements.txt", "package.json", "pyproject.toml"}
            for s in siblings
        )
        if not has_code:
            continue
        slug = sanitize_role_name(re.sub(r"[_\s]+", "-", app_dir.name.lower()))
        # drop structural dirs ("agents", "client") and numbered lessons ("1-starter-agent")
        if not slug or slug in GENERIC_APP_SLUGS or re.match(r"^\d", slug):
            continue
        try:
            text = readme.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _meta, body = read_frontmatter(text)
        # first heading + first paragraph as the description
        desc = ""
        m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = m.group(1).strip() if m else humanize(app_dir.name)
        para = re.split(r"\n\s*\n", re.sub(r"^#\s+.+$", "", body, count=1, flags=re.MULTILINE).strip())
        if para:
            desc = _clean(re.sub(r"[<*_`>]+", " ", para[0]), MAX_DESCRIPTION_CHARS)
        persona = _clean(
            f"You are an AI agent specialist: {title}.\n\n"
            f"Reference implementation from the `{repo_slug}` collection.\n\n"
            f"{desc}\n\n## Source\n`{rel}`",
            MAX_PERSONA_CHARS,
        )
        found.append(
            RawAgent(
                slug=slug,
                title=title,
                description=desc,
                persona=persona,
                category=_category_for(rel, repo_slug),
                source=rel,
                kind="app",
                repo=repo_slug,
            )
        )
    return found


def discover_agents(root: Path, repo_slug: str, subdir: str | None = None) -> list[RawAgent]:
    """Run every strategy under ``root`` (optionally only ``subdir``) and dedupe."""
    scan_root = root
    if subdir:
        candidate = root / subdir
        if candidate.is_dir():
            scan_root = candidate
        else:
            raise ImportError_(f"subdir '{subdir}' not found in {repo_slug}")

    raw: list[RawAgent] = []
    raw += discover_skills(scan_root, repo_slug)
    raw += discover_personas(scan_root, repo_slug)
    raw += discover_apps(scan_root, repo_slug)

    # de-duplicate: prefer the shortest source path (kills docs/ mirrors),
    # then skill > persona > app for the same fingerprint.
    kind_rank = {"skill": 0, "persona": 1, "app": 2}
    best: dict[str, RawAgent] = {}
    for agent in raw:
        key = agent.fingerprint()
        cur = best.get(key)
        if cur is None:
            best[key] = agent
            continue
        if (len(agent.source), kind_rank.get(agent.kind, 9)) < (len(cur.source), kind_rank.get(cur.kind, 9)):
            best[key] = agent
    # also collapse identical slugs (keep the richer persona)
    by_slug: dict[str, RawAgent] = {}
    for agent in sorted(best.values(), key=lambda a: (a.slug, -len(a.persona))):
        if agent.slug not in by_slug:
            by_slug[agent.slug] = agent
    return sorted(by_slug.values(), key=lambda a: (a.category, a.slug))


# --------------------------------------------------------------------------- #
# conversion to Nexus roles
# --------------------------------------------------------------------------- #


def _unique_name(slug: str, taken: set[str], repo_slug: str) -> str:
    if slug not in taken:
        return slug
    candidate = f"{slug}-{repo_slug}"
    if candidate not in taken:
        return candidate
    n = 2
    while f"{slug}-{n}" in taken:
        n += 1
    return f"{slug}-{n}"


def _agent_for(raw: RawAgent, forced: str | None) -> str:
    if forced:
        return forced
    if SECURITY_RE.search(raw.category) or SECURITY_RE.search(raw.slug) or SECURITY_RE.search(raw.title):
        return "openrouter-uncensored"
    return ""  # inherit default_agent


def convert(
    agents: Iterable[RawAgent],
    *,
    taken: set[str],
    repo_slug: str,
    forced_agent: str | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Turn discovered agents into role dicts. Returns ``(roles, skipped)``."""
    roles: list[dict[str, Any]] = []
    skipped = 0
    local_taken = set(taken)
    for raw in agents:
        if limit is not None and len(roles) >= limit:
            skipped += 1
            continue
        name = _unique_name(raw.slug, local_taken, repo_slug)
        local_taken.add(name)
        keywords = make_keywords(raw.slug, raw.title, raw.description)
        if not keywords:
            keywords = make_keywords(humanize(raw.slug))
        aliases: list[str] = []
        if raw.slug != name:
            aliases.append(raw.slug)
        title_alias = sanitize_role_name(re.sub(r"[_\s]+", "-", raw.title.lower()))
        if title_alias and title_alias != name and title_alias not in local_taken:
            aliases.append(title_alias)
        persona = raw.persona
        if raw.description and raw.description not in persona[: len(raw.description) + 40]:
            persona = f"{raw.description}\n\n{persona}".strip()
        roles.append(
            {
                "name": name,
                "title": raw.title or humanize(name),
                "category": raw.category or repo_slug,
                "agent": _agent_for(raw, forced_agent),
                "persona": _clean(persona, MAX_PERSONA_CHARS),
                "keywords": keywords,
                "aliases": aliases[:4],
                "source_repo": repo_slug,
                "source_path": raw.source,
                "imported_kind": raw.kind,
            }
        )
    return roles, skipped


# --------------------------------------------------------------------------- #
# YAML pack rendering
# --------------------------------------------------------------------------- #


class _LiteralStr(str):
    """A str that PyYAML dumps as a ``|`` literal block when multiline."""


def _str_representer(dumper: yaml.Dumper, data: str) -> Any:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


class _PackDumper(yaml.SafeDumper):
    pass


_PackDumper.add_representer(str, _str_representer)


def render_pack_yaml(repo_slug: str, url: str, roles: Sequence[dict[str, Any]]) -> str:
    header = (
        f"# Imported by `nexus addagent {url}`\n"
        f"# {len(roles)} agent(s) from {repo_slug} - generated {date.today().isoformat()}.\n"
        f"# Regenerate with: nexus addagent {url} --dest repo --force\n"
        f"# Edit freely; this file is just a roles.yaml layer.\n"
    )
    clean_roles: dict[str, Any] = {}
    for role in roles:
        entry: dict[str, Any] = {
            "title": role.get("title", ""),
            "category": role.get("category", repo_slug),
            "agent": role.get("agent", ""),
            "persona": role.get("persona", ""),
            "keywords": role.get("keywords", []),
        }
        if role.get("aliases"):
            entry["aliases"] = role["aliases"]
        clean_roles[role["name"]] = entry
    body = yaml.dump(
        {"roles": clean_roles},
        Dumper=_PackDumper,
        sort_keys=False,
        allow_unicode=True,
        width=100,
        default_flow_style=False,
    )
    return header + body


# --------------------------------------------------------------------------- #
# destinations
# --------------------------------------------------------------------------- #


def resolve_dest(kind: str, workspace: Path) -> Path:
    """Where a pack is written: ``project`` | ``global`` | ``repo``."""
    from .config import REPO_ROOT, nexus_home  # local import to avoid a cycle

    kind = (kind or "project").lower()
    if kind in {"global", "user", "home"}:
        return nexus_home() / "roles.d"
    if kind in {"repo", "committed", "ship"}:
        return REPO_ROOT / "roles.d"
    return Path(workspace) / ".nexus" / "roles.d"


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #


def import_repo(
    url: str,
    *,
    workspace: Path,
    dest_kind: str = "project",
    forced_agent: str | None = None,
    category_prefix: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    taken: set[str] | None = None,
    log: Any = None,
) -> ImportResult:
    """Fetch ``url``, discover agents, and write a ``roles.d`` pack.

    ``taken`` is the set of role names that already exist (core + other packs)
    so imported names never clobber a curated role.
    """
    def _say(msg: str) -> None:
        if log:
            log(msg)

    owner, repo, ref, subdir, single = parse_github_url(url)
    repo_slug = sanitize_role_name(re.sub(r"[_\s]+", "-", repo.lower())) or "imported"
    result = ImportResult(repo=repo_slug, url=url, dest=None, dry_run=dry_run)
    taken = set(taken or set())

    tmp = Path(tempfile.mkdtemp(prefix="nexus-addagent-"))
    try:
        if single:
            # a direct link to one .md file
            root = tmp
            fetched = _fetch_single(owner, repo, ref, single, tmp)
            agents = _agents_from_files([fetched], repo_slug, root)
        else:
            root = fetch_repo(owner, repo, ref, tmp, log=_say)
            agents = discover_agents(root, repo_slug, subdir)
        result.discovered = len(agents)
        _say(f"discovered {len(agents)} agent(s) in {owner}/{repo}")

        if category_prefix:
            prefix = sanitize_role_name(category_prefix)
            for a in agents:
                a.category = f"{prefix}-{a.category}" if a.category else prefix

        roles, skipped = convert(
            agents, taken=taken, repo_slug=repo_slug, forced_agent=forced_agent, limit=limit
        )
        result.roles = roles
        result.skipped = skipped
        if not roles:
            result.notes.append("no agents discovered - is this a repo of agent/skill definitions?")
            return result

        dest_dir = resolve_dest(dest_kind, workspace)
        pack_path = dest_dir / f"{repo_slug}.yaml"
        result.dest = pack_path
        if pack_path.exists() and not force and not dry_run:
            result.notes.append(f"{pack_path.name} already exists - re-run with --force to overwrite")
            return result

        text = render_pack_yaml(repo_slug, url, roles)
        if not dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            pack_path.write_text(text, encoding="utf-8")
            # a --force overwrite can reuse the same size; drop the parse cache so
            # the next load_config() (e.g. the REPL's reload) re-reads it fresh
            from .config import clear_yaml_cache

            clear_yaml_cache()
            _say(f"wrote {pack_path} ({len(roles)} roles)")
        result.notes.append(f"{len(roles)} role(s) -> {pack_path}")
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fetch_single(owner: str, repo: str, ref: str | None, path: str, tmp: Path) -> Path:
    """Fetch one file via the GitHub contents API (raw.* is not always reachable)."""
    branch = ref or "HEAD"
    api = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    if branch and branch != "HEAD":
        api += f"?ref={branch}"
    try:
        req = urllib.request.Request(api, headers={"Accept": "application/vnd.github.raw+json"})
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            data = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        raise ImportError_(f"could not fetch {path} from {owner}/{repo}: {exc}") from exc
    out = tmp / Path(path).name
    out.write_text(data, encoding="utf-8")
    return out


def _agents_from_files(files: Sequence[Path], repo_slug: str, root: Path) -> list[RawAgent]:
    agents: list[RawAgent] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body = read_frontmatter(text)
        name = str(meta.get("name") or path.stem).strip()
        desc = str(meta.get("description") or "").strip()
        slug = sanitize_role_name(re.sub(r"[_\s]+", "-", name.lower()))
        if not slug:
            continue
        persona = _clean(body or desc, MAX_PERSONA_CHARS)
        if not persona:
            continue
        agents.append(
            RawAgent(
                slug=slug, title=name, description=_clean(desc, MAX_DESCRIPTION_CHARS),
                persona=persona, category=repo_slug, source=path.name, kind="skill", repo=repo_slug,
            )
        )
    return agents


def existing_role_names(workspace: Path) -> set[str]:
    """Names already defined (core roles.yaml + every roles.d pack) so imports
    never clobber a curated role."""
    from .config import load_config

    names: set[str] = set()
    try:
        cfg = load_config(workspace)
        names.update(cfg.roles.names())
        for role in cfg.roles.roles.values():
            names.update(role.aliases)
    except Exception:  # noqa: BLE001
        pass
    return names
