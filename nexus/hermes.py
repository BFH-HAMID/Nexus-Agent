"""Bridge to Nous Research's `hermes-agent <https://github.com/NousResearch/hermes-agent>`_.

Nexus does not fork Hermes - it borrows what Hermes is best at:

* its **skills library** (``SKILL.md``, agentskills.io compatible) is imported
  into Nexus' own skills roots, so every Hermes skill becomes procedural
  memory for the Nexus team;
* its **memories** (``MEMORY.md`` / ``USER.md``) can be merged into Nexus'
  memory store;
* an existing Hermes install can still be driven directly (``nexus hermes run``)
  for the capabilities Nexus does not have (gateway, TUI, desktop).
* Nexus can **update itself** from its own git checkout (``nexus update``).

Everything here is best effort: no network call happens unless you ask for
``install``/``update``/``sync``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import skills as skilllib

HERMES_REPO = "https://github.com/NousResearch/hermes-agent.git"
HERMES_UPSTREAM = "https://github.com/NousResearch/hermes-agent"
HERMES_CHECKOUT_ENV = "NEXUS_HERMES_CHECKOUT"


def checkout_dir() -> Path:
    override = os.environ.get(HERMES_CHECKOUT_ENV)
    if override:
        return Path(override).expanduser()
    from .config import nexus_home

    return nexus_home() / "hermes-agent"


def find_cli() -> str | None:
    return shutil.which("hermes")


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 300) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd)} timed out after {timeout}s"


def status() -> dict[str, Any]:
    checkout = checkout_dir()
    cli = find_cli()
    imported = [s for s in skilllib.discover() if s.source in {"hermes", "local"} and s.provenance.get("imported_from")]
    return {
        "repo": HERMES_UPSTREAM,
        "checkout": str(checkout),
        "checkout_present": (checkout / ".git").exists() or (checkout / "skills").is_dir(),
        "cli": cli or "",
        "cli_present": bool(cli),
        "skills_dir": str(checkout / "skills"),
        "skills_available": len(list((checkout / "skills").rglob(skilllib.SKILL_FILE)))
        if (checkout / "skills").is_dir() else 0,
        "imported_skills": len(imported),
        "memories": str(Path.home() / ".hermes" / "memories"),
    }


def ensure_checkout(*, refresh: bool = False, log: Any = None) -> tuple[Path | None, str]:
    """Clone (once) or fast-forward the upstream checkout."""
    log = log or (lambda _msg: None)
    dest = checkout_dir()
    if (dest / ".git").exists():
        if refresh:
            log(f"updating {dest}")
            code, out = _run(["git", "-C", str(dest), "pull", "--ff-only"], timeout=180)
            if code != 0:
                return None, out.strip() or "git pull failed"
        return dest, ""
    if dest.exists() and any(dest.iterdir()):
        return dest, "directory exists but is not a git checkout"
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"cloning {HERMES_REPO} → {dest} (shallow)")
    code, out = _run(["git", "clone", "--depth", "1", HERMES_REPO, str(dest)], timeout=600)
    if code != 0:
        return None, out.strip() or "git clone failed"
    return dest, ""


def import_skills(*, refresh: bool = True, include_optional: bool = True,
                  force: bool = False, dest_root: Path | None = None,
                  log: Any = None) -> tuple[int, list[str]]:
    """Import every upstream SKILL.md into the Nexus skills library."""
    log = log or (lambda _msg: None)
    checkout, error = ensure_checkout(refresh=refresh, log=log)
    if checkout is None:
        return 0, [f"could not fetch hermes-agent: {error}"]
    notes: list[str] = []
    imported = 0
    for relative, prefix in (("skills", "hermes"), ("optional-skills", "hermes-optional")):
        source = checkout / relative
        if not source.is_dir():
            continue
        if relative == "optional-skills" and not include_optional:
            continue
        count, extra = skilllib.import_tree(
            source, dest_root=dest_root, category_prefix=prefix, force=force, source_label="hermes"
        )
        imported += count
        notes.extend(extra[:20])
        notes.append(f"{relative}: {count} imported")
    return imported, notes


def import_memories(directory: Path | str | None = None) -> tuple[int, list[str]]:
    from .memory import MemoryStore

    store = MemoryStore()
    return store.import_hermes(directory)


def run_cli(argv: list[str] | None = None) -> int:
    """Hand a task to an installed ``hermes`` binary (all of its features)."""
    cli = find_cli()
    if not cli:
        print(
            "hermes is not installed.\n"
            "  install:  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash\n"
            "  or just import its skills without installing:  nexus hermes install"
        )
        return 1
    cmd = [cli, *(argv or [])]
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 130


# --------------------------------------------------------------------------- #
# self-update
# --------------------------------------------------------------------------- #


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def self_update(*, pull: bool = True, reinstall: bool = True, log: Any = None) -> tuple[bool, list[str]]:
    """``nexus update``: fast-forward this checkout and reinstall the package."""
    log = log or (lambda _msg: None)
    root = repo_root()
    messages: list[str] = []
    if not (root / ".git").exists():
        return False, [f"{root} is not a git checkout - update with pip instead (`pip install -U nexus-agent`)"]
    if pull:
        log("git pull --ff-only")
        code, out = _run(["git", "-C", str(root), "pull", "--ff-only"], timeout=180)
        if code != 0:
            return False, [out.strip() or "git pull failed (dirty tree or diverged branch)"]
        messages.append(out.strip().splitlines()[-1] if out.strip() else "already up to date")
    if reinstall:
        log("pip install -e .")
        code, out = _run([sys.executable, "-m", "pip", "install", "-q", "-e", str(root)], timeout=600)
        if code != 0:
            return False, [*messages, out.strip()[-600:] or "pip install failed"]
        messages.append("reinstalled the package")
    messages.append("run `nexus doctor` to confirm")
    return True, messages
