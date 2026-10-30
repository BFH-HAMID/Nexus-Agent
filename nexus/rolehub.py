"""Role Hub: a compact index over the whole roster, one role's detail, a
versioned editor trail, quality checks and an offline route preview.

The roster is big on purpose (the repo ships ~1000 roles), so nothing here sends
the whole thing to a browser:

``GET /api/roles/index``   one page of rows (50 by default) plus roster stats
``GET /api/roles/detail``  one role with persona, keywords and provenance
``GET /api/roles/history`` versions of the GUI-managed role layer
``GET /api/roles/diff``    one role (or the whole layer) against a version
``POST /api/roles/route``  keyword routing preview - no model is called
``POST /api/roles/export`` selected roles as YAML
``POST /api/roles/restore`` restore a recorded version (typed confirmation)

Editing itself stays on the existing reviewed path
(``/api/workbench/configuration``): the same revision check, the same typed
``SAVE ROLE <name>`` confirmation, the same atomic write with rollback. This
module adds the read surfaces and records the *previous* file content before
each edit, so a change can be inspected and undone.

Recorded versions live in ``<workspace>/.nexus/rolehub/`` (runtime state, never
committed) and are bounded to the newest 20.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml

SCHEMA = 1
PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
ROLE_FILE = "roles.d/zz-gui-managed.yaml"
STORE_DIR = ".nexus/rolehub"
MAX_VERSIONS = 20
MAX_EXPORT = 60
MAX_DIFF_LINES = 400
SEARCH_FIELDS = ("name", "title", "category", "agent")
VERSION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}(?:-[0-9]+)?$")

_audit_lock = threading.Lock()
_audit_at: dict[str, float] = {}


# ── small helpers ──────────────────────────────────────────────────────── #


def _int(value: Any, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError("Text is required")
    value = value.strip()
    if not value or len(value) > limit or "\x00" in value:
        raise ValueError(f"Text must be 1-{limit} characters")
    return value


def _mask_values(state) -> list[str]:
    """Workspace secrets that must never reach the browser (token included)."""
    from .redaction import secrets

    values = [str(getattr(state, "token", "") or "")] + list(secrets(getattr(state, "workspace", None)))
    return [value for value in values if isinstance(value, str) and len(value) >= 8]


def _clean(state, value: Any) -> Any:
    """Mask secrets in every string the browser will see."""
    from .redaction import mask

    values = _mask_values(state)
    if not values:
        return value
    if isinstance(value, str):
        return mask(value, values)
    if isinstance(value, list):
        return [_clean(state, item) for item in value]
    if isinstance(value, dict):
        return {key: _clean(state, item) for key, item in value.items()}
    return value


def role_file(workspace: Path) -> Path:
    return Path(workspace) / ROLE_FILE


def store(workspace: Path) -> Path:
    return Path(workspace) / STORE_DIR


def _read_layer(path: Path) -> tuple[dict[str, Any], str | None]:
    """The managed overlay, or an explanation of why it could not be read."""
    if not path.exists():
        return {}, None
    try:
        if not path.is_file() or path.stat().st_size > 2_000_000:
            return {}, "Role overlay is not a bounded file."
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return {}, f"Role overlay unreadable ({type(exc).__name__})."
    if not isinstance(data, dict) or not isinstance(data.get("roles", {}), dict):
        return {}, "Role overlay is not a roles mapping."
    return data, None


def override_names(workspace: Path) -> set[str]:
    data, error = _read_layer(role_file(workspace))
    return set() if error else set(data.get("roles") or {})


def layer_updated_at(workspace: Path) -> float | None:
    path = role_file(workspace)
    try:
        return path.stat().st_mtime if path.is_file() else None
    except OSError:
        return None


def _source_label(role) -> str:
    name = Path(str(getattr(role, "source", "") or "")).name
    if name == Path(ROLE_FILE).name:
        return "workspace override"
    return name or "built-in"


def _persona(role) -> str:
    return str(getattr(role, "persona", "") or "")


def _instructions(role) -> str:
    return str(getattr(role, "extra_instructions", "") or "")


# ── version store ──────────────────────────────────────────────────────── #


def _index_path(workspace: Path) -> Path:
    return store(workspace) / "index.jsonl"


def versions(workspace: Path, limit: int = MAX_VERSIONS) -> list[dict[str, Any]]:
    """Newest first; a damaged index line is skipped rather than fatal."""
    path = _index_path(workspace)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-200:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and VERSION_RE.match(str(row.get("id", ""))):
                rows.append(row)
    except OSError:
        return []
    rows.sort(key=lambda row: float(row.get("at") or 0), reverse=True)
    return rows[:limit]


def record(workspace: Path, *, before: bytes | None, reason: str) -> dict[str, Any] | None:
    """Keep the content that an edit is about to replace.

    Called by the reviewed configuration path *after* a successful write. A
    first-time creation has nothing to keep, so it records nothing.
    """
    if not before:
        return None
    folder = store(workspace) / "history"
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(before).hexdigest()[:6]
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    ident, suffix = f"{stamp}-{digest}", 1
    while (folder / f"{ident}.yaml").exists():
        suffix += 1
        ident = f"{stamp}-{digest}-{suffix}"
    path = folder / f"{ident}.yaml"
    path.write_bytes(before)
    row = {"id": ident, "at": time.time(), "sha256": hashlib.sha256(before).hexdigest(),
           "bytes": len(before), "reason": str(reason)[:80], "file": ROLE_FILE}
    with (store(workspace) / "lock").open("a", encoding="utf-8") as handle:
        handle.write("")
    index = _index_path(workspace)
    with index.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    keep = {item["id"] for item in versions(workspace)}
    for item in folder.glob("*.yaml"):
        if item.stem not in keep:
            item.unlink(missing_ok=True)
    return row


def _version_bytes(workspace: Path, ident: str) -> bytes:
    if not VERSION_RE.match(ident):
        raise ValueError("Unknown version identifier")
    known = {row["id"] for row in versions(workspace, limit=200)}
    if ident not in known:
        raise ValueError("Unknown version; refresh the history list")
    path = store(workspace) / "history" / f"{ident}.yaml"
    if not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError("Version content is missing or oversized")
    return path.read_bytes()


# ── projections ────────────────────────────────────────────────────────── #


def _row(role, overrides: set[str], updated_at: float | None) -> dict[str, Any]:
    name = str(role.name)
    return {
        "name": name,
        "title": str(getattr(role, "title", "") or ""),
        "category": str(getattr(role, "category", "") or "general"),
        "agent": str(getattr(role, "agent", "") or ""),
        "keywords": len(getattr(role, "keywords", []) or []),
        "aliases": len(getattr(role, "aliases", []) or []),
        "tools": None if getattr(role, "tools", None) is None else len(role.tools),
        "persona_chars": len(_persona(role)),
        "model": getattr(role, "model", None),
        "source": _source_label(role),
        "override": name in overrides,
        "updated_at": updated_at if name in overrides else None,
    }


def _stats(state, rows: list[dict[str, Any]]) -> dict[str, Any]:
    cfg = state.config
    categories: dict[str, int] = {}
    agents: dict[str, int] = {}
    for row in rows:
        categories[row["category"]] = categories.get(row["category"], 0) + 1
        key = row["agent"] or "(default)"
        agents[key] = agents.get(key, 0) + 1
    return {
        "total": len(rows),
        "overridden": sum(1 for row in rows if row["override"]),
        "without_keywords": sum(1 for row in rows if not row["keywords"]),
        "without_persona": sum(1 for row in rows if not row["persona_chars"]),
        "categories": len(categories),
        "agents": len(agents),
        "unknown_agent": sum(1 for row in rows if row["agent"] and row["agent"] not in cfg.agents),
        "by_category": dict(sorted(categories.items(), key=lambda pair: (-pair[1], pair[0]))[:12]),
        "by_agent": dict(sorted(agents.items(), key=lambda pair: (-pair[1], pair[0]))),
        "teams": len(cfg.pipelines),
        "default_agent": cfg.default_agent,
    }


def index(state, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    query = query or {}

    def arg(name: str) -> str:
        values = query.get(name) or [""]
        return str(values[0] if values else "")[:200]

    needle = arg("q").strip().lower()
    category, agent, source = arg("category"), arg("agent"), arg("source")
    sort = arg("sort") or "name"
    if sort not in {"name", "title", "category", "agent", "keywords", "persona", "updated"}:
        raise ValueError("Unknown sort field")
    descending = arg("dir") == "desc"
    page = max(1, _int(arg("page") or 1, 1))
    size = min(MAX_PAGE_SIZE, max(10, _int(arg("size") or PAGE_SIZE, PAGE_SIZE)))

    workspace = Path(state.workspace)
    overrides = override_names(workspace)
    updated_at = layer_updated_at(workspace)
    every_row = [_row(role, overrides, updated_at) for role in state.config.roles.roles.values()]
    stats = _stats(state, every_row)

    rows = every_row
    if category:
        rows = [row for row in rows if row["category"] == category]
    if agent:
        rows = [row for row in rows if (row["agent"] or "(default)") == agent]
    if source:
        rows = [row for row in rows if row["source"] == source]
    if needle:
        roles = state.config.roles.roles

        def hit(row: dict[str, Any]) -> bool:
            role = roles[row["name"]]
            haystack = " ".join([row["name"], row["title"], row["category"], str(_persona(role))[:4000],
                                 " ".join(k.text for k in (getattr(role, "keywords", []) or [])),
                                 " ".join(getattr(role, "aliases", []) or [])]).lower()
            return needle in haystack

        rows = [row for row in rows if hit(row)]

    keys = {"persona": "persona_chars", "updated": "updated_at"}.get(sort, sort)
    if keys == "updated_at":
        rows.sort(key=lambda row: (row["updated_at"] or 0, row["name"]), reverse=descending)
    elif keys in {"persona_chars", "keywords"}:
        rows.sort(key=lambda row: (row.get(keys) or 0, row["name"]), reverse=descending)
    else:
        rows.sort(key=lambda row: (str(row.get(keys) or ""), row["name"]), reverse=descending)
    pages = max(1, math.ceil(len(rows) / size))
    page = min(page, pages)
    return {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "total": len(every_row),
        "matched": len(rows),
        "page": page,
        "page_size": size,
        "pages": pages,
        "sort": sort,
        "dir": "desc" if descending else "asc",
        "stats": stats,
        "filters": {
            "categories": sorted({row["category"] for row in every_row}),
            "agents": sorted({row["agent"] or "(default)" for row in every_row}),
            "sources": sorted({row["source"] for row in every_row}),
        },
        "roles": rows[(page - 1) * size: page * size],
        "note": "Titles, sizes and counts only. Open a role for its prompt text.",
    }


def _resolve(state, name: str):
    clean = str(name or "").strip().lower()
    if not clean or len(clean) > 64:
        raise ValueError("A role name is required")
    role = state.config.roles.roles.get(clean)
    if role is None:
        for candidate in state.config.roles.roles.values():
            if clean in getattr(candidate, "aliases", []):
                role = candidate
                break
    if role is None:
        raise ValueError(f"Unknown role: {clean}")
    return role


def detail(state, name: str) -> dict[str, Any]:
    role = _resolve(state, name)
    workspace = Path(state.workspace)
    overrides = override_names(workspace)
    cfg = state.config
    teams = [team for team, pipeline in cfg.pipelines.items()
             if any(str(step.get("role") or "") in {role.name, *getattr(role, "aliases", [])} for step in pipeline.steps)]
    try:
        from .gui_config import Configuration
        revision = Configuration(state.workbench).revision()
    except Exception:  # noqa: BLE001 - the hub still shows the role without an edit token
        revision = None
    data = {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "name": role.name,
        "title": getattr(role, "title", ""),
        "category": getattr(role, "category", "general"),
        "agent": getattr(role, "agent", ""),
        "persona": _persona(role),
        "instructions": _instructions(role),
        "keywords": [{"text": keyword.text, "weight": keyword.weight} for keyword in getattr(role, "keywords", []) or []],
        "aliases": list(getattr(role, "aliases", []) or []),
        "tools": getattr(role, "tools", None),
        "focus": list(getattr(role, "focus", []) or []),
        "model": getattr(role, "model", None),
        "temperature": getattr(role, "temperature", None),
        "source": _source_label(role),
        "override": role.name in overrides,
        "editable": not bool(getattr(state, "demo", False)),
        "removable": role.name in overrides,
        "used_by": {"teams": teams, "agents": [getattr(role, "agent", "")] if getattr(role, "agent", "") else [],
                    "is_default_agent": bool(getattr(role, "agent", "")) and role.agent == cfg.default_agent},
        "revision": revision,
        "keywords_hint": "Weight 1-5; a role with no keyword can only be picked by name or the classifier.",
    }
    return _clean(state, data)


def quality(state) -> dict[str, Any]:
    """Static checks over the roster. Read-only and cheap; no model calls."""
    cfg = state.config
    roles = cfg.roles.roles
    issues: list[dict[str, Any]] = []
    seen_aliases: dict[str, str] = {}

    def flag(severity: str, code: str, name: str, message: str) -> None:
        issues.append({"severity": severity, "code": code, "name": name, "message": message})

    for name, role in roles.items():
        persona = _persona(role).strip()
        if not persona:
            flag("high", "missing-persona", name, "No persona: the role sends no instructions.")
        elif len(persona) < 40:
            flag("low", "short-persona", name, f"Persona is only {len(persona)} characters.")
        elif len(persona) > 12000:
            flag("low", "long-persona", name, f"Persona is {len(persona)} characters; it costs tokens on every run.")
        if role.agent and role.agent not in cfg.agents:
            flag("high", "unknown-agent", name, f"Agent '{role.agent}' is not configured.")
        if not getattr(role, "keywords", None):
            flag("info", "no-keywords", name, "No keywords: automatic routing cannot pick this role.")
        for alias in getattr(role, "aliases", []) or []:
            if alias in roles and alias != name:
                flag("high", "alias-conflict", name, f"Alias '{alias}' is also a role name.")
            elif alias in seen_aliases and seen_aliases[alias] != name:
                flag("high", "alias-duplicate", name, f"Alias '{alias}' is also used by '{seen_aliases[alias]}'.")
            else:
                seen_aliases[alias] = name
    order = {"high": 0, "low": 1, "info": 2}
    issues.sort(key=lambda row: (order.get(row["severity"], 3), row["name"], row["code"]))
    counts = {level: sum(1 for row in issues if row["severity"] == level) for level in ("high", "low", "info")}
    return {"schema": SCHEMA, "observed_at": time.time(), "total_roles": len(roles),
            "issues": issues[:400], "truncated": len(issues) > 400, "counts": counts,
            "checked": ["missing persona", "unknown agent", "no keywords", "alias conflicts"],
            "not_checked": ["prompt quality", "model fit", "cost of a run"]}


def route(state, payload: dict[str, Any]) -> dict[str, Any]:
    """Keyword routing preview. No provider is constructed, so no model is called."""
    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required")
    task = _text(payload.get("task"), 2000)
    from .router import Router

    router = Router(state.config, providers=None)
    decision = router.keyword_route(task)
    scored = []
    for role in state.config.roles.roles.values():
        score, hits = role.keyword_score(task.lower())
        if score > 0:
            scored.append({"role": role.name, "score": round(score, 2), "hits": hits[:8]})
    scored.sort(key=lambda row: (-row["score"], row["role"]))
    threshold = float(state.config.settings.router_min_score)
    return {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "task": task,
        "provider_calls": 0,
        "threshold": threshold,
        "decision": None if decision is None else {
            "role": decision.role.name, "method": decision.method,
            "score": round(float(decision.score), 2), "hits": decision.hits[:10],
            "note": decision.note,
        },
        "candidates": scored[:8],
        "note": ("Offline keyword preview: no model was called and nothing ran. "
                 "A tie or an empty match is resolved by the classifier at run time (Prompt 4 owns that lab)."),
    }


def export(state, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required")
    names = payload.get("names")
    if not isinstance(names, list) or not names or len(names) > MAX_EXPORT:
        raise ValueError(f"Choose 1-{MAX_EXPORT} roles to export")
    selected: dict[str, Any] = {}
    for raw in names:
        role = _resolve(state, str(raw))
        selected[role.name] = {
            "title": role.title, "category": role.category, "agent": role.agent,
            "persona": _persona(role),
            **({"instructions": _instructions(role)} if _instructions(role) else {}),
            "keywords": [{"text": k.text, "weight": k.weight} for k in getattr(role, "keywords", []) or []],
            "aliases": list(getattr(role, "aliases", []) or []),
            **( {"tools": list(role.tools)} if getattr(role, "tools", None) is not None else {}),
            **({"focus": list(role.focus)} if getattr(role, "focus", None) else {}),
            **({"model": role.model} if getattr(role, "model", None) else {}),
            **({"temperature": role.temperature} if getattr(role, "temperature", None) is not None else {}),
        }
    text = yaml.safe_dump({"roles": selected}, allow_unicode=True, sort_keys=False)
    text = str(_clean(state, text))
    if len(text.encode()) > 2_000_000:
        raise ValueError("Selection is too large to export")
    return {"schema": SCHEMA, "count": len(selected), "bytes": len(text.encode()),
            "filename": f"roles-export-{time.strftime('%Y%m%d-%H%M%S')}.yaml", "yaml": text}


def diff(state, query: dict[str, list[str]]) -> dict[str, Any]:
    def arg(name: str) -> str:
        return str((query.get(name) or [""])[0])[:80]

    ident, name = arg("version"), arg("name")
    before = _version_bytes(Path(state.workspace), ident)
    current = role_file(Path(state.workspace))
    after = current.read_bytes() if current.is_file() else b""
    old_doc = yaml.safe_load(before.decode("utf-8", "replace")) or {}
    new_doc = yaml.safe_load(after.decode("utf-8", "replace")) or {}
    old_roles = (old_doc.get("roles") or {}) if isinstance(old_doc, dict) else {}
    new_roles = (new_doc.get("roles") or {}) if isinstance(new_doc, dict) else {}

    def render(section: dict[str, Any]) -> str:
        data = section.get(name) if name else None
        if name and data is None:
            return ""
        return yaml.safe_dump({name: data} if name else section, allow_unicode=True, sort_keys=True, width=100)

    left, right = render(old_roles), render(new_roles)
    lines = list(difflib.unified_diff(left.splitlines(), right.splitlines(),
                                     fromfile=f"{ident}/{name or 'role layer'}",
                                     tofile=f"current/{name or 'role layer'}", lineterm=""))
    truncated = len(lines) > MAX_DIFF_LINES
    changed = any(line[:1] in {"+", "-"} and not line.startswith(("+++", "---")) for line in lines)
    return {"schema": SCHEMA, "observed_at": time.time(), "version": ident, "name": name or None,
            "changed": changed, "truncated": truncated,
            "lines": [str(_clean(state, line)) for line in lines[:MAX_DIFF_LINES]]}


def restore(state, payload: dict[str, Any]) -> dict[str, Any]:
    """Put a recorded version back, through the same atomic write as an edit."""
    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required")
    if getattr(state, "demo", False):
        raise ValueError("Role history is read-only in demo mode")
    ident = str(payload.get("version") or "")
    if payload.get("confirmation") != f"RESTORE ROLES {ident}":
        raise ValueError("Type the exact confirmation to restore a version")
    snapshot = _version_bytes(Path(state.workspace), ident)
    try:
        doc = yaml.safe_load(snapshot.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError):
        raise ValueError("Version is not readable YAML") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("roles", {}), dict):
        raise ValueError("Version is not a roles mapping")

    from .config import clear_yaml_cache, load_config
    from .gui_config import Configuration

    path = role_file(Path(state.workspace))
    with state.workbench.lock:
        if payload.get("revision") not in {None, Configuration(state.workbench).revision()}:
            raise ValueError("Configuration changed; reload before restoring")
        before = path.read_bytes() if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".role-restore-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(snapshot)
            os.replace(temporary, path)
            clear_yaml_cache()
            try:
                updated = load_config(state.workspace)
                state.config = updated
            except Exception:  # noqa: BLE001 - put the previous layer back
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(before)
                clear_yaml_cache()
                raise ValueError("Role file rejected; previous content restored") from None
        finally:
            Path(temporary).unlink(missing_ok=True)
        record(Path(state.workspace), before=before, reason=f"restore {ident}")
    return {"schema": SCHEMA, "restored": ident, "revision": Configuration(state.workbench).revision(),
            "roles": len(doc.get("roles") or {}),
            "applies_to": "New runs; existing approvals, permissions and local-only policy remain enforced"}


# ── audit ──────────────────────────────────────────────────────────────── #


def audit(state, client: str, action: str, *, every: float = 300.0) -> bool:
    """One redacted audit line per client per window (reads poll, so sample)."""
    if every <= 0:
        return _write_audit(state, action, client)
    now = time.time()
    key = f"{client}|{action}"
    with _audit_lock:
        if now - _audit_at.get(key, 0.0) < every:
            return False
        _audit_at[key] = now
    return _write_audit(state, action, client)


def _write_audit(state, action: str, client: str) -> bool:
    from . import webapi

    webapi.audit_api(state.workspace, action, actor="user", target="dashboard", result="ok", risk="low",
                     detail={"client": client})
    return True
