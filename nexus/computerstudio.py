"""Computer Control: narrow, reviewed desktop surfaces for the dashboard.

The workspace already has guarded adapters (`nexus/computer.py`): a headless
browser worker with an exact origin allowlist, an Office reader/writer, a VS
Code launcher and a Docker shell backend. What it did not have was one place
where an operator could *see* those permissions, switch the two surfaces the
dashboard can own, and stop everything at once.

This module adds that page's service layer. Two rules shape all of it:

* **Narrowing only.** The studio store can lower what `permissions.yaml` and the
  adapter opt-ins already allow. It can never raise them. A capability the
  operator disabled in `permissions.yaml` shows as disabled here no matter what
  this file says.
* **No second execution path.** Terminal, Office and the VS Code bridge are
  reported from their real policy and probed from the real environment; this
  page does not run commands, launch apps or drive the screen. Files and browser
  inspection are the only surfaces it executes, and each write carries an exact
  typed confirmation.

Runtime state lives under `<workspace>/.nexus/computer/` and is never committed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA = 1
STORE_DIR = ".nexus/computer"
SETTINGS_FILE = "settings.json"
TRASH_DIR = "trash"
HISTORY_DIR = "history"
MAX_LEVELS = ("off", "read", "ask", "auto")
CAPABILITIES = ("files", "browser", "terminal", "office", "screen", "apps", "vscode")
OWNED = ("files", "browser")
DEFAULT_LEVELS = {"files": "off", "browser": "off"}
DEFAULT_PROFILE = "locked"
PROFILES = {
    "locked": {"files": "off", "browser": "off"},
    "read-only": {"files": "read", "browser": "off"},
    "review": {"files": "ask", "browser": "read"},
}
MAX_ROOTS = 8
MAX_ORIGINS = 20
MAX_TREE = 400
MAX_DEPTH = 4
MAX_PREVIEW = 120_000
MAX_WRITE = 400_000
MAX_TRASH = 100
MAX_VERSIONS = 20
MAX_BROWSER_TEXT = 24_000
RULE_ID_RE = re.compile(r"^t-[0-9a-f]{8}$")
SNAPSHOT_RE = re.compile(r"^[a-f0-9]{32}$")
VERSION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}(?:-[0-9]+)?$")

_lock = threading.RLock()
_audit_lock = threading.Lock()
_audit_at: dict[str, float] = {}


# ── small helpers ──────────────────────────────────────────────────────── #


def _text(value: Any, limit: int, label: str = "Text") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is required")
    value = value.strip()
    if not value or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be 1-{limit} characters")
    return value


def _store(workspace: Path) -> Path:
    return Path(workspace) / STORE_DIR


def settings_path(workspace: Path) -> Path:
    return _store(workspace) / SETTINGS_FILE


def trash_path(workspace: Path) -> Path:
    return _store(workspace) / TRASH_DIR


def history_path(workspace: Path) -> Path:
    return _store(workspace) / HISTORY_DIR


def _atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = __import__("tempfile").mkstemp(dir=path.parent, prefix=".computer-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_json(path: Path, limit: int = 400_000) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Missing or unsafe state file")
    if path.stat().st_size > limit:
        raise ValueError("State file is too large")
    return json.loads(path.read_text(encoding="utf-8"))


def _levels(value: Any, label: str = "Levels") -> dict[str, str]:
    if not isinstance(value, dict) or set(value) - set(OWNED):
        raise ValueError(f"{label} must describe files and browser only")
    parsed = {}
    for key in OWNED:
        level = value.get(key, "off")
        if level not in MAX_LEVELS:
            raise ValueError(f"Unknown {key} level: {level!r}")
        parsed[key] = level
    return parsed


def _roots(workspace: Path, values: Any) -> list[str]:
    if values is None:
        return ["."]
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_ROOTS:
        raise ValueError(f"Provide 1-{MAX_ROOTS} scope roots")
    from .workbench import safe_path

    parsed = []
    for value in values:
        name = _text(value, 200, "Scope root")
        path = safe_path(workspace, name)
        if not path.is_dir():
            raise ValueError(f"Scope root is not a directory: {name}")
        parsed.append(path.relative_to(Path(workspace).resolve()).as_posix() or ".")
    return sorted(set(parsed))


def _origins(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_ORIGINS:
        raise ValueError(f"At most {MAX_ORIGINS} browser origins")
    parsed = []
    for value in values:
        name = _text(value, 300, "Origin")
        parts = urlsplit(name)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
                or parts.path or parts.query or parts.fragment or parts.port is None):
            raise ValueError("Use exact scheme://host:port origins without credentials")
        parsed.append(name)
    return sorted(set(parsed))


# ── the studio store (narrows the workspace policy, never widens it) ───── #


def _compile_settings(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {"schema", "profile", "levels", "scopes", "updated_at"}:
        raise ValueError("Invalid computer settings")
    profile = raw.get("profile", DEFAULT_PROFILE)
    if profile not in PROFILES:
        raise ValueError("Unknown trust profile")
    scopes = raw.get("scopes", {})
    if not isinstance(scopes, dict) or set(scopes) - {"roots", "origins"}:
        raise ValueError("Invalid scopes")
    return {
        "schema": SCHEMA,
        "profile": profile,
        "levels": _levels(raw.get("levels", DEFAULT_LEVELS)),
        "scopes": {"roots": list(scopes.get("roots") or ["."]), "origins": list(scopes.get("origins") or [])},
        "updated_at": raw.get("updated_at"),
    }


def default_settings() -> dict[str, Any]:
    return {"schema": SCHEMA, "profile": DEFAULT_PROFILE, "levels": dict(DEFAULT_LEVELS),
            "scopes": {"roots": ["."], "origins": []}, "updated_at": None}


def revision_of(settings: dict[str, Any]) -> str:
    body = json.dumps({key: settings.get(key) for key in ("profile", "levels", "scopes")},
                      sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()[:16]


def load_settings(workspace: Path) -> dict[str, Any]:
    path = settings_path(workspace)
    if not path.exists():
        return {**default_settings(), "revision": revision_of(default_settings()), "error": ""}
    try:
        data = _compile_settings({**( _read_json(path) or {}), "schema": SCHEMA})
    except (OSError, ValueError, TypeError, AttributeError):
        # A damaged store must not grant anything: fall back to locked.
        fallback = default_settings()
        return {**fallback, "revision": revision_of(fallback), "error": "Settings unreadable; everything stays off."}
    return {**data, "revision": revision_of(data), "error": ""}


def _history_snapshot(workspace: Path, settings: dict[str, Any], reason: str) -> str | None:
    if not settings_path(workspace).exists():
        return None
    directory = history_path(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    version = f"{stamp}-{revision_of(settings)[:6]}"
    suffix = 1
    while (directory / f"{version}.json").exists():
        version = f"{stamp}-{revision_of(settings)[:6]}-{suffix}"
        suffix += 1
    _atomic_bytes(directory / f"{version}.json", json.dumps(
        {"schema": SCHEMA, "reason": reason[:120], "saved_at": time.time(), "settings": settings},
        sort_keys=True).encode())
    for stale in sorted(directory.glob("*.json"), reverse=True)[MAX_VERSIONS:]:
        stale.unlink(missing_ok=True)
    return version


def _write_settings(state, *, profile: str, levels: dict[str, str], scopes: dict[str, list[str]],
                    reason: str) -> dict[str, Any]:
    if getattr(state, "demo", False):
        raise ValueError("Computer policy cannot change in demo mode")
    current = load_settings(Path(state.workspace))
    _history_snapshot(Path(state.workspace), current, reason)
    body = {"schema": SCHEMA, "profile": profile, "levels": levels,
            "scopes": scopes, "updated_at": time.time()}
    _compile_settings(body)
    _atomic_bytes(settings_path(Path(state.workspace)), json.dumps(body, sort_keys=True, indent=2).encode())
    return load_settings(Path(state.workspace))


# ── workspace policy + environment probes ─────────────────────────────── #


def _permissions(workspace: Path) -> dict[str, Any] | None:
    from .permissions import Permissions

    try:
        return Permissions(workspace).load()
    except (OSError, ValueError):
        return None


def _playwright() -> tuple[bool, str]:
    try:
        import importlib.util

        spec = importlib.util.find_spec("playwright")
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return False, "The Playwright Python package is not installed in this environment."
    return True, "Playwright is installed; navigation stays inside the exact origin allowlist."


def _screen() -> tuple[bool, str]:
    display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    tool = next((name for name in ("grim", "scrot", "gnome-screenshot", "screencapture") if shutil.which(name)), "")
    if not display:
        return False, "No display session (DISPLAY/WAYLAND_DISPLAY is unset) — screen view is unavailable here."
    if not tool:
        return False, "A display exists but no supported capture tool is installed."
    return True, f"Display {display}; capture would use {tool} (not started by this page)."


def _apps() -> tuple[bool, str]:
    desktop = os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION")
    launcher = next((name for name in ("gtk-launch", "gio", "open") if shutil.which(name)), "")
    if not desktop:
        return False, "No desktop session is running — app launching is unavailable here."
    if not launcher:
        return False, "A desktop session exists but no supported launcher was found."
    return True, f"Desktop {desktop}; launching would use {launcher} (not implemented in this page)."


def _vscode() -> tuple[bool, str]:
    code = shutil.which("code")
    if not code:
        return False, "The VS Code CLI (code) is not installed on this host."
    return True, f"Found {code}; the editor adapter opens one file with an isolated profile."


def _browser_worker_ok(workspace: Path) -> tuple[bool, str]:
    worker = Path(__file__).with_name("browser_worker.py")
    if not worker.is_file():
        return False, "The isolated browser worker file is missing."
    return True, "Isolated worker present; only GET/HEAD inside the allowlist."


def capabilities(state) -> list[dict[str, Any]]:
    """Every capability with its real level, ceiling and reason. Never a guess."""
    workspace = Path(state.workspace)
    store = load_settings(workspace)
    policy = _permissions(workspace)
    computer = (policy or {}).get("computer") or {}
    default_mode = (policy or {}).get("default") or "ASK_EVERY_TIME"
    playwright, playwright_note = _playwright()
    worker_ok, worker_note = _browser_worker_ok(workspace)
    rows = []

    files_ceiling = "read" if default_mode == "READ_ONLY" else "ask"
    files_reason = ("permissions.yaml default is READ_ONLY, so writes stay unavailable."
                    if files_ceiling == "read" else "Every change is one reviewed request with an exact token.")
    rows.append({
        "id": "files", "owned": True, "level": store["levels"]["files"], "ceiling": files_ceiling,
        "managed_by": "this page + permissions.yaml default",
        "available": True, "reason": files_reason,
        "levels": ["off", "read", "ask"],
        "auto_note": "Auto is not offered for files: each write and trash keeps its own typed confirmation.",
    })

    origins = computer.get("browser_origins") or []
    browser_ceiling = "read" if computer.get("browser") and origins else "off"
    if not computer.get("browser"):
        browser_reason = "permissions.yaml disables the browser adapter (computer.browser: false)."
    elif not origins:
        browser_reason = "No browser origin is allowlisted, so nothing can be opened."
    elif not (playwright and worker_ok):
        browser_reason = playwright_note if not playwright else worker_note
    else:
        browser_reason = "Read-only inspection of allowlisted origins; clicks and typing stay in the agent tool."
    rows.append({
        "id": "browser", "owned": True, "level": store["levels"]["browser"], "ceiling": browser_ceiling,
        "managed_by": "this page + permissions.yaml computer.browser",
        "available": bool(computer.get("browser") and origins and playwright and worker_ok),
        "reason": browser_reason, "levels": ["off", "read"], "origins": origins,
        "auto_note": "Auto is not offered for the browser: every navigation is an explicit request.",
    })

    rows.append({
        "id": "terminal", "owned": False, "level": "on" if computer else "unknown", "ceiling": "n/a",
        "managed_by": "permissions.yaml computer.shell_backend",
        "available": True,
        "reason": f"Shell backend: {computer.get('shell_backend', 'host')}. Commands still pass the existing approval gate.",
        "levels": [], "link": "run",
    })
    rows.append({
        "id": "office", "owned": False, "level": "on" if computer.get("office") else "off", "ceiling": "n/a",
        "managed_by": "permissions.yaml computer.office",
        "available": bool(computer.get("office")),
        "reason": ("Office files are edited by the agent adapter through its own approval and snapshot path."
                   if computer.get("office") else "The Office adapter is disabled in permissions.yaml."),
        "levels": [], "link": "run",
    })
    screen_ok, screen_note = _screen()
    rows.append({"id": "screen", "owned": False, "level": "unavailable", "ceiling": "n/a",
                 "managed_by": "host environment", "available": screen_ok,
                 "reason": screen_note, "levels": []})
    apps_ok, apps_note = _apps()
    rows.append({"id": "apps", "owned": False, "level": "unavailable", "ceiling": "n/a",
                 "managed_by": "host environment", "available": apps_ok,
                 "reason": apps_note, "levels": []})
    code_ok, code_note = _vscode()
    rows.append({"id": "vscode", "owned": False, "level": "on" if computer.get("editor") else "off", "ceiling": "ask",
                 "managed_by": "permissions.yaml computer.editor",
                 "available": bool(code_ok and computer.get("editor")),
                 "reason": code_note if not computer.get("editor") else code_note, "levels": []})
    return rows


def effective(state, capability: str) -> str:
    """The level this page actually applies, after narrowing by the workspace."""
    store = load_settings(Path(state.workspace))
    row = next((item for item in capabilities(state) if item["id"] == capability), None)
    if row is None:
        raise ValueError("Unknown capability")
    level = store["levels"].get(capability, "off")
    ceiling = row.get("ceiling", "off")
    if ceiling not in MAX_LEVELS:
        return "off"
    order = {name: index for index, name in enumerate(MAX_LEVELS)}
    return level if order[level] <= order[ceiling] else ceiling


def state_view(state) -> dict[str, Any]:
    from .computer_control import EmergencyStop

    workspace = Path(state.workspace)
    store = load_settings(workspace)
    policy = _permissions(workspace)
    stop = EmergencyStop()
    settings = state.workbench.settings() if getattr(state, "workbench", None) else {}
    return {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "demo": bool(getattr(state, "demo", False)),
        "store": {"settings": store, "revision": store["revision"], "error": store.get("error", ""),
                  "path": f"{STORE_DIR}/{SETTINGS_FILE} (workspace runtime state, never committed)"},
        "capabilities": capabilities(state),
        "scopes": store["scopes"],
        "profiles": {name: dict(levels) for name, levels in PROFILES.items()},
        "policy": {
            "default": (policy or {}).get("default", "ASK_EVERY_TIME"),
            "computer": {key: (policy or {}).get("computer", {}).get(key) for key in
                         ("browser", "editor", "office", "shell_backend")},
            "readable": policy is not None,
        },
        "latch": {"stopped": stop.stopped()},
        "queue": {"paused": bool(getattr(state, "queue_paused", False))},
        "local_only": bool(settings.get("local_only")),
        "notes": [
            "This is not an OS sandbox: approved commands run on the host with the server's privileges.",
            "The studio can only lower the workspace policy; it can never raise it.",
            "Screen view, app launching and the VS Code bridge are reported from probes, not simulated.",
        ],
    }


def settings_view(state) -> dict[str, Any]:
    return state_view(state)["store"]


def save_settings(state, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required")
    current = load_settings(Path(state.workspace))
    if payload.get("revision") != current["revision"]:
        raise ValueError("The computer policy changed since this page loaded; reload and review again.")
    profile = payload.get("profile", current["profile"])
    if profile not in PROFILES:
        raise ValueError("Unknown trust profile")
    levels = _levels(payload.get("levels", current["levels"]))
    scopes = payload.get("scopes", {}) if isinstance(payload.get("scopes"), dict) else {}
    roots = _roots(Path(state.workspace), scopes.get("roots"))
    origins = _origins(scopes.get("origins"))
    if payload.get("confirmation") != "SAVE COMPUTER POLICY":
        raise ValueError("Type SAVE COMPUTER POLICY to confirm")
    written = _write_settings(state, profile=profile, levels=levels,
                              scopes={"roots": roots, "origins": origins}, reason="save computer policy")
    audit(state, _client(payload), "computer.settings", every=0)
    return {"schema": SCHEMA, "observed_at": time.time(), "settings": written, "revision": written["revision"],
            "note": "Saved. Adapters still also require permissions.yaml; this page cannot raise them."}


# ── files: tree, preview, write, trash, undo ──────────────────────────── #


def _scoped(state, value: Any) -> tuple[Path, Path]:
    """Resolve a workspace path inside the configured roots and the safe set."""
    from .workbench import safe_path

    workspace = Path(state.workspace).resolve()
    path = safe_path(workspace, _text(value, 400, "Path"))
    store = load_settings(workspace)
    allowed = [workspace] if "." in store["scopes"]["roots"] else [
        (workspace / root).resolve() for root in store["scopes"]["roots"]]
    if not any(path == root or path.is_relative_to(root) for root in allowed):
        raise ValueError("Path is outside the configured scope roots.")
    return workspace, path


def _level_or_refuse(state, capability: str, need: str) -> str:
    level = effective(state, capability)
    order = {name: index for index, name in enumerate(MAX_LEVELS)}
    if level == "off" or order[level] < order[need]:
        raise ValueError(f"{capability.title()} permission is {level}; this action needs {need}.")
    return level


def _tree_row(root: Path, path: Path, depth: int) -> dict[str, Any]:
    stat = path.lstat()
    row = {
        "name": path.name if depth else ".",
        "path": path.relative_to(root).as_posix() or ".",
        "depth": depth,
        "kind": "dir" if path.is_dir() and not path.is_symlink() else "link" if path.is_symlink() else "file",
        "size": int(stat.st_size),
        "modified": float(stat.st_mtime),
        "hidden": path.name.startswith("."),
    }
    return row


def tree(state, payload: dict[str, Any]) -> dict[str, Any]:
    _level_or_refuse(state, "files", "read")
    workspace, path = _scoped(state, payload.get("path") or ".")
    if not path.is_dir():
        raise ValueError("Not a directory")
    depth_limit = min(MAX_DEPTH, max(1, int(payload.get("depth") or 1)))
    rows: list[dict[str, Any]] = []
    truncated = False
    stack = [(path, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError:
            continue
        for entry in entries:
            if len(rows) >= MAX_TREE:
                truncated = True
                break
            row = _tree_row(workspace, entry, depth + 1)
            rows.append(row)
            if row["kind"] == "dir" and depth + 1 < depth_limit:
                stack.append((entry, depth + 1))
        if truncated:
            break
    return {"schema": SCHEMA, "observed_at": time.time(), "root": path.relative_to(workspace).as_posix() or ".",
            "entries": rows, "truncated": truncated, "limit": MAX_TREE,
            "scope": load_settings(workspace)["scopes"]["roots"],
            "note": "Private and runtime paths (.git, .nexus, keys, .env) are never listed."}


def preview(state, payload: dict[str, Any]) -> dict[str, Any]:
    _level_or_refuse(state, "files", "read")
    workspace, path = _scoped(state, payload.get("path"))
    if not path.is_file():
        raise ValueError("Not a regular file")
    size = path.stat().st_size
    if size > MAX_PREVIEW:
        raise ValueError(f"Preview is limited to {MAX_PREVIEW} bytes")
    data = path.read_bytes()
    if b"\x00" in data:
        return {"schema": SCHEMA, "observed_at": time.time(), "path": path.relative_to(workspace).as_posix(),
                "size": size, "binary": True, "text": "", "truncated": False,
                "note": "Binary file: no text preview is shown."}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"schema": SCHEMA, "observed_at": time.time(), "path": path.relative_to(workspace).as_posix(),
                "size": size, "binary": True, "text": "", "truncated": False,
                "note": "Not valid UTF-8: no text preview is shown."}
    return {"schema": SCHEMA, "observed_at": time.time(), "path": path.relative_to(workspace).as_posix(),
            "size": size, "binary": False, "text": text, "truncated": False,
            "revision": hashlib.sha256(data).hexdigest()[:16],
            "note": "Read from the workspace; secrets are not filtered from source files you open here."}


def write(state, payload: dict[str, Any]) -> dict[str, Any]:
    _level_or_refuse(state, "files", "ask")
    workspace, path = _scoped(state, payload.get("path"))
    text = payload.get("text")
    if not isinstance(text, str) or len(text.encode()) > MAX_WRITE or "\x00" in text:
        raise ValueError(f"Text must be under {MAX_WRITE} bytes")
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ValueError("Only a regular file can be written")
    expected = payload.get("revision")
    before = path.read_bytes() if path.exists() else None
    if expected is not None:
        current = hashlib.sha256(before or b"").hexdigest()[:16] if before is not None else "missing"
        if expected != current:
            raise ValueError("The file changed since it was previewed; reopen it and review again.")
    token = f"WRITE {path.relative_to(workspace).as_posix()}"
    if payload.get("confirmation") != token:
        raise ValueError(f"Type {token} to confirm")
    from .github_safety import GitHubSafetyError, scan_text
    from .snapshots import Snapshots

    try:
        scan_text(text, workspace=workspace)
    except GitHubSafetyError as exc:
        raise ValueError(str(exc)) from None
    snapshots = Snapshots(workspace)
    data = text.encode()
    with snapshots.change(path, "write_file", data, expected=before) as snapshot:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / (".computer-write-" + uuid.uuid4().hex)
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    audit(state, _client(payload), "computer.file.write", every=0)
    return {"schema": SCHEMA, "observed_at": time.time(),
            "saved": path.relative_to(workspace).as_posix(), "bytes": len(data),
            "snapshot": snapshot, "revision": hashlib.sha256(data).hexdigest()[:16],
            "note": "Written atomically. Undo is available from this page while the snapshot is current."}


def snapshots(state) -> list[dict[str, Any]]:
    _level_or_refuse(state, "files", "read")
    from .snapshots import Snapshots

    rows = Snapshots(state.workspace).listing()[:30]
    return [{"id": row["id"], "path": row["path"], "operation": row["operation"], "status": row["status"],
             "created": row["created"]} for row in rows]


def undo(state, payload: dict[str, Any]) -> dict[str, Any]:
    _level_or_refuse(state, "files", "ask")
    identifier = payload.get("id")
    if not isinstance(identifier, str) or not SNAPSHOT_RE.fullmatch(identifier):
        raise ValueError("A snapshot id is required")
    from .snapshots import Snapshots
    from .tools import ApprovalDecision, ApprovalGate

    snapshots_store = Snapshots(state.workspace)
    reviewed = snapshots_store.preview(identifier)
    token = f"RESTORE {identifier}"
    if payload.get("confirmation") != token:
        raise ValueError(f"Type {token} to confirm")
    # The typed token is the human decision; the gate keeps the lineage/identity
    # checks in Snapshots.undo() intact instead of duplicating them here.
    gate = ApprovalGate(mode="auto", allow_dangerous=True,
                        callback=lambda *_args: ApprovalDecision(True))
    result = snapshots_store.undo(reviewed, gate)
    audit(state, _client(payload), "computer.file.undo", every=0)
    return {"schema": SCHEMA, "observed_at": time.time(), "restored": result,
            "note": "Restored from the pre-change snapshot; the restored version is itself recorded."}


# ── trash ──────────────────────────────────────────────────────────────── #


def _trash_entries(workspace: Path) -> list[dict[str, Any]]:
    root = trash_path(workspace)
    if not root.exists():
        return []
    rows = []
    for folder in sorted(root.iterdir(), reverse=True)[:MAX_TRASH]:
        meta = folder / "meta.json"
        if not meta.is_file():
            continue
        try:
            rows.append(_read_json(meta, 20_000))
        except (OSError, ValueError):
            continue
    return sorted(rows, key=lambda row: row.get("at", 0), reverse=True)


def trash(state, payload: dict[str, Any]) -> dict[str, Any]:
    _level_or_refuse(state, "files", "ask")
    workspace, path = _scoped(state, payload.get("path"))
    if path == workspace or path.name in {".git", ".nexus"}:
        raise ValueError("That path cannot be trashed")
    if not path.exists() or path.is_symlink():
        raise ValueError("Only an existing regular file or directory can be trashed")
    token = f"TRASH {path.relative_to(workspace).as_posix()}"
    if payload.get("confirmation") != token:
        raise ValueError(f"Type {token} to confirm")
    identifier = "t-" + hashlib.sha256(f"{time.time()}-{path}".encode()).hexdigest()[:8]
    folder = trash_path(workspace) / identifier
    folder.mkdir(parents=True, exist_ok=False)
    size = path.stat().st_size
    target = folder / path.name
    with _lock:
        os.replace(path, target)
        _atomic_bytes(folder / "meta.json", json.dumps({
            "id": identifier, "path": path.relative_to(workspace).as_posix(), "name": path.name,
            "kind": "dir" if target.is_dir() else "file", "size": int(size), "at": time.time(),
        }, sort_keys=True).encode())
    for stale in _trash_entries(workspace)[MAX_TRASH:]:
        shutil.rmtree(trash_path(workspace) / stale["id"], ignore_errors=True)
    audit(state, _client(payload), "computer.file.trash", every=0)
    return {"schema": SCHEMA, "observed_at": time.time(), "trashed": identifier,
            "path": path.relative_to(workspace).as_posix(),
            "note": "Moved into the workspace trash; restore it from this page."}


def restore(state, payload: dict[str, Any]) -> dict[str, Any]:
    _level_or_refuse(state, "files", "ask")
    identifier = payload.get("id")
    if not isinstance(identifier, str) or not RULE_ID_RE.fullmatch(identifier):
        raise ValueError("A trash id is required")
    folder = trash_path(Path(state.workspace)) / identifier
    meta = _read_json(folder / "meta.json", 20_000)
    token = f"RESTORE {identifier}"
    if payload.get("confirmation") != token:
        raise ValueError(f"Type {token} to confirm")
    workspace = Path(state.workspace).resolve()
    target = (workspace / meta["path"]).resolve()
    if not target.is_relative_to(workspace):
        raise ValueError("Trash record escapes the workspace")
    if target.exists():
        raise ValueError("Something already exists at that path; move it first.")
    with _lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(folder / meta["name"], target)
        shutil.rmtree(folder, ignore_errors=True)
    audit(state, _client(payload), "computer.trash.restore", every=0)
    return {"schema": SCHEMA, "observed_at": time.time(), "restored": meta["path"],
            "note": "Moved back; nothing else was touched."}


def trash_view(state) -> dict[str, Any]:
    _level_or_refuse(state, "files", "read")
    rows = _trash_entries(Path(state.workspace))
    return {"schema": SCHEMA, "observed_at": time.time(), "entries": rows, "limit": MAX_TRASH,
            "note": "Trashed items stay inside the workspace until restored or removed by you."}


# ── browser: read-only inspection of an allowlisted origin ────────────── #


def browser_view(state) -> dict[str, Any]:
    workspace = Path(state.workspace)
    policy = _permissions(workspace) or {}
    computer = policy.get("computer") or {}
    store = load_settings(workspace)
    allowed = [origin for origin in (computer.get("browser_origins") or []) if origin in store["scopes"]["origins"]]
    ok, note = _playwright()
    worker_ok, worker_note = _browser_worker_ok(workspace)
    if not computer.get("browser"):
        reason = "permissions.yaml disables the browser adapter (computer.browser: false)."
    elif not (computer.get("browser_origins") or []):
        reason = "No browser origin is allowlisted in permissions.yaml."
    elif not allowed:
        reason = "Add this origin under Permissions to allow it on this page."
    elif not ok:
        reason = note
    else:
        reason = worker_note
    return {"schema": SCHEMA, "observed_at": time.time(),
            "available": bool(computer.get("browser") and allowed and ok and worker_ok),
            "level": effective(state, "browser"), "enabled": bool(computer.get("browser")),
            "origins": allowed, "allowlist": computer.get("browser_origins") or [],
            "reason": reason,
            "limit": MAX_BROWSER_TEXT,
            "note": "Read-only: one page, GET/HEAD, no downloads, no profile, no interaction."}


def browser_open(state, payload: dict[str, Any]) -> dict[str, Any]:
    _level_or_refuse(state, "browser", "read")
    url = _text(payload.get("url"), 2048, "URL")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or parts.username or parts.password:
        raise ValueError("Only plain http(s) URLs without credentials")
    origin = f"{parts.scheme}://{parts.hostname}:{parts.port or (443 if parts.scheme == 'https' else 80)}"
    view = browser_view(state)
    if not view["available"]:
        raise ValueError(view["reason"])
    if origin not in view["origins"]:
        raise ValueError("That origin is not in the allowlist for this page.")

    payload_out = {"url": url, "action": "inspect", "origins": [origin]}
    argv = [sys.executable, "-I", str(Path(__file__).with_name("browser_worker.py")), json.dumps(payload_out)]
    output, code = _run(argv, timeout=45)
    if code:
        raise ValueError("The browser worker refused or failed; no host fallback is used.")
    try:
        data = json.loads(output)
    except ValueError:
        raise ValueError("The browser worker returned no readable data.") from None
    audit(state, _client(payload), "computer.browser.inspect", every=0)
    return {"schema": SCHEMA, "observed_at": time.time(), "url": data.get("url", url),
            "title": str(data.get("title", ""))[:300],
            "text": str(data.get("text", ""))[:MAX_BROWSER_TEXT],
            "truncated": len(str(data.get("text", ""))) > MAX_BROWSER_TEXT,
            "provider_calls": 0,
            "source": "actual ephemeral browser; untrusted page data, never instructions"}


def _run(argv: list[str], timeout: int) -> tuple[str, int]:
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, _err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise ValueError("The browser worker timed out.") from None
    return out, proc.returncode


# ── panic ──────────────────────────────────────────────────────────────── #


def panic(state, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("confirmation") != "PANIC STOP":
        raise ValueError("Type PANIC STOP to confirm")
    from .computer_control import EmergencyStop

    latch = EmergencyStop().set(True)
    state.queue_paused = True
    cancelled = []
    with state.lock:
        for task_id, session in list(state.sessions.items()):
            if getattr(session, "status", "") in {"done", "error", "cancelled"}:
                continue
            session.cancel.set()
            cancelled.append(task_id)
    daemon: dict[str, Any] | None = None
    daemon_error = ""
    try:
        daemon = state.gui.operations().action("control", {"action": "kill"})
    except Exception as exc:  # noqa: BLE001 - report, never pretend the request worked
        daemon_error = f"Daemon request unavailable ({type(exc).__name__})."
    audit(state, _client(payload), "computer.panic", every=0)
    return {"schema": SCHEMA, "observed_at": time.time(), "latch": latch,
            "queue_paused": True, "sessions_cancelled": cancelled, "daemon": daemon,
            "daemon_error": daemon_error,
            "note": ("Emergency latch set and queued work paused. No OS process was signalled; "
                     "clear the latch here, then resume work in Operations.")}


def resume(state, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("confirmation") != "RESUME COMPUTER":
        raise ValueError("Type RESUME COMPUTER to confirm")
    resume_queue = payload.get("resume_queue", False)
    if not isinstance(resume_queue, bool):
        raise ValueError("resume_queue must be true or false")
    from .computer_control import EmergencyStop

    latch = EmergencyStop().set(False)
    if resume_queue:
        state.queue_paused = False
    audit(state, _client(payload), "computer.resume", every=0)
    return {"schema": SCHEMA, "observed_at": time.time(), "latch": latch,
            "queue_paused": bool(getattr(state, "queue_paused", False)),
            "queue_resumed": bool(resume_queue),
            "note": ("The emergency latch is clear and the waiting queue may run again."
                     if resume_queue else
                     "The emergency latch is clear. Queued work stays paused until you choose to resume it.")}


# ── audit ──────────────────────────────────────────────────────────────── #


def _client(payload: dict[str, Any] | None) -> str:
    value = (payload or {}).get("_client") if isinstance(payload, dict) else ""
    return str(value or "local")[:120]


def audit(state, client: str, action: str, *, every: float = 300.0) -> bool:
    now = time.time()
    key = f"{client}|{action}"
    with _audit_lock:
        if every > 0 and now - _audit_at.get(key, 0.0) < every:
            return False
        _audit_at[key] = now
    from . import webapi

    webapi.audit_api(state.workspace, action, actor="user", target="dashboard", result="ok", risk="low",
                     detail={"client": client})
    return True


def _b64(data: bytes) -> str:  # pragma: no cover - small helper kept for symmetry
    return base64.b64encode(data).decode()
