"""Long-term memory: MEMORY.md (agent notes) + USER.md (who the user is).

Same layout and delimiters as Hermes (`~/.hermes/memories/*.md`, entries split
by ``§``), so an existing Hermes memory directory can be imported directly. The
files live in ``~/.config/nexus/memories`` and are injected into the system
prompt as a frozen snapshot at the start of a run.

* ``MEMORY.md`` - facts, decisions, gotchas the agent must remember (2200 chars)
* ``USER.md``   - stable preferences about the human (1375 chars)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DELIMITER = "\n§\n"
MEMORY_LIMIT = 2200
USER_LIMIT = 1375


def memories_dir() -> Path:
    from .config import nexus_home

    return nexus_home() / "memories"


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class MemoryBlock:
    name: str
    path: Path
    limit: int
    entries: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return DELIMITER.join(self.entries)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def full(self) -> bool:
        return self.chars >= self.limit

    def render(self) -> str:
        if not self.entries:
            return ""
        return "\n".join(f"- {entry}" for entry in self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": str(self.path), "chars": self.chars,
                "limit": self.limit, "entries": list(self.entries)}


def _clean(entry: str) -> str:
    text = " ".join(str(entry).split()).strip(" -")
    return text[:600]


class MemoryStore:
    """Two-file curated memory with hard character caps."""

    def __init__(self, directory: Path | str | None = None,
                 *, memory_limit: int = MEMORY_LIMIT, user_limit: int = USER_LIMIT,
                 enabled: bool = True):
        self.dir = Path(directory) if directory else memories_dir()
        self.memory = MemoryBlock("memory", self.dir / "MEMORY.md", memory_limit)
        self.user = MemoryBlock("user", self.dir / "USER.md", user_limit)
        self.enabled = enabled
        if enabled:
            self.reload()

    # -- io -------------------------------------------------------------- #
    def block(self, section: str) -> MemoryBlock:
        section = (section or "memory").strip().lower()
        if section in {"user", "profile", "user_profile"}:
            return self.user
        return self.memory

    def reload(self) -> None:
        for block in (self.memory, self.user):
            try:
                raw = block.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                block.entries = []
                continue
            block.entries = [e.strip() for e in raw.split("§") if e.strip()]

    def _save(self, block: MemoryBlock) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        block.path.write_text(block.text, encoding="utf-8")

    # -- api ------------------------------------------------------------- #
    def add(self, text: str, section: str = "memory") -> tuple[bool, str]:
        if not self.enabled:
            return False, "memory is disabled (NEXUS_MEMORY=0)"
        block = self.block(section)
        entry = _clean(text)
        if not entry:
            return False, "nothing to remember"
        if entry in block.entries:
            return False, f"already in {block.name}"
        candidate_len = len(block.text) + len(entry) + (len(DELIMITER) if block.entries else 0)
        if candidate_len > block.limit:
            return False, (
                f"{block.name} is full ({block.chars}/{block.limit} chars). Remove an entry first - "
                "consolidate instead of appending."
            )
        block.entries.append(entry)
        self._save(block)
        return True, f"saved to {block.name} ({block.chars}/{block.limit} chars)"

    def remove(self, needle: str, section: str = "memory") -> tuple[bool, str]:
        block = self.block(section)
        needle = needle.strip().lower()
        if not needle:
            return False, "nothing to remove"
        for index, entry in enumerate(block.entries):
            if needle in entry.lower():
                del block.entries[index]
                self._save(block)
                return True, f"removed from {block.name}"
        return False, f"no {block.name} entry matches '{needle}'"

    def entries(self, section: str = "memory") -> list[str]:
        return list(self.block(section).entries)

    def stats(self) -> dict[str, Any]:
        return {"memory": self.memory.to_dict(), "user": self.user.to_dict(),
                "enabled": self.enabled}

    def prompt_sections(self) -> str:
        parts: list[str] = []
        if self.memory.entries:
            parts.append("# Memory (MEMORY.md - decisions and gotchas from earlier sessions)\n"
                         + self.memory.render())
        if self.user.entries:
            parts.append("# User profile (USER.md - who you are working with)\n" + self.user.render())
        return "\n\n".join(parts)

    # -- migration ------------------------------------------------------- #
    def import_hermes(self, directory: Path | str | None = None) -> tuple[int, list[str]]:
        """Import MEMORY.md/USER.md from a Hermes home (or any directory)."""
        source = Path(directory) if directory else Path.home() / ".hermes" / "memories"
        imported = 0
        notes: list[str] = []
        if not source.is_dir():
            return 0, [f"{source} is not a directory"]
        for name, block in (("MEMORY.md", self.memory), ("USER.md", self.user)):
            path = source / name
            if not path.is_file():
                continue
            incoming = [e.strip() for e in path.read_text(encoding="utf-8", errors="replace").split("§") if e.strip()]
            for entry in incoming:
                if entry in block.entries:
                    continue
                if block.chars + len(entry) > block.limit:
                    notes.append(f"{block.name}: stopped at the character cap")
                    break
                block.entries.append(entry)
                imported += 1
            self._save(block)
        return imported, notes


def memory_snapshot() -> MemoryStore:
    """Fresh store for contexts without a live agent (CLI commands, server)."""
    return MemoryStore(enabled=env_flag("NEXUS_MEMORY", True))
