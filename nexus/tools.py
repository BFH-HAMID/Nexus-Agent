"""Tool layer: files, terminal, git, GitHub, per-role memory, agent-to-agent.

Design notes
------------
* Every tool is a small callable with a JSON-schema spec, so it can be sent to
  both the Claude Messages API and an OpenAI-compatible endpoint.
* Nothing mutates the disk without going through the **approval gate**
  (:class:`ApprovalGate`). Read-only tools never prompt.
* All paths are resolved against the workspace and escapes are rejected unless
  the user explicitly allows absolute paths.
"""

from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .types import Event, EventHandler, EventType, Risk, ToolSpec

# --------------------------------------------------------------------------- #
# safety
# --------------------------------------------------------------------------- #

DEFAULT_BLOCKED_PATTERNS: list[str] = [
    r"\brm\s+(?:-[a-z]*\s+)*-[a-z]*[rf][a-z]*\s+(?:-[a-z]+\s+)*(?:/|~|\$HOME)",  # rm -rf <absolute path>
    r"\bsudo\s+rm\b",
    r"\bmkfs(\.|$|\s)",
    r"\bdd\s+if=.*\bof=/dev/",
    r":\(\)\s*\{.*\};\s*:",           # fork bomb
    r"\bshutdown\b|\breboot\b|\bhalt\b",
    r"\bgit\s+push\s+.*(--force\b(?!-with-lease)|-f\b)",
    r"\bgit\s+reset\s+--hard\s+origin/",
    r"\bgit\s+clean\s+-[a-z]*f[a-z]*x?\s+(/|~)",
    r"\bchmod\s+-R\s+777\s+/",
    r">\s*/dev/sd[a-z]",
    r"\bcurl\b[^|]*\|\s*(ba)?sh",
    r"\bwget\b[^|]*\|\s*(ba)?sh",
    r"\bnpm\s+publish\b",
    r"\bgh\s+repo\s+delete\b",
    r"\bgit\s+push\s+.*--delete\b",
    r"\bdrop\s+database\b",
]

SENSITIVE_DIRS = {".git", ".svn", ".hg"}
DEFAULT_EXCLUDES = [
    ".git/*", ".venv/*", "venv/*", "node_modules/*", "__pycache__/*", ".mypy_cache/*",
    ".pytest_cache/*", ".ruff_cache/*", "dist/*", "build/*", "target/*", ".next/*",
    ".cache/*", "*.pyc", "*.pyo", ".DS_Store", "*.egg-info/*", ".tox/*", "coverage/*",
]


def find_blocked_pattern(command: str, extra_patterns: Sequence[str] = ()) -> str | None:
    """Return the first deny-list pattern the command matches (or None)."""
    lowered = command.lower()
    for pattern in [*DEFAULT_BLOCKED_PATTERNS, *extra_patterns]:
        try:
            if re.search(pattern, lowered):
                return pattern
        except re.error:
            if pattern in lowered:
                return pattern
    return None


class PathGuard:
    """Keeps file tools inside the workspace."""

    def __init__(self, workspace: Path, *, allow_outside: bool = False):
        self.workspace = Path(workspace).resolve()
        self.allow_outside = allow_outside

    def resolve(self, raw: str | os.PathLike[str], *, must_exist: bool = False) -> Path:
        candidate = Path(str(raw)).expanduser()
        path = candidate if candidate.is_absolute() else (self.workspace / candidate)
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise ToolFailure(f"cannot resolve path {raw!r}: {exc}") from exc
        if not self.allow_outside:
            try:
                resolved.relative_to(self.workspace)
            except ValueError as exc:
                raise ToolFailure(
                    f"{resolved} is outside the workspace ({self.workspace}). "
                    "Pass --allow-outside if you really mean it."
                ) from exc
        if must_exist and not resolved.exists():
            raise ToolFailure(f"no such file or directory: {resolved}")
        return resolved

    def rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)


class ToolFailure(Exception):
    """Raised by a tool; converted into an ``is_error`` tool result."""


# --------------------------------------------------------------------------- #
# approval gate
# --------------------------------------------------------------------------- #


@dataclass
class ApprovalDecision:
    approved: bool
    scope: str = "once"      # once | session | always | deny
    reason: str = ""


ApprovalCallback = Callable[[ToolSpec, dict[str, Any], Risk, str], ApprovalDecision]


class ApprovalGate:
    """Decides whether a tool call may run.

    Modes: ``ask`` (default, interactive y/N), ``auto`` (``--yes``),
    ``plan`` (read-only only, never mutate).
    """

    def __init__(
        self,
        *,
        mode: str = "ask",
        allow_dangerous: bool = False,
        always_allow: Sequence[str] = (),
        never_allow: Sequence[str] = (),
        extra_blocked_patterns: Sequence[str] = (),
        callback: ApprovalCallback | None = None,
        emit: EventHandler | None = None,
    ):
        self.mode = mode
        self.allow_dangerous = allow_dangerous
        self.always_allow = set(always_allow)
        self.never_allow = set(never_allow)
        self.extra_blocked_patterns = list(extra_blocked_patterns)
        self.session_allowed: set[str] = set()
        self.callback = callback
        self.emit = emit or (lambda _event: None)
        self.decisions: list[tuple[str, bool]] = []
        self.policy = None

    # ------------------------------------------------------------------ #
    def check(self, spec: ToolSpec, args: dict[str, Any]) -> ApprovalDecision:
        name = spec.name
        if self.policy:
            reason = self.policy(spec, args)
            if reason:
                return ApprovalDecision(False, reason=reason)

        if name in self.never_allow:
            return ApprovalDecision(False, reason=f"tool '{name}' is in never_allow")

        blocked = self._blocked_reason(spec, args)
        if blocked:
            if self.mode == "plan":
                return ApprovalDecision(False, reason=f"plan mode: {blocked}")
            if not self.allow_dangerous:
                return ApprovalDecision(False, reason=f"blocked command pattern: {blocked}")

        if spec.is_readonly_call(args):
            return ApprovalDecision(True, scope="auto", reason="read-only")

        if self.mode == "plan":
            return ApprovalDecision(False, reason="plan mode is read-only (use `nexus task` to apply changes)")

        if name in self.always_allow or name in self.session_allowed:
            return ApprovalDecision(True, scope="auto", reason="pre-approved")

        if self.mode == "auto" and (not blocked or self.allow_dangerous):
            return ApprovalDecision(True, scope="auto", reason="--yes")

        if self.callback is None:
            return ApprovalDecision(
                False,
                reason="no approval channel available (non-interactive run without --yes)",
            )

        preview = preview_args(spec, args)
        self.emit(Event(EventType.APPROVAL, {"tool": name, "args": args, "preview": preview,
                                             "risk": spec.risk.value, "blocked": bool(blocked)}))
        decision = self.callback(spec, args, spec.risk, preview)
        self.decisions.append((name, decision.approved))
        if decision.approved and decision.scope in {"session", "always"}:
            self.session_allowed.add(name)
        return decision

    def _blocked_reason(self, spec: ToolSpec, args: dict[str, Any]) -> str | None:
        haystacks: list[str] = []
        for key in ("command", "cmd", "script", "args", "message", "body", "url"):
            value = args.get(key)
            if isinstance(value, str):
                haystacks.append(value)
            elif isinstance(value, list):
                haystacks.extend(str(v) for v in value)
        if spec.name == "git":
            haystacks.append("git " + " ".join(str(a) for a in (args.get("args") or [])))
        for text in haystacks:
            hit = find_blocked_pattern(text, self.extra_blocked_patterns)
            if hit:
                return hit
        return None


def preview_args(spec: ToolSpec, args: dict[str, Any], limit: int = 400) -> str:
    """One-line human readable summary shown before y/N."""
    name = spec.name
    if name == "run_command":
        return str(args.get("command", ""))[:limit]
    if name == "write_file":
        content = str(args.get("content", ""))
        return f"{args.get('path')} ({len(content)} chars, {content.count(chr(10)) + 1} lines)"
    if name in {"read_file", "list_files", "delete_file"}:
        return str(args.get("path") or args.get("directory") or "")[:limit]
    if name == "edit_file":
        return f"{args.get('path')} :: {str(args.get('old_text', ''))[:60]!r} -> {str(args.get('new_text', ''))[:60]!r}"
    if name == "git":
        return "git " + " ".join(str(a) for a in (args.get("args") or []))[:limit]
    if name in {"gh_create_pr", "gh_merge_pr", "gh_pr", "gh_run"}:
        return json.dumps(args, ensure_ascii=False)[:limit]
    if name == "ask_agent":
        return f"{args.get('role')}: {str(args.get('question', ''))[:160]}"
    if name == "remember":
        return str(args.get("text", ""))[:limit]
    return json.dumps(args, ensure_ascii=False)[:limit]


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


@dataclass
class Tool:
    spec: ToolSpec
    handler: Callable[[dict[str, Any]], str]


@dataclass
class ToolContext:
    workspace: Path
    guard: PathGuard
    gate: ApprovalGate
    config: Any = None                      # NexusConfig (optional; for memory/ask_agent)
    providers: Any = None                   # ProviderRegistry (optional; for ask_agent)
    emit: EventHandler = field(default=lambda _e: None)
    cancel_event: threading.Event | None = None
    max_output_chars: int = 24_000
    max_read_chars: int = 120_000
    command_timeout: float = 120.0
    excludes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    ask_agent_depth: int = 0
    memory_store: Any = None                 # nexus.memory.MemoryStore (global notes/profile)
    sessions: Any = None                     # nexus.sessions.SessionStore (recall)
    state: dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    def __init__(self, ctx: ToolContext):
        self.ctx = ctx
        self._tools: dict[str, Tool] = {}

    # -- registration ------------------------------------------------------ #
    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[[dict[str, Any]], str],
        *,
        risk: Risk = Risk.WRITE,
        read_only: bool = False,
        readonly_subcommands: Sequence[str] = (),
    ) -> None:
        self._tools[name] = Tool(
            spec=ToolSpec(name=name, description=description, input_schema=input_schema,
                          risk=risk, read_only=read_only,
                          readonly_subcommands=frozenset(readonly_subcommands)),
            handler=handler,
        )

    def names(self) -> list[str]:
        return list(self._tools)

    def keep_only(self, allowed: set[str]) -> list[str]:
        """Hard-remove tools from the registry (not just from the schema list).

        Used for read-only/plan mode so a model that hallucinates a write gets
        `Unknown tool` instead of a modified disk.
        """
        dropped = [name for name in self._tools if name not in allowed]
        for name in dropped:
            del self._tools[name]
        return dropped

    def specs(self, only: Sequence[str] | None = None, drop: Sequence[str] = ()) -> list[ToolSpec]:
        selected = only if only else list(self._tools)
        return [self._tools[n].spec for n in selected if n in self._tools and n not in set(drop)]

    def has(self, name: str) -> bool:
        return name in self._tools

    # -- execution --------------------------------------------------------- #
    def _emit_failure(self, name: str, message: str) -> None:
        self.ctx.emit(Event(EventType.TOOL_CALL, {"tool": name, "args": {}, "preview": "", "risk": "write"}))
        self.ctx.emit(Event(EventType.TOOL_RESULT, {"tool": name, "is_error": True,
                                                    "preview": message, "chars": len(message)}))

    def call(self, name: str, args: dict[str, Any] | None) -> tuple[str, bool]:
        """Run one tool call. Returns (text, is_error)."""
        args = args or {}
        tool = self._tools.get(name)
        if tool is None:
            message = (
                f"Unknown tool '{name}'. Available: {', '.join(sorted(self._tools))}. "
                "Reply with prose if you are done."
            )
            self._emit_failure(name, message)
            return message, True
        if not isinstance(args, dict):
            message = f"arguments for '{name}' must be an object, got {type(args).__name__}"
            self._emit_failure(name, message)
            return message, True

        decision = self.ctx.gate.check(tool.spec, args)
        if not decision.approved:
            message = f"DENIED: {decision.reason or 'user rejected this call'}."
            if self.ctx.gate.mode == "plan":
                message += " You are in plan mode - describe the change instead of applying it."
            return message, True

        call_id = uuid.uuid4().hex
        self.ctx.state["active_call_id"] = call_id
        self.ctx.state["active_tool"] = name
        self.ctx.emit(Event(EventType.TOOL_CALL, {
            "call_id": call_id, "tool": name, "args": args, "preview": preview_args(tool.spec, args),
            "risk": tool.spec.risk.value,
        }))
        started = time.monotonic()
        result = ""
        is_error = False
        try:
            result = tool.handler(args)
        except ToolFailure as exc:
            result, is_error = str(exc), True
        except subprocess.TimeoutExpired as exc:
            result, is_error = f"command timed out after {exc.timeout}s", True
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            result, is_error = f"{exc.__class__.__name__}: {exc}", True
        result = clip(str(result if result is not None else ""), self.ctx.max_output_chars)
        self.ctx.emit(Event(EventType.TOOL_RESULT, {
            "call_id": call_id, "tool": name, "is_error": is_error, "preview": clip(result, 1200),
            "output": clip(result, 64_000), "output_truncated": len(result) > 64_000,
            "chars": len(result), "duration_ms": round((time.monotonic() - started) * 1000, 2),
        }))
        return result, is_error


def clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    keep = limit // 2
    return f"{text[:keep]}\n\n... [nexus clipped {len(text) - limit:,} chars] ...\n\n{text[-keep:]}"


# --------------------------------------------------------------------------- #
# subprocess helper
# --------------------------------------------------------------------------- #


def run_shell(
    command: str,
    cwd: Path,
    *,
    timeout: float = 120.0,
    env: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Run a command through the shell, return (output, returncode)."""
    full_env = dict(os.environ)
    full_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    full_env.setdefault("PAGER", "cat")
    if env:
        full_env.update(env)
    proc = subprocess.run(
        command,
        shell=True,
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    parts: list[str] = []
    if proc.stdout:
        parts.append(proc.stdout.rstrip())
    if proc.stderr:
        parts.append(f"[stderr]\n{proc.stderr.rstrip()}")
    parts.append(f"[exit {proc.returncode}]")
    return "\n".join(p for p in parts if p), proc.returncode


def run_git(args: Sequence[str], cwd: Path, *, timeout: float = 60.0) -> tuple[str, int]:
    quoted = " ".join(shlex.quote(str(a)) for a in args)
    return run_shell(f"git {quoted}", cwd, timeout=timeout)


# --------------------------------------------------------------------------- #
# file helpers
# --------------------------------------------------------------------------- #


def _is_excluded(rel: str, excludes: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel.split("/")[0], pat.split("/")[0])
               for pat in excludes)


def walk_files(root: Path, excludes: Sequence[str], limit: int) -> list[str]:
    out: list[str] = []
    stack = [root]
    while stack and len(out) < limit:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except (PermissionError, OSError):
            continue
        for entry in entries:
            rel = entry.relative_to(root).as_posix()
            if entry.name in SENSITIVE_DIRS or _is_excluded(rel, excludes):
                continue
            if entry.is_dir():
                stack.append(entry)
            else:
                out.append(rel)
                if len(out) >= limit:
                    break
    return out


def unified_diff_snippet(path: str, old: str, new: str, max_lines: int = 40) -> str:
    import difflib

    diff = list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="", n=2
        )
    )
    if not diff:
        return f"{path}: no change"
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"... (+{len(diff) - max_lines} more diff lines)"]
    return "\n".join(diff)


# --------------------------------------------------------------------------- #
# memory (Phase 7)
# --------------------------------------------------------------------------- #


class RoleMemory:
    """Per-role markdown notes under ``.nexus/memory/<role>.md``."""

    def __init__(self, workspace: Path, subdir: str = ".nexus/memory", *, max_chars: int = 4000,
                 enabled: bool = True):
        self.dir = Path(workspace) / subdir
        self.max_chars = max_chars
        self.enabled = enabled

    def path_for(self, role: str) -> Path:
        safe = re.sub(r"[^a-z0-9-]+", "-", role.lower()).strip("-") or "general"
        return self.dir / f"{safe}.md"

    def read(self, role: str) -> str:
        if not self.enabled:
            return ""
        path = self.path_for(role)
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if len(text) > self.max_chars:
            text = text[-self.max_chars :]
            text = "...(older memory truncated)\n" + text
        return text

    def append(self, role: str, note: str) -> Path:
        if not self.enabled:
            raise ToolFailure("memory is disabled (settings.memory_enabled=false)")
        note = note.strip()
        if not note:
            raise ToolFailure("nothing to remember")
        path = self.path_for(role)
        path.parent.mkdir(parents=True, exist_ok=True)
        import datetime as _dt

        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        existing = path.read_text(encoding="utf-8") if path.is_file() else f"# {role} memory\n\n"
        path.write_text(f"{existing.rstrip()}\n\n- [{stamp}] {note}\n", encoding="utf-8")
        return path

    def all_roles(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(p.stem for p in self.dir.glob("*.md"))


# --------------------------------------------------------------------------- #
# registry builder
# --------------------------------------------------------------------------- #


def build_tools(ctx: ToolContext, *, role: str = "general", include_ask_agent: bool = True) -> ToolRegistry:
    reg = ToolRegistry(ctx)

    # -- files ------------------------------------------------------------- #
    reg.register(
        "list_files",
        "List files/directories under a path (recursive, gitignored-ish excludes applied).",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "directory, default '.'"},
                "pattern": {"type": "string", "description": "glob filter, e.g. '*.py'"},
                "max_depth": {"type": "integer", "description": "1 = only this directory"},
                "limit": {"type": "integer", "description": "max entries (default 300)"},
            },
        },
        lambda a: tool_list_files(ctx, a),
        risk=Risk.READ,
        read_only=True,
    )

    reg.register(
        "read_file",
        "Read a text file (optionally a line range). Use this before editing anything.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
        lambda a: tool_read_file(ctx, a),
        risk=Risk.READ,
        read_only=True,
    )

    reg.register(
        "grep",
        "Search file contents with a regex. Returns matching lines with file:line.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "file or directory, default '.'"},
                "glob": {"type": "string", "description": "e.g. '*.py'"},
                "ignore_case": {"type": "boolean"},
                "max_results": {"type": "integer"},
            },
            "required": ["pattern"],
        },
        lambda a: tool_grep(ctx, a),
        risk=Risk.READ,
        read_only=True,
    )

    reg.register(
        "write_file",
        "Create or fully overwrite a file. Prefer edit_file for surgical changes.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "append": {"type": "boolean", "description": "append instead of overwrite"},
            },
            "required": ["path", "content"],
        },
        lambda a: tool_write_file(ctx, a),
        risk=Risk.WRITE,
    )

    reg.register(
        "edit_file",
        "Replace the first exact occurrence of old_text with new_text in a file. "
        "old_text must be copied verbatim from read_file output (include indentation).",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_text", "new_text"],
        },
        lambda a: tool_edit_file(ctx, a),
        risk=Risk.WRITE,
    )

    reg.register(
        "delete_file",
        "Delete a file or an empty directory inside the workspace.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        lambda a: tool_delete_file(ctx, a),
        risk=Risk.DANGEROUS,
    )

    # -- terminal ---------------------------------------------------------- #
    reg.register(
        "run_command",
        "Run a shell command in the workspace and return stdout/stderr/exit code. "
        "Use for tests, builds, installs, scripts.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "relative working directory"},
                "timeout": {"type": "integer", "description": "seconds"},
            },
            "required": ["command"],
        },
        lambda a: tool_run_command(ctx, a),
        risk=Risk.WRITE,
    )

    # -- git --------------------------------------------------------------- #
    reg.register(
        "git",
        "Run git. Read-only subcommands (status/diff/log/branch/show) never need approval; "
        "add/commit/checkout/push do.",
        {
            "type": "object",
            "properties": {
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "git arguments, e.g. ['commit','-m','fix: typo']",
                },
            },
            "required": ["args"],
        },
        lambda a: tool_git(ctx, a),
        risk=Risk.WRITE,
        readonly_subcommands=sorted(GIT_READ_ONLY),
    )

    reg.register(
        "git_prepare",
        "One-shot: create/switch to a role-named branch, stage the given paths (or -A) and commit "
        "with a conventional message. Returns the branch name and commit sha.",
        {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "short task description for the branch/commit name"},
                "message": {"type": "string", "description": "commit message (auto-generated when omitted)"},
                "paths": {"type": "array", "items": {"type": "string"}},
                "branch": {"type": "string", "description": "explicit branch name"},
                "push": {"type": "boolean", "description": "push after committing"},
                "type": {"type": "string", "description": "conventional type: feat|fix|docs|refactor|test|chore"},
            },
            "required": ["task"],
        },
        lambda a: tool_git_prepare(ctx, a, role=role),
        risk=Risk.WRITE,
    )

    # -- github ------------------------------------------------------------ #
    reg.register(
        "gh_create_pr",
        "Open a pull request with the GitHub CLI (falls back to the REST API). Auto-fills the body "
        "with what changed and which Nexus role did it.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "base": {"type": "string", "description": "target branch, default repo default"},
                "head": {"type": "string", "description": "source branch, default current"},
                "draft": {"type": "boolean"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "reviewers": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title"],
        },
        lambda a: tool_gh_create_pr(ctx, a, role=role),
        risk=Risk.WRITE,
    )

    reg.register(
        "gh",
        "Run an arbitrary `gh` CLI command (pr list, issue create, run watch, release ...).",
        {
            "type": "object",
            "properties": {
                "args": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["args"],
        },
        lambda a: tool_gh(ctx, a),
        risk=Risk.WRITE,
    )

    reg.register(
        "gh_merge_pr",
        "Merge or close a pull request. This is destructive - always confirm with the user first.",
        {
            "type": "object",
            "properties": {
                "number": {"type": "integer"},
                "method": {"type": "string", "enum": ["merge", "squash", "rebase"]},
                "close": {"type": "boolean", "description": "close without merging"},
                "delete_branch": {"type": "boolean"},
            },
            "required": ["number"],
        },
        lambda a: tool_gh_merge_pr(ctx, a),
        risk=Risk.DANGEROUS,
    )

    # -- memory ------------------------------------------------------------ #
    reg.register(
        "remember",
        f"Save a durable note for the '{role}' role in .nexus/memory/{role}.md so future sessions "
        "keep this decision/context.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "role": {"type": "string", "description": "defaults to the active role"},
            },
            "required": ["text"],
        },
        lambda a: tool_remember(ctx, a, role=role),
        risk=Risk.WRITE,
    )

    # -- learning surfaces (skills / memory / recall) ---------------------- #
    reg.register(
        "skill_view",
        "Load a saved skill (reusable procedure) by name, in full. Call with no name to list "
        "what is installed. Check the skills index in your system prompt first.",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "skill slug (optional)"}},
        },
        lambda a: tool_skill_view(ctx, a),
        risk=Risk.READ,
    )
    reg.register(
        "skill_manage",
        "Create, patch or delete a skill (agentskills.io SKILL.md). Use it after a non-trivial "
        "task that will repeat, or to fix a skill that was wrong. description <= 60 chars, "
        "body in markdown with When to Use / Procedure / Verification.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "patch", "delete"]},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "category": {"type": "string", "description": "default: learned"},
            },
            "required": ["action", "name"],
        },
        lambda a: tool_skill_manage(ctx, a),
        risk=Risk.WRITE,
    )
    if ctx.memory_store is not None:
        reg.register(
            "memory",
            "Curated long-term memory. section=memory for decisions/gotchas, section=user for "
            "stable preferences about the human. Actions: add, remove, list. Keep entries short.",
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "remove", "list"]},
                    "section": {"type": "string", "enum": ["memory", "user"]},
                    "text": {"type": "string"},
                },
                "required": ["action"],
            },
            lambda a: tool_memory(ctx, a),
            risk=Risk.WRITE,
        )
    if ctx.sessions is not None:
        reg.register(
            "recall",
            "Search your own past sessions in this project (what we did about X, which command "
            "fixed Y). Call with no query for the most recent runs.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            },
            lambda a: tool_recall(ctx, a),
            risk=Risk.READ,
        )

    # -- collaboration ----------------------------------------------------- #
    if include_ask_agent and ctx.providers is not None:
        reg.register(
            "ask_agent",
            "Consult another specialist role (one question, one answer, no tools). Use it for a "
            "second opinion: e.g. backend asks 'security' to review an auth design.",
            {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "description": "target role name from roles.yaml"},
                    "question": {"type": "string"},
                    "context": {"type": "string", "description": "extra background"},
                },
                "required": ["role", "question"],
            },
            lambda a: tool_ask_agent(ctx, a, role=role),
            risk=Risk.READ,
            read_only=True,
        )

    reg.register(
        "finish",
        "Call this when the task is complete. Provide a short summary of what you did and what the "
        "user should check.",
        {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "followups": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary"],
        },
        lambda a: tool_finish(ctx, a),
        risk=Risk.READ,
        read_only=True,
    )

    return reg


# --------------------------------------------------------------------------- #
# tool implementations
# --------------------------------------------------------------------------- #


def tool_list_files(ctx: ToolContext, args: dict[str, Any]) -> str:
    start = ctx.guard.resolve(args.get("path") or ".", must_exist=True)
    pattern = args.get("pattern")
    limit = int(args.get("limit") or 300)
    max_depth = args.get("max_depth")

    if start.is_file():
        return ctx.guard.rel(start)

    if max_depth in (1, "1"):
        entries = []
        for entry in sorted(start.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            rel = entry.relative_to(ctx.workspace).as_posix() if entry.is_absolute() else entry.name
            if entry.name in SENSITIVE_DIRS or _is_excluded(rel, ctx.excludes):
                continue
            entries.append(rel + ("/" if entry.is_dir() else ""))
            if len(entries) >= limit:
                break
        return "\n".join(entries) or "(empty)"

    files = walk_files(start, ctx.excludes, limit)
    if pattern:
        files = [f for f in files if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(Path(f).name, pattern)]
    total = len(files)
    body = "\n".join(files) or "(no files matched)"
    return f"{total} entries under {ctx.guard.rel(start)}:\n{body}"


def tool_read_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    path = ctx.guard.resolve(args["path"], must_exist=True)
    if path.is_dir():
        return f"{path} is a directory - use list_files."
    if path.stat().st_size > 5_000_000:
        raise ToolFailure(f"{path.name} is {path.stat().st_size / 1e6:.1f} MB - too large to read")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = int(args.get("start_line") or 1)
    end = int(args.get("end_line") or len(lines) or 1)
    start = max(1, start)
    end = min(len(lines), end) if lines else 1
    if end < start:
        start, end = end, start
    selected = lines[start - 1 : end]
    numbered = "\n".join(f"{i + start:>5}| {line}" for i, line in enumerate(selected))
    header = f"{ctx.guard.rel(path)} (lines {start}-{end} of {len(lines)})"
    body = clip(numbered, ctx.max_read_chars)
    return f"{header}\n{body}"


def tool_grep(ctx: ToolContext, args: dict[str, Any]) -> str:
    pattern = args["pattern"]
    flags = re.IGNORECASE if args.get("ignore_case") else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ToolFailure(f"bad regex: {exc}") from exc
    root = ctx.guard.resolve(args.get("path") or ".", must_exist=True)
    glob_pat = args.get("glob")
    max_results = int(args.get("max_results") or 120)

    targets: list[Path] = [root] if root.is_file() else [root / f for f in walk_files(root, ctx.excludes, 5000)]
    hits: list[str] = []
    for path in targets:
        rel = ctx.guard.rel(path)
        if glob_pat and not (fnmatch.fnmatch(rel, glob_pat) or fnmatch.fnmatch(path.name, glob_pat)):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{rel}:{lineno}: {line.strip()[:240]}")
                if len(hits) >= max_results:
                    return "\n".join(hits) + f"\n(reached max_results={max_results})"
    return "\n".join(hits) if hits else f"no matches for /{pattern}/"


def tool_write_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    path = ctx.guard.resolve(args["path"])
    content = str(args.get("content", ""))
    append = bool(args.get("append"))
    existed = path.exists()
    old = path.read_text(encoding="utf-8", errors="replace") if existed and path.is_file() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    if append:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)
        action = "appended to"
    else:
        path.write_text(content, encoding="utf-8")
        action = "updated" if existed else "created"
    ctx.state.setdefault("changed_files", set()).add(ctx.guard.rel(path))
    updated = old + content if append else content
    detail = unified_diff_snippet(ctx.guard.rel(path), old, updated, max_lines=400)
    emit_file_change(ctx, path, detail, "created" if not existed else "updated")
    return f"{action} {ctx.guard.rel(path)} ({len(content):,} chars)\n{detail}".strip()


def tool_edit_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    path = ctx.guard.resolve(args["path"], must_exist=True)
    old_text = str(args.get("old_text", ""))
    new_text = str(args.get("new_text", ""))
    if not old_text:
        raise ToolFailure("old_text must not be empty (use write_file to create a new file)")
    original = path.read_text(encoding="utf-8", errors="replace")
    count = original.count(old_text)
    if count == 0:
        fuzzy = _fuzzy_find(original, old_text)
        if fuzzy is None:
            snippet = clip(old_text, 200)
            raise ToolFailure(
                f"old_text not found in {path.name}. Read the file again and copy the text exactly "
                f"(whitespace matters). Looked for:\n{snippet}"
            )
        old_text = fuzzy
        count = original.count(old_text)
    if count > 1 and not args.get("replace_all"):
        raise ToolFailure(
            f"old_text matches {count} places in {path.name}. Add more surrounding context, "
            "or set replace_all=true."
        )
    updated = original.replace(old_text, new_text) if args.get("replace_all") else original.replace(old_text, new_text, 1)
    if updated == original:
        raise ToolFailure("no change produced (old_text == new_text)")
    path.write_text(updated, encoding="utf-8")
    ctx.state.setdefault("changed_files", set()).add(ctx.guard.rel(path))
    detail = unified_diff_snippet(ctx.guard.rel(path), original, updated, max_lines=400)
    emit_file_change(ctx, path, detail, "updated")
    return f"edited {ctx.guard.rel(path)} ({count} occurrence(s))\n{detail}"


def _fuzzy_find(haystack: str, needle: str) -> str | None:
    """Tolerate whitespace/indentation differences."""
    norm = re.sub(r"[ \t]+", " ", needle).strip()
    if not norm:
        return None
    pattern = r"\s*".join(re.escape(part) for part in norm.split("\n") if part.strip())
    match = re.search(pattern, haystack, re.DOTALL)
    return match.group(0) if match else None


def emit_file_change(ctx: ToolContext, path: Path, diff: str, action: str) -> None:
    ctx.emit(Event(EventType.FILE_CHANGE, {"call_id": ctx.state.get("active_call_id", ""),
        "path": ctx.guard.rel(path), "action": action, "diff": clip(diff, 24_000),
        "note": "Applied change; diff may be bounded, not a complete workspace snapshot"}))


def tool_delete_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    path = ctx.guard.resolve(args["path"], must_exist=True)
    if path.is_dir() and any(path.iterdir()):
        raise ToolFailure(f"{path.name} is a non-empty directory - refusing to delete recursively")
    diff = "Deleted directory; no text diff"
    if path.is_file():
        size = path.stat().st_size
        diff = f"Deleted file ({size} bytes); text diff omitted for size or binary content"
        if size <= 500_000:
            try:
                old = path.read_text(encoding="utf-8")
                if "\x00" not in old:
                    diff = unified_diff_snippet(ctx.guard.rel(path), old, "", max_lines=400)
            except UnicodeError:
                pass
    if path.is_dir():
        path.rmdir()
    else:
        path.unlink()
    emit_file_change(ctx, path, diff, "deleted")
    ctx.state.setdefault("changed_files", set()).add(ctx.guard.rel(path))
    return f"deleted {ctx.guard.rel(path)}"


def run_live_command(ctx: ToolContext, command: str, cwd: Path, timeout: float) -> tuple[str, int]:
    from .live_process import stream_shell

    return stream_shell(command, cwd, timeout=min(timeout, ctx.command_timeout), emit=ctx.emit,
        cancel=ctx.cancel_event, call_id=ctx.state.get("active_call_id", ""), max_chars=ctx.max_output_chars,
        tool=ctx.state.get("active_tool", "run_command"))


def tool_run_command(ctx: ToolContext, args: dict[str, Any]) -> str:
    command = str(args.get("command", "")).strip()
    if not command:
        raise ToolFailure("command must not be empty")
    cwd = ctx.guard.resolve(args.get("cwd") or ".")
    cwd.mkdir(parents=True, exist_ok=True)
    timeout = float(ctx.command_timeout if args.get("timeout") is None else args["timeout"])
    if not math.isfinite(timeout) or timeout <= 0:
        raise ToolFailure("timeout must be a positive finite number")
    output, code = run_live_command(ctx, command, cwd, timeout)
    ctx.state["last_exit_code"] = code
    prefix = f"$ {command}\n"
    return prefix + (output or "(no output)")


GIT_READ_ONLY = {"status", "diff", "log", "show", "branch", "remote", "tag", "rev-parse",
                 "ls-files", "describe", "shortlog", "config", "stash"}  # noqa: E501  (used above too)


def tool_git(ctx: ToolContext, args: dict[str, Any]) -> str:
    raw = args.get("args")
    if isinstance(raw, str):
        parts = shlex.split(raw)
    elif isinstance(raw, list):
        parts = [str(p) for p in raw]
    else:
        raise ToolFailure("args must be a list of git arguments")
    if not parts:
        raise ToolFailure("no git arguments given")
    if parts[0].startswith("-"):
        raise ToolFailure("pass git subcommands, not bare flags")
    sub = parts[0]
    output, code = run_live_command(ctx, "git " + shlex.join(parts), ctx.workspace, 60)
    ctx.state["last_exit_code"] = code
    note = "" if sub in GIT_READ_ONLY else f" (git {sub})"
    return f"$ git {' '.join(shlex.quote(p) for p in parts)}{note}\n{output or '(no output)'}"


def slugify(text: str, maxlen: int = 42) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > maxlen:
        slug = slug[:maxlen].rstrip("-")
    return slug or "task"


def make_branch_name(role: str, task: str, prefix: str = "feat") -> str:
    return f"{prefix}/{role}-{slugify(task)}"


def unique_branch(base: str, workspace: Path) -> str:
    candidate = base
    index = 2
    while True:
        _, code = run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}"], workspace)
        if code != 0:
            return candidate
        candidate = f"{base}-{index}"
        index += 1
        if index > 50:
            return f"{base}-{int(time.time())}"


def summarize_changes(workspace: Path, paths: Sequence[str] | None = None) -> str:
    output, _ = run_git(["status", "--porcelain"] + (list(paths) if paths else []), workspace)
    return output.strip()


def conventional_message(task: str, role: str, ctype: str = "feat", changes: str = "") -> str:
    subject = re.sub(r"\s+", " ", task.strip()).strip(".!")
    if len(subject) > 72:
        subject = subject[:71].rstrip() + "…"
    body_lines = [f"Role: {role}"]
    if changes:
        body_lines.append("")
        body_lines.append("Changes:")
        body_lines.extend(f"- {line.strip()}" for line in changes.splitlines()[:20] if line.strip())
    body_lines.append("")
    body_lines.append("Generated with Nexus Agent (auto-routed role).")
    return f"{ctype}({role}): {subject}\n\n" + "\n".join(body_lines)


def tool_git_prepare(ctx: ToolContext, args: dict[str, Any], *, role: str) -> str:
    task = str(args.get("task") or "").strip()
    if not task:
        raise ToolFailure("task is required")
    prefix = (ctx.config.settings.branch_prefix if ctx.config else "feat") or "feat"
    ctype = str(args.get("type") or ("docs" if role in {"docs", "technical-writing"} else "feat"))
    paths = [str(p) for p in (args.get("paths") or [])]

    log: list[str] = []
    # 1. branch
    branch = args.get("branch") or make_branch_name(role, task, prefix)
    current, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], ctx.workspace)
    current = current.strip()
    if current != branch:
        branch = unique_branch(branch, ctx.workspace)
        _, code = run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], ctx.workspace)
        if code == 0:
            out, _ = run_git(["checkout", branch], ctx.workspace)
        else:
            out, _ = run_git(["checkout", "-b", branch], ctx.workspace)
        log.append(f"branch: {branch} (was {current or 'detached'})\n{out.strip()}")
    else:
        log.append(f"branch: already on {branch}")

    # 2. stage
    stage_args = ["add"] + (paths if paths else ["-A"])
    out, _ = run_git(stage_args, ctx.workspace)
    log.append(f"staged: {' '.join(stage_args[1:])}{(' - ' + out.strip()) if out.strip() else ''}")

    staged, _ = run_git(["diff", "--cached", "--name-status"], ctx.workspace)
    if not staged.strip():
        log.append("nothing staged - working tree is clean, no commit created")
        return "\n".join(log)

    # 3. commit
    message = args.get("message") or conventional_message(task, role, ctype, staged.strip())
    out, code = run_git(["commit", "-m", message], ctx.workspace)
    log.append(f"commit: {out.strip()}")
    sha, _ = run_git(["rev-parse", "--short", "HEAD"], ctx.workspace)

    # 4. optional push
    if args.get("push"):
        out, code = run_git(["push", "-u", "origin", branch], ctx.workspace)
        log.append(f"push: {out.strip()}")

    ctx.state["branch"] = branch
    ctx.state["commit_sha"] = sha.strip()
    ctx.state["last_commit_message"] = message
    return "\n".join(log)


# -- GitHub ---------------------------------------------------------------- #


def gh_available() -> bool:
    from shutil import which

    return which("gh") is not None


def github_token(ctx: ToolContext) -> str | None:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(var)
        if value:
            return value
    return None


def repo_slug(ctx: ToolContext) -> str | None:
    out, code = run_git(["config", "--get", "remote.origin.url"], ctx.workspace)
    url = out.strip()
    if code != 0 or not url:
        return None
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return match.group(1) if match else None


def default_branch(ctx: ToolContext) -> str:
    out, _ = run_git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], ctx.workspace)
    if out.strip():
        return out.strip().split("/")[-1]
    out, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], ctx.workspace)
    return out.strip() or "main"


def pr_body(ctx: ToolContext, *, role: str, title: str, user_body: str = "") -> str:
    template_path = None
    if ctx.config is not None:
        template_path = ctx.workspace / ctx.config.settings.pr_template
    if template_path and template_path.is_file():
        template = template_path.read_text(encoding="utf-8")
    else:
        template = (
            "## What changed\n\n<!-- summary -->\n\n## Why\n\n<!-- motivation -->\n\n"
            "## How it was done\n\n## Role / agent\n\n## Checklist\n\n- [ ] Tests pass locally\n"
        )
    changes = summarize_changes(ctx.workspace)
    base, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], ctx.workspace)
    diffstat, _ = run_git(["diff", "--stat", f"origin/{default_branch(ctx)}...HEAD"], ctx.workspace)
    commits, _ = run_git(["log", "--oneline", f"origin/{default_branch(ctx)}..HEAD"], ctx.workspace)

    agent_label = role
    if ctx.config is not None:
        try:
            agent_cfg = ctx.config.agent_for_role(ctx.config.role(role))
            agent_label = f"{role} → {agent_cfg.name} ({agent_cfg.model})"
        except Exception:  # noqa: BLE001
            pass

    replacements = {
        "<!-- summary -->": (user_body or title).strip(),
        "<!-- motivation -->": f"Requested via Nexus Agent, auto-routed to **{role}**.",
    }
    body = template
    for key, value in replacements.items():
        body = body.replace(key, value)

    extras = [
        "\n---\n",
        "### Nexus run",
        f"- **Role**: {agent_label}",
        f"- **Branch**: `{base.strip()}`",
    ]
    if commits.strip():
        extras.append("- **Commits**:\n" + "\n".join(f"  - `{c.strip()}`" for c in commits.strip().splitlines()[:15]))
    if diffstat.strip():
        extras.append("\n<details><summary>diffstat</summary>\n\n```\n" + diffstat.strip() + "\n```\n</details>")
    if changes:
        extras.append("\n<details><summary>working tree at commit time</summary>\n\n```\n" + changes + "\n```\n</details>")
    extras.append("\n_Opened by [Nexus Agent](../blob/main/README.md) — auto-routed specialist team._")
    return body + "\n".join(extras)


def tool_gh_create_pr(ctx: ToolContext, args: dict[str, Any], *, role: str) -> str:
    title = str(args.get("title") or "").strip()
    if not title:
        raise ToolFailure("title is required")
    head = args.get("head") or run_git(["rev-parse", "--abbrev-ref", "HEAD"], ctx.workspace)[0].strip()
    base = args.get("base") or default_branch(ctx)
    body = pr_body(ctx, role=role, title=title, user_body=str(args.get("body") or ""))

    if gh_available():
        cmd = ["gh", "pr", "create", "--title", title, "--body", body, "--base", base, "--head", head]
        if args.get("draft"):
            cmd.append("--draft")
        for label in args.get("labels") or []:
            cmd += ["--label", str(label)]
        for reviewer in args.get("reviewers") or []:
            cmd += ["--reviewer", str(reviewer)]
        output, code = run_live_command(ctx, shlex.join(cmd), ctx.workspace, 120)
        if code == 0:
            ctx.state["last_pr"] = output.strip().splitlines()[-1] if output.strip() else ""
            return f"pull request created:\n{output.strip()}"
        return f"gh pr create failed (exit {code}):\n{output.strip()}"

    token = github_token(ctx)
    slug = repo_slug(ctx)
    if not token or not slug:
        raise ToolFailure(
            "neither the `gh` CLI nor GITHUB_TOKEN + a github.com remote is available - cannot open a PR"
        )
    import httpx

    resp = httpx.post(
        f"https://api.github.com/repos/{slug}/pulls",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": title, "head": head, "base": base, "body": body, "draft": bool(args.get("draft"))},
        timeout=60,
    )
    if resp.status_code >= 400:
        return f"GitHub API {resp.status_code}: {resp.text[:800]}"
    data = resp.json()
    ctx.state["last_pr"] = data.get("html_url", "")
    for label in args.get("labels") or []:
        httpx.post(
            f"https://api.github.com/repos/{slug}/issues/{data['number']}/labels",
            headers={"Authorization": f"Bearer {token}"},
            json={"labels": [str(label)]},
            timeout=30,
        )
    return f"pull request created: {data.get('html_url')} (#{data.get('number')})"


def tool_gh(ctx: ToolContext, args: dict[str, Any]) -> str:
    if not gh_available():
        raise ToolFailure("the `gh` CLI is not installed (https://cli.github.com)")
    parts = [str(a) for a in (args.get("args") or [])]
    if not parts:
        raise ToolFailure("no gh arguments given")
    output, code = run_live_command(ctx, shlex.join(["gh", *parts]), ctx.workspace, 120)
    return f"$ gh {' '.join(shlex.quote(p) for p in parts)}\n{output.strip() or '(no output)'}"


def tool_gh_merge_pr(ctx: ToolContext, args: dict[str, Any]) -> str:
    number = int(args.get("number") or 0)
    if number <= 0:
        raise ToolFailure("a positive PR number is required")
    if not gh_available():
        raise ToolFailure("the `gh` CLI is required to merge/close a PR")
    if args.get("close"):
        cmd = ["gh", "pr", "close", str(number)]
    else:
        method = str(args.get("method") or "merge")
        if method not in {"merge", "squash", "rebase"}:
            raise ToolFailure("method must be merge|squash|rebase")
        cmd = ["gh", "pr", "merge", str(number), f"--{method}"]
        if args.get("delete_branch"):
            cmd.append("--delete-branch")
    output, code = run_live_command(ctx, shlex.join(cmd), ctx.workspace, 180)
    return f"$ {' '.join(shlex.quote(c) for c in cmd)}\n{output.strip() or '(no output)'}"


# -- memory / collaboration ------------------------------------------------ #


def memory_for(ctx: ToolContext) -> RoleMemory:
    settings = ctx.config.settings if ctx.config is not None else None
    return RoleMemory(
        ctx.workspace,
        getattr(settings, "memory_dir", ".nexus/memory"),
        max_chars=getattr(settings, "memory_max_chars", 4000),
        enabled=getattr(settings, "memory_enabled", True),
    )


def tool_remember(ctx: ToolContext, args: dict[str, Any], *, role: str) -> str:
    target_role = str(args.get("role") or role)
    note = str(args["text"]).strip()
    path = memory_for(ctx).append(target_role, note)
    ctx.emit(Event(EventType.MEMORY, {"role": target_role, "path": str(path)}))
    return f"remembered for '{target_role}' -> {ctx.guard.rel(path)}\nnote: {note}"


def tool_ask_agent(ctx: ToolContext, args: dict[str, Any], *, role: str) -> str:
    if ctx.providers is None or ctx.config is None:
        raise ToolFailure("ask_agent is unavailable in this context")
    if ctx.ask_agent_depth >= 1:
        raise ToolFailure("nested ask_agent calls are not allowed (depth limit reached)")
    target_name = str(args.get("role") or "general")
    target = ctx.config.roles.get(target_name, default="general")
    if target.name == role:
        return "That is your own role - answer the question yourself."
    question = str(args.get("question") or "").strip()
    if not question:
        raise ToolFailure("question must not be empty")
    context = str(args.get("context") or "").strip()
    prompt = question if not context else f"Context from {role}:\n{context}\n\nQuestion:\n{question}"

    agent_cfg = ctx.config.agent_for_role(target)
    system = (
        f"{target.persona}\n\nYou are being consulted by the '{role}' specialist inside Nexus. "
        "Answer concisely and technically. You have NO tools here - just your expertise."
    )
    from .types import Message as _Message

    provider, completion = ctx.providers.call_with_fallback(
        agent_cfg.name,
        system,
        [_Message(role="user", content=prompt)],
        None,
        max_tokens=min(int(agent_cfg.max_tokens), 2048),
        temperature=target.temperature,
        on_event=ctx.emit,
    )
    answer = completion.text or "(no answer)"
    ctx.emit(Event(EventType.STATUS, {"message": f"⇄ asked '{target.name}' via {provider.name}"}))
    return clip(f"[{target.name} ({provider.label()}) answers]\n{answer}", ctx.max_output_chars)


def tool_finish(ctx: ToolContext, args: dict[str, Any]) -> str:
    summary = str(args.get("summary") or "").strip()
    followups = [str(f) for f in (args.get("followups") or [])]
    ctx.state["finished"] = True
    ctx.state["summary"] = summary
    ctx.state["followups"] = followups
    lines = ["Task marked complete."]
    if summary:
        lines.append(f"Summary: {summary}")
    if followups:
        lines.append("Follow-ups: " + "; ".join(followups))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# read-only tool subset (used by `nexus ask` / plan mode)
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# skills / memory / recall tools (Hermes-style learning surfaces)
# --------------------------------------------------------------------------- #


def tool_skill_view(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Load one SKILL.md in full - the agent reads the procedure, not a summary."""
    from . import skills as skilllib

    name = str(args.get("name") or "").strip()
    if not name:
        library = skilllib.discover(ctx.workspace)
        if not library:
            return "No skills are installed yet. `skill_manage` can create the first one."
        return "Available skills:\n" + "\n".join(s.one_line() for s in library)
    skill = skilllib.get(name, ctx.workspace)
    if skill is None:
        library = skilllib.discover(ctx.workspace)
        close = [s.name for s in library if name.lower() in s.name.lower()][:8]
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        return f"No skill named '{name}'.{hint}"
    skill.record_use()
    header = (
        f"# Skill: {skill.name} (v{skill.version}, source: {skill.source})\n"
        f"description: {skill.description}\n"
        f"tags: {', '.join(skill.tags) or '-'}\n"
        f"path: {skill.path}\n"
    )
    support = ""
    if skill.dir is not None:
        files = sorted(
            str(p.relative_to(skill.dir))
            for p in skill.dir.rglob("*")
            if p.is_file() and p.name != skilllib.SKILL_FILE
        )[:20]
        if files:
            support = "\nsupport files (skill-relative): " + ", ".join(files) + "\n"
    return header + support + "\n" + skill.body[: ctx.max_read_chars]


def tool_skill_manage(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Create / patch / delete one skill (agentskills.io layout)."""
    from . import skills as skilllib

    action = str(args.get("action") or "create").strip().lower()
    name = str(args.get("name") or "").strip().lower()
    if not name:
        raise ToolFailure("`name` is required")
    if action == "delete":
        return "deleted" if skilllib.delete_skill(name, ctx.workspace) else f"no skill named '{name}'"
    if action not in {"create", "patch"}:
        raise ToolFailure("action must be create, patch or delete")

    existing = skilllib.get(name, ctx.workspace)
    if action == "create" and existing is not None:
        action = "patch"
    if action == "patch":
        if existing is None:
            raise ToolFailure(f"no skill named '{name}' - use action=create")
        if existing.source == "hermes":
            raise ToolFailure(f"'{name}' is imported from Hermes - create a new skill instead")
        body = args.get("body") if isinstance(args.get("body"), str) and args["body"].strip() else None
        description = args.get("description") if isinstance(args.get("description"), str) else None
        if body is None and description is None:
            raise ToolFailure("patch needs a new `body` or `description`")
        skilllib.patch_skill(existing.path, body=body, description=description)
        return f"patched skill '{name}' ({existing.path})"

    description = str(args.get("description") or "").strip()
    body = str(args.get("body") or "").strip()
    tags = args.get("tags") if isinstance(args.get("tags"), list) else []
    warnings = skilllib.lint(name, description, body, tags=tags)
    if any("invalid" in w or "required" in w for w in warnings):
        raise ToolFailure("; ".join(warnings))
    path = skilllib.create_skill(
        name=name, description=description, body=body, tags=tags,
        category=str(args.get("category") or "learned"), workspace=ctx.workspace,
        provenance={"created_by": "agent"},
    )
    note = f" (warnings: {'; '.join(warnings)})" if warnings else ""
    ctx.emit(Event(EventType.SKILL, {"action": "create", "name": name, "path": str(path)}))
    return f"created skill '{name}' at {path}{note}"


def tool_memory(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Curated long-term memory (MEMORY.md + USER.md)."""
    store = ctx.memory_store
    if store is None:
        raise ToolFailure("memory is unavailable in this context")
    action = str(args.get("action") or "add").strip().lower()
    section = str(args.get("section") or "memory").strip().lower()
    text = str(args.get("text") or "").strip()
    if action == "list":
        block = store.block(section)
        return f"{block.name} ({block.chars}/{block.limit} chars)\n" + (
            block.render() or "(empty)"
        )
    if action == "remove":
        ok, message = store.remove(text, section)
        if not ok:
            raise ToolFailure(message)
        return message
    ok, message = store.add(text, section)
    if not ok:
        raise ToolFailure(message)
    ctx.emit(Event(EventType.MEMORY, {"role": section, "text": text,
                                      "path": str(store.block(section).path)}))
    return message


def tool_recall(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Search this project's past runs (FTS5 over stored transcripts)."""
    if ctx.sessions is None:
        raise ToolFailure("session history is unavailable in this context")
    query = str(args.get("query") or "").strip()
    limit = int(args.get("limit") or 5)
    if not query:
        rows = ctx.sessions.recent(limit)
        if not rows:
            return "No past sessions recorded yet."
        return "\n".join(
            f"{row['id']}  {row.get('role') or '-':<18} {str(row.get('task') or '')[:110]}"
            for row in rows
        )
    rows = ctx.sessions.search(query, limit=limit)
    if not rows:
        return f"No past session matches '{query}'."
    from .sessions import format_hit

    return "\n\n".join(format_hit(row) + (f"\n        …{row.get('hit', '')}" if row.get("hit") else "")
                        for row in rows)


READ_ONLY_TOOLS = {
    "list_files", "read_file", "grep", "git", "ask_agent", "finish", "skill_view", "recall",
}


def restrict_to_read_only(names: Sequence[str]) -> list[str]:
    return [n for n in names if n in READ_ONLY_TOOLS]
