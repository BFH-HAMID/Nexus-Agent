"""Durable idempotency ledger preventing duplicate external side effects across crash/resume."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from filelock import FileLock

from .config import nexus_home
from .studio import redact

MAX_LEDGER_ENTRIES = 2000

_EXTERNAL_TOOLS = frozenset({
    "git_prepare",
    "gh_create_pr",
    "gh_merge_pr",
    "browser_action",
    "project_action",
})

_GIT_EXTERNAL_SUBS = frozenset({"push", "fetch", "pull", "clone"})
_GH_READONLY_PAIRS = frozenset({
    ("pr", "list"), ("pr", "view"), ("pr", "diff"), ("pr", "checks"), ("pr", "status"),
    ("issue", "list"), ("issue", "view"), ("issue", "status"),
    ("repo", "list"), ("repo", "view"),
    ("run", "list"), ("run", "view"),
    ("workflow", "list"), ("workflow", "view"),
    ("release", "list"), ("release", "view"),
    ("auth", "status"),
})


def _git_subcommand(args: dict[str, Any]) -> str:
    raw = args.get("args")
    if isinstance(raw, str):
        parts = raw.split()
    elif isinstance(raw, (list, tuple)):
        parts = [str(p) for p in raw]
    else:
        parts = []
    for part in parts:
        if not part.startswith("-"):
            return part.casefold()
    return ""


def _gh_pair(args: dict[str, Any]) -> tuple[str, str]:
    raw = args.get("args")
    if isinstance(raw, str):
        parts = [p for p in raw.split() if not p.startswith("-")]
    elif isinstance(raw, (list, tuple)):
        parts = [str(p) for p in raw if not str(p).startswith("-")]
    else:
        parts = []
    first = parts[0].casefold() if len(parts) >= 1 else ""
    second = parts[1].casefold() if len(parts) >= 2 else ""
    return first, second


def is_external_action(tool_name: str, args: dict[str, Any] | None = None) -> bool:
    """Return True when ``tool_name`` with ``args`` performs an external side effect."""
    args = args or {}
    name = (tool_name or "").strip()
    if name.startswith("mcp_"):
        return True
    if name == "git_prepare":
        return bool(args.get("push", False))
    if name == "git":
        return _git_subcommand(args) in _GIT_EXTERNAL_SUBS
    if name == "gh":
        pair = _gh_pair(args)
        if not pair[0]:
            return False
        return pair not in _GH_READONLY_PAIRS
    return name in _EXTERNAL_TOOLS


def canonical_args_digest(args: dict[str, Any] | None) -> str:
    payload = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_idempotency_key(
    scope: str,
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    explicit_key: str | None = None,
) -> str:
    """Compute a deterministic SHA-256 idempotency key inside ``scope``."""
    if explicit_key and isinstance(explicit_key, str) and explicit_key.strip():
        raw = f"{scope}:{tool_name}:explicit:{explicit_key.strip()}"
    else:
        raw = f"{scope}:{tool_name}:{canonical_args_digest(args)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class IdempotencyLedger:
    """Bounded FileLock-backed ledger under ``$NEXUS_HOME/idempotency/<ws-hash>/``."""

    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).resolve()
        ws_hash = hashlib.sha256(str(self.workspace).encode("utf-8")).hexdigest()[:32]
        self.dir = nexus_home().resolve() / "idempotency" / ws_hash
        self.path = self.dir / "ledger.json"
        self.lock_path = self.dir / "ledger.lock"

    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise ValueError("Unsafe idempotency ledger symlink")

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema": 1, "entries": {}}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("entries"), dict):
            return {"schema": 1, "entries": {}}
        return raw

    def _write(self, data: dict[str, Any]) -> None:
        self._ensure_dir()
        entries = data.get("entries", {})
        if len(entries) > MAX_LEDGER_ENTRIES:
            ordered = sorted(
                entries.items(),
                key=lambda kv: float(kv[1].get("completed_at", 0.0)),
                reverse=True,
            )[:MAX_LEDGER_ENTRIES]
            data["entries"] = dict(ordered)
        tmp = self.dir / f".ledger.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def lookup(self, key: str) -> dict[str, Any] | None:
        self._ensure_dir()
        with FileLock(str(self.lock_path), timeout=5):
            data = self._read()
            entry = data["entries"].get(key)
            return dict(entry) if isinstance(entry, dict) else None

    def record(
        self,
        key: str,
        *,
        scope: str,
        tool: str,
        result_preview: str,
        status: str = "completed",
    ) -> dict[str, Any]:
        self._ensure_dir()
        safe_preview = str(redact(result_preview))[:500]
        entry = {
            "key": key,
            "scope": str(scope)[:160],
            "tool": str(tool)[:80],
            "status": status,
            "result_preview": safe_preview,
            "completed_at": time.time(),
        }
        with FileLock(str(self.lock_path), timeout=5):
            data = self._read()
            data["entries"][key] = entry
            self._write(data)
        return dict(entry)

    def list_scope(self, scope_prefix: str) -> list[dict[str, Any]]:
        self._ensure_dir()
        with FileLock(str(self.lock_path), timeout=5):
            data = self._read()
            return [
                dict(entry)
                for entry in data["entries"].values()
                if isinstance(entry, dict) and str(entry.get("scope", "")).startswith(scope_prefix)
            ]
