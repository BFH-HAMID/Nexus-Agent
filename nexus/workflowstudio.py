"""Workflow Studio: a real builder, version diffs and a scheduler that survives restarts.

The workspace already had a validated DAG engine (`nexus/studio_workflows.py`):
immutable versions, role/agent routing, conditions, budgets, reviewer phases and
evidence-bearing runs. What it did not have was a way to *see* a workflow, edit
one without hand-writing JSON, compare versions or runs, or run on a schedule
that still exists after the process restarts.

This module is the service layer for that page. It adds no second executor:

* saving a version calls `Workflows.save` (same validation, same immutability);
* running calls `Workflows.run` (same typed token, same idle check, same node
  engine) and cancelling calls `Workflows.cancel`;
* scheduling only decides *when* that existing call is made, and every schedule
  is itself an explicitly confirmed, visible, revocable record.

Guarantees that shape the scheduler:

* **Review-first.** A schedule is created with a typed token, pinned to one
  immutable version, and can be disabled or removed at any time. A missed
  window is recorded as missed and skipped — the server never "catches up" by
  starting work nobody asked for at that moment.
* **One runner.** A per-workspace file lock means two servers on the same
  workspace cannot both fire the same schedule.
* **Honest state.** Every skip carries its reason (latched emergency stop, demo
  mode, paused queue, another run in progress, missing version) and the page
  shows it instead of a fake success.

Runtime state lives under `<workspace>/.nexus/workflows/` and is never committed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = 1
STORE_DIR = ".nexus/workflows"
SCHEDULES_FILE = "schedules.json"
HISTORY_DIR = "history"
LOCK_FILE = "scheduler.lock"

TRIGGERS = ("manual", "interval", "cron")
MIN_INTERVAL = 5
MAX_INTERVAL = 10_080            # one week, in minutes
MAX_SCHEDULES = 20
MAX_RUNS_KEPT = 20
MAX_HISTORY = 20
PREVIEW_COUNT = 5
MISSED_GRACE = 120.0             # a window older than this was missed, not deferred
DEFER_LIMIT = 1_800.0            # give up on a blocked window after 30 minutes
MAX_ITEMS = 60

SAVE_TOKEN = "SAVE WORKFLOW"
SCHEDULE_TOKEN = "SCHEDULE WORKFLOW"
RUN_TOKEN = "RUN REVIEWED WORKFLOW"

_SCHEDULE_RE = re.compile(r"^w-[0-9a-f]{8}$")
_CRON_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),
)


# ── store ──────────────────────────────────────────────────────────────── #


def _store(workspace: Path) -> Path:
    return Path(workspace) / STORE_DIR


def schedules_path(workspace: Path) -> Path:
    return _store(workspace) / SCHEDULES_FILE


def history_path(workspace: Path) -> Path:
    return _store(workspace) / HISTORY_DIR


def _atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / ("." + uuid.uuid4().hex + ".tmp")
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path, limit: int = 400_000) -> Any:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("The schedule store must be a bounded regular file")
    return json.loads(path.read_text(encoding="utf-8"))


def revision_of(schedules: list[dict[str, Any]]) -> str:
    payload = json.dumps(schedules, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _compile_schedule(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Invalid schedule record")
    keys = {"id", "family", "workflow_id", "version", "name", "trigger", "enabled", "created",
            "updated", "next_run_at", "last", "runs", "missed", "last_skip"}
    if set(raw) - keys:
        raise ValueError("Unknown schedule field")
    trigger = raw.get("trigger")
    if not isinstance(trigger, dict):
        raise ValueError("Invalid trigger")
    kind = trigger.get("kind")
    if kind not in TRIGGERS:
        raise ValueError("Unknown trigger kind")
    compiled = {"kind": kind}
    if kind == "interval":
        compiled["minutes"] = _interval_minutes(trigger.get("minutes"))
    elif kind == "cron":
        compiled["cron"] = cron_expression(trigger.get("cron"))
    return {
        "id": str(raw.get("id") or ""),
        "family": str(raw.get("family") or ""),
        "workflow_id": str(raw.get("workflow_id") or ""),
        "version": raw.get("version"),
        "name": str(raw.get("name") or "")[:120],
        "trigger": compiled,
        "enabled": bool(raw.get("enabled")),
        "created": raw.get("created") or time.time(),
        "updated": raw.get("updated") or time.time(),
        "next_run_at": raw.get("next_run_at"),
        "last": raw.get("last") if isinstance(raw.get("last"), dict) else None,
        "runs": [str(item) for item in (raw.get("runs") or [])][:MAX_RUNS_KEPT],
        "missed": max(0, int(raw.get("missed") or 0)),
        "last_skip": raw.get("last_skip") if isinstance(raw.get("last_skip"), dict) else None,
    }


def load_store(workspace: Path) -> dict[str, Any]:
    """Read the schedules; a damaged file reads as empty with an explicit error."""
    path = schedules_path(workspace)
    error = ""
    rows: list[dict[str, Any]] = []
    last_tick = 0.0
    try:
        raw = _read_json(path)
        if isinstance(raw, dict):
            if raw.get("schema") != SCHEMA:
                raise ValueError("Unsupported schedule store schema")
            rows = [_compile_schedule(item) for item in (raw.get("schedules") or [])][:MAX_SCHEDULES]
            last_tick = float(raw.get("last_tick_at") or 0.0)
        elif raw is not None:
            raise ValueError("Schedule store must be an object")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        error = str(exc)[:300]
        rows, last_tick = [], 0.0
    return {"schema": SCHEMA, "schedules": rows, "revision": revision_of(rows),
            "last_tick_at": last_tick, "error": error}


def _write_store(workspace: Path, rows: list[dict[str, Any]], last_tick: float) -> dict[str, Any]:
    payload = {"schema": SCHEMA, "schedules": rows, "last_tick_at": last_tick,
               "updated_at": time.time()}
    _atomic_bytes(schedules_path(workspace), json.dumps(payload, indent=1, sort_keys=True).encode())
    return {"schema": SCHEMA, "schedules": rows, "revision": revision_of(rows),
            "last_tick_at": last_tick, "error": ""}


def _history_snapshot(workspace: Path, rows: list[dict[str, Any]], reason: str) -> str | None:
    if not rows:
        return None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"{stamp}-{revision_of(rows)[:8]}.json"
    directory = history_path(workspace)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_bytes(directory / name, json.dumps({"reason": reason, "schedules": rows}, indent=1).encode())
    except OSError:
        return None
    keep = sorted(directory.glob("*.json"))
    for stale in keep[:-MAX_HISTORY]:
        stale.unlink(missing_ok=True)
    return name


# ── trigger validation and next-run maths ──────────────────────────────── #


def _interval_minutes(value: Any) -> int:
    if type(value) is not int or not MIN_INTERVAL <= value <= MAX_INTERVAL:
        raise ValueError(f"Interval must be {MIN_INTERVAL}–{MAX_INTERVAL} minutes")
    return value


def _cron_field(text: str, low: int, high: int, label: str) -> set[int]:
    values: set[int] = set()
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"Empty {label} field")
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or not 1 <= int(raw_step) <= high:
                raise ValueError(f"Invalid step in {label}")
            step = int(raw_step)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            first, _, last = part.partition("-")
            if not first.isdigit() or not last.isdigit():
                raise ValueError(f"Invalid range in {label}")
            start, end = int(first), int(last)
        else:
            if not part.isdigit():
                raise ValueError(f"Invalid value in {label}")
            start = end = int(part)
        if start > end or not low <= start <= high or not low <= end <= high:
            raise ValueError(f"{label} must be {low}–{high}")
        values.update(range(start, end + 1, step))
    return values


def cron_expression(value: Any) -> str:
    """Validate a five-field cron and return it normalised (whitespace-collapsed)."""
    if not isinstance(value, str):
        raise ValueError("A cron expression is required")
    text = " ".join(value.split())
    if not text or len(text) > 120:
        raise ValueError("A cron expression of at most 120 characters is required")
    fields = text.split(" ")
    if len(fields) != 5:
        raise ValueError("Cron needs five fields: minute hour day month weekday")
    for text_field, (label, low, high) in zip(fields, _CRON_FIELDS, strict=True):
        _cron_field(text_field, low, high, label)
    return text


def _parse_cron(text: str) -> tuple[set[int], ...]:
    fields = text.split(" ")
    out = []
    for text_field, (label, low, high) in zip(fields, _CRON_FIELDS, strict=True):
        values = _cron_field(text_field, low, high, label)
        if label == "day of week" and 7 in values:
            values = (values - {7}) | {0}
        out.append(values)
    return tuple(out)


def _day_matches(day: float, minutes: set[int], hours: set[int], dom: set[int], month: set[int],
                 dow: set[int], raw_dom: str, raw_dow: str) -> bool:
    stamp = time.gmtime(day)
    if stamp.tm_mon not in month:
        return False
    dom_restricted = raw_dom != "*"
    dow_restricted = raw_dow != "*"
    dom_ok = stamp.tm_mday in dom
    dow_ok = (stamp.tm_wday + 1) % 7 in dow
    if dom_restricted and dow_restricted:
        # Vixie cron: when both day fields are restricted, either may match.
        return dom_ok or dow_ok
    if dom_restricted:
        return dom_ok
    if dow_restricted:
        return dow_ok
    return True


def next_cron(expression: str, after: float) -> float | None:
    """Next matching time strictly after ``after`` (UTC, minute resolution)."""
    minutes, hours, dom, month, dow = _parse_cron(expression)
    raw = expression.split(" ")
    start = math.floor(after / 60) * 60 + 60
    limit = start + 366 * 86_400
    day = math.floor(start / 86_400) * 86_400
    while day < limit:
        if _day_matches(day, minutes, hours, dom, month, dow, raw[2], raw[4]):
            for hour in sorted(hours):
                base = day + hour * 3600
                for minute in sorted(minutes):
                    stamp = base + minute * 60
                    if stamp >= start:
                        return float(stamp)
        day += 86_400
    return None


def next_run(trigger: dict[str, Any], after: float) -> float | None:
    """When this trigger next fires; a trigger it cannot read is refused."""
    if not isinstance(trigger, dict):
        raise ValueError("A trigger object is required")
    kind = trigger.get("kind")
    if kind == "interval":
        return float(math.floor(after) + _interval_minutes(trigger.get("minutes")) * 60)
    if kind == "cron":
        return next_cron(cron_expression(trigger.get("cron")), after)
    if kind in TRIGGERS:
        return None
    raise ValueError("Unknown trigger kind")


def preview_trigger(trigger: dict[str, Any], count: int = PREVIEW_COUNT, *, after: float | None = None) -> list[float]:
    """The next occurrences of a trigger without saving anything."""
    if not isinstance(trigger, dict):
        raise ValueError("A trigger object is required")
    kind = trigger.get("kind")
    if kind == "cron":
        expression = cron_expression(trigger.get("cron"))
        compiled = {"kind": "cron", "cron": expression}
    elif kind == "interval":
        compiled = {"kind": "interval", "minutes": _interval_minutes(trigger.get("minutes"))}
    elif kind == "manual":
        return []
    else:
        raise ValueError("Unknown trigger kind")
    out: list[float] = []
    cursor = float(after if after is not None else time.time())
    for _ in range(max(1, min(int(count), 20))):
        moment = next_run(compiled, cursor)
        if moment is None:
            break
        out.append(moment)
        cursor = moment
    return out


# ── workflow projection ────────────────────────────────────────────────── #


def _studio(state):
    return state.workbench.studio


def _definition(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("definition") or {"name": "", "nodes": []}


def families(state) -> dict[str, Any]:
    """Every workflow family with its immutable versions, newest first."""
    studio = _studio(state)
    rows = [row for row in studio.items("workflow") if isinstance(row.get("definition"), dict)]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("family") or row["id"]), []).append(row)
    out = []
    for family, versions in grouped.items():
        versions.sort(key=lambda row: (row.get("version") or 0, row.get("updated_at") or 0), reverse=True)
        definition = _definition(versions[0])
        out.append({
            "family": family,
            "name": str(definition.get("name") or "")[:120],
            "latest": {"id": versions[0]["id"], "version": versions[0].get("version"),
                       "updated_at": versions[0].get("updated_at")},
            "versions": [{"id": row["id"], "version": row.get("version"),
                          "name": str(_definition(row).get("name") or "")[:120],
                          "nodes": len(_definition(row).get("nodes") or []),
                          "updated_at": row.get("updated_at"),
                          "project_plan_id": _definition(row).get("project_plan_id")} for row in versions],
        })
    out.sort(key=lambda row: (row["latest"]["updated_at"] or 0), reverse=True)
    return {"schema": SCHEMA, "observed_at": time.time(), "families": out[:200],
            "count": len(out), "store": "Studio store: workflow (immutable versions)"}


def _layout(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Left-to-right DAG layers so the canvas can draw real edges."""
    depth: dict[str, int] = {}
    remaining = {node["id"]: set(node.get("depends_on") or []) for node in nodes}
    for _ in range(len(nodes) + 1):
        progress = False
        for ident, deps in list(remaining.items()):
            if all(dep in depth for dep in deps):
                depth[ident] = 1 + max((depth[dep] for dep in deps), default=-1)
                del remaining[ident]
                progress = True
        if not remaining or not progress:
            break
    for ident in remaining:                     # unreachable in a validated DAG
        depth[ident] = 0
    layers: dict[int, list[str]] = {}
    for node in nodes:
        layers.setdefault(depth[node["id"]], []).append(node["id"])
    ordered = [{"id": ident, "layer": index, "row": row, "x": index * 264 + 20, "y": row * 132 + 20}
               for index in sorted(layers) for row, ident in enumerate(layers[index])]
    edges = [{"from": dep, "to": node["id"]} for node in nodes for dep in (node.get("depends_on") or [])]
    return {"nodes": ordered, "edges": edges, "layers": len(layers),
            "width": max((item["x"] for item in ordered), default=0) + 220,
            "height": max((item["y"] for item in ordered), default=0) + 132}


def version_detail(state, identifier: str) -> dict[str, Any]:
    studio = _studio(state)
    record = studio.find("workflow", identifier)
    definition = _definition(record)
    normalized, order = studio.workflows.validate(definition)
    return {"schema": SCHEMA, "observed_at": time.time(), "id": record["id"],
            "family": record.get("family"), "version": record.get("version"),
            "name": normalized["name"], "definition": normalized, "order": order,
            "layout": _layout(normalized["nodes"]),
            "read_only_nodes": [n["id"] for n in normalized["nodes"] if n.get("read_only")],
            "writes": [n["id"] for n in normalized["nodes"] if not n.get("read_only") and n["kind"] == "agent"],
            "updated_at": record.get("updated_at")}


def vocabulary(state) -> dict[str, Any]:
    roles = sorted(str(name) for name in state.config.roles.roles)
    return {
        "roles": roles[:1200], "role_count": len(roles),
        "agents": sorted(str(name) for name in state.config.agents),
        "kinds": ["agent", "research", "reviewer", "verification", "approval"],
        "conditions": ["success", "failure", "always"],
        "checks": ["tests", "lint", "types"],
        "budgets": {"turns": [24, 100], "tools": [100, 1000], "tokens": [32000, 1000000],
                    "seconds": [300, 3600]},
        "node_limit": 30,
        "note": "Roles, agents and limits come from the live configuration; the validator is the authority.",
    }


def _node_signature(node: dict[str, Any]) -> dict[str, Any]:
    return {key: node.get(key) for key in ("kind", "role", "task", "depends_on", "condition", "check",
                                           "budgets", "read_only", "agent", "review_of", "max_retries",
                                           "inputs", "expected_output", "acceptance_criteria", "allowed_tools")
            if key in node}


def diff_versions(state, left: str, right: str) -> dict[str, Any]:
    """Structural diff between two saved versions, field by field."""
    studio = _studio(state)
    first = studio.find("workflow", left)
    second = studio.find("workflow", right)
    a = {node["id"]: node for node in _definition(first).get("nodes") or []}
    b = {node["id"]: node for node in _definition(second).get("nodes") or []}
    added = [ident for ident in b if ident not in a]
    removed = [ident for ident in a if ident not in b]
    changed = []
    for ident in [name for name in a if name in b]:
        before, after = _node_signature(a[ident]), _node_signature(b[ident])
        fields = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
        if fields:
            changed.append({"id": ident, "fields": fields,
                            "before": {key: before.get(key) for key in fields},
                            "after": {key: after.get(key) for key in fields}})
    return {"schema": SCHEMA, "observed_at": time.time(),
            "left": {"id": first["id"], "version": first.get("version"), "name": _definition(first).get("name")},
            "right": {"id": second["id"], "version": second.get("version"), "name": _definition(second).get("name")},
            "same_family": first.get("family") == second.get("family"),
            "added": added, "removed": removed, "changed": changed,
            "unchanged": sorted(ident for ident in a if ident in b and ident not in {c["id"] for c in changed}),
            "identical": not added and not removed and not changed}


def run_rows(state, limit: int = MAX_ITEMS) -> dict[str, Any]:
    studio = _studio(state)
    limit = max(1, min(int(limit), MAX_ITEMS))
    rows = sorted(studio.items("workflow-run"), key=lambda row: row.get("updated_at") or 0, reverse=True)
    items = []
    for row in rows[:limit]:
        nodes = row.get("nodes") or {}
        statuses: dict[str, int] = {}
        for value in nodes.values():
            key = str((value or {}).get("status") or "pending")
            statuses[key] = statuses.get(key, 0) + 1
        items.append({
            "id": row["id"], "workflow_id": row.get("workflow_id"), "version": row.get("version"),
            "name": str(_definition(row).get("name") or "")[:120],
            "status": row.get("status"), "created": row.get("updated_at"),
            "nodes_done": sum(1 for v in nodes.values() if (v or {}).get("status") not in {None, "pending"}),
            "nodes_total": len(_definition(row).get("nodes") or []),
            "statuses": statuses, "error": str(row.get("error") or "")[:400],
        })
    return {"schema": SCHEMA, "observed_at": time.time(), "items": items, "count": len(rows),
            "store": "Studio store: workflow-run"}


def run_detail(state, identifier: str) -> dict[str, Any]:
    studio = _studio(state)
    record = studio.find("workflow-run", identifier)
    definition = _definition(record)
    observed = record.get("nodes") or {}
    nodes = []
    for node in definition.get("nodes") or []:
        value = observed.get(node["id"]) or {}
        result = value.get("result") if isinstance(value.get("result"), dict) else {}
        nodes.append({
            "id": node["id"], "kind": node["kind"], "role": node["role"],
            "depends_on": node.get("depends_on") or [],
            "condition": node.get("condition"), "read_only": node.get("read_only"),
            "task": str(node.get("task") or "")[:400],
            "status": str(value.get("status") or "pending"),
            "ok": value.get("ok"), "task_id": value.get("task_id"),
            "evidence_id": value.get("evidence_id"),
            "duration_s": value.get("duration_s"),
            "answer": value.get("answer"),
            "error": str(value.get("error") or "")[:400],
            "review_approved": value.get("review_approved"),
            "escalated": bool(value.get("escalated")),
            "text": str(result.get("text") or "")[:2000],
        })
    return {"schema": SCHEMA, "observed_at": time.time(), "id": record["id"],
            "workflow_id": record.get("workflow_id"), "version": record.get("version"),
            "name": str(definition.get("name") or "")[:120], "status": record.get("status"),
            "error": str(record.get("error") or "")[:2000],
            "created": record.get("updated_at"), "nodes": nodes,
            "layout": _layout(definition.get("nodes") or []),
            "counts": {status: sum(1 for node in nodes if node["status"] == status)
                       for status in {node["status"] for node in nodes}}}


def diff_runs(state, left: str, right: str) -> dict[str, Any]:
    """Node-level comparison of two runs of the same shape."""
    studio = _studio(state)
    first = studio.find("workflow-run", left)
    second = studio.find("workflow-run", right)
    a = (first.get("nodes") or {})
    b = (second.get("nodes") or {})
    changed, only_left, only_right = [], [], []
    for ident in sorted(set(a) | set(b)):
        if ident not in b:
            only_left.append(ident)
            continue
        if ident not in a:
            only_right.append(ident)
            continue
        before, after = a[ident] or {}, b[ident] or {}
        if (before.get("status"), before.get("ok")) != (after.get("status"), after.get("ok")):
            changed.append({"id": ident, "before": {"status": before.get("status"), "ok": before.get("ok"),
                                                    "task_id": before.get("task_id")},
                            "after": {"status": after.get("status"), "ok": after.get("ok"),
                                      "task_id": after.get("task_id")}})
    return {"schema": SCHEMA, "observed_at": time.time(),
            "left": {"id": first["id"], "status": first.get("status"), "version": first.get("version")},
            "right": {"id": second["id"], "status": second.get("status"), "version": second.get("version")},
            "changed": changed, "only_left": only_left, "only_right": only_right,
            "identical": not changed and not only_left and not only_right}


# ── comparisons for the page's overview ────────────────────────────────── #


def overview(state) -> dict[str, Any]:
    from .computer_control import EmergencyStop

    store = load_store(Path(state.workspace))
    runs = run_rows(state)
    saved = families(state)
    active = sum(1 for row in runs["items"] if row["status"] == "running")
    enabled = [row for row in store["schedules"] if row["enabled"]]
    next_due = min((row["next_run_at"] for row in enabled if row.get("next_run_at")), default=None)
    return {
        "schema": SCHEMA, "observed_at": time.time(), "demo": bool(getattr(state, "demo", False)),
        "families": saved["count"], "versions": sum(len(row["versions"]) for row in saved["families"]),
        "runs": runs["count"], "running": active,
        "schedules": len(store["schedules"]), "enabled_schedules": len(enabled),
        "next_run_at": next_due,
        "busy": bool(getattr(state, "workflow_busy", False) or getattr(state, "active_task", None)
                     or getattr(state, "task_queue", None)),
        "latch": {"stopped": EmergencyStop().stopped()},
        "queue_paused": bool(getattr(state, "queue_paused", False)),
        "limits": {"schedules": MAX_SCHEDULES, "interval": [MIN_INTERVAL, MAX_INTERVAL],
                   "runs_kept": MAX_RUNS_KEPT, "history": MAX_HISTORY, "nodes": 30},
        "tokens": {"save": SAVE_TOKEN, "schedule": SCHEDULE_TOKEN, "run": RUN_TOKEN,
                   "enable": "ENABLE SCHEDULE <id>", "disable": "DISABLE SCHEDULE <id>",
                   "remove": "REMOVE SCHEDULE <id>", "run_now": "RUN SCHEDULE <id>"},
        "vocabulary": vocabulary(state),
        "store": f"{STORE_DIR}/{SCHEDULES_FILE} (runtime state, never committed)",
        "notes": [
            {"id": "pinned", "text": "A schedule is pinned to one immutable version; saving a newer version never changes what it runs."},
            {"id": "missed", "text": "A window that passed while the server was down is recorded as missed and skipped, never replayed."},
            {"id": "consent", "text": "Creating, enabling, removing and running a schedule each need their own typed token."},
        ],
        "error": store.get("error", ""),
    }


# ── mutations ──────────────────────────────────────────────────────────── #


def _need(payload: dict[str, Any], token: str, hint: str) -> None:
    if payload.get("confirmation") != token:
        raise ValueError(f"Type {token} to confirm: {hint}")


def save_version(state, payload: dict[str, Any]) -> dict[str, Any]:
    studio = _studio(state)
    if getattr(state, "demo", False):
        raise ValueError("Live mode is required to save a workflow version.")
    definition = payload.get("definition")
    _need(payload, SAVE_TOKEN, "a new immutable version is written; nothing executes")
    family = payload.get("family")
    if family is not None and not isinstance(family, str):
        raise ValueError("family must be text")
    record = studio.workflows.save({"definition": definition, "family": family or ""})
    saved = record.get("data") if isinstance(record, dict) and "data" in record else record
    return {"schema": SCHEMA, "observed_at": time.time(), "saved": saved,
            "note": "New immutable version stored. The previous version still exists and still runs."}


def run_workflow(state, payload: dict[str, Any]) -> dict[str, Any]:
    studio = _studio(state)
    if getattr(state, "demo", False):
        raise ValueError("Live mode is required to run a workflow.")
    identifier = payload.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("A workflow version id is required")
    _need(payload, RUN_TOKEN, "agent nodes may write after their normal approvals")
    result = studio.workflows.run({"id": identifier, "confirm": RUN_TOKEN})
    body = result.get("data") if isinstance(result, dict) and "data" in result else result
    return {"schema": SCHEMA, "observed_at": time.time(), "started": body,
            "note": "The existing DAG engine is running this version; each write still passes its own approval."}


def cancel_run(state, payload: dict[str, Any]) -> dict[str, Any]:
    studio = _studio(state)
    identifier = payload.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("A run id is required")
    _need(payload, f"CANCEL RUN {identifier}", "running nodes are asked to stop cooperatively")
    result = studio.workflows.cancel(identifier)
    note = "Cooperative cancellation requested; in-flight provider calls finish under their own timeouts."
    return {"schema": SCHEMA, "observed_at": time.time(), "cancelled": result, "note": note}


def validate_definition(state, payload: dict[str, Any]) -> dict[str, Any]:
    """Dry run: validate and lay out a definition without writing or executing."""
    studio = _studio(state)
    definition, order = studio.workflows.validate(payload.get("definition"))
    return {"schema": SCHEMA, "observed_at": time.time(), "valid": True, "order": order,
            "definition": definition, "layout": _layout(definition["nodes"]),
            "writes": [n["id"] for n in definition["nodes"] if not n["read_only"] and n["kind"] == "agent"],
            "note": "Structural only: no model call, command or write happened."}


# ── schedules ──────────────────────────────────────────────────────────── #


def _schedule_id(workflow_id: str, trigger: dict[str, Any]) -> str:
    seed = json.dumps({"w": workflow_id, "t": trigger}, sort_keys=True).encode()
    return "w-" + hashlib.sha256(seed).hexdigest()[:8]


def _compile_trigger(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("A trigger object is required")
    kind = raw.get("kind")
    if kind == "interval":
        return {"kind": "interval", "minutes": _interval_minutes(raw.get("minutes"))}
    if kind == "cron":
        return {"kind": "cron", "cron": cron_expression(raw.get("cron"))}
    if kind == "manual":
        return {"kind": "manual"}
    raise ValueError("Trigger kind must be manual, interval or cron")


def _describe(trigger: dict[str, Any]) -> str:
    if trigger["kind"] == "interval":
        return f"every {trigger['minutes']} minutes"
    if trigger["kind"] == "cron":
        return f"cron {trigger['cron']}"
    return "manual only"


def schedules_view(state) -> dict[str, Any]:
    workspace = Path(state.workspace)
    store = load_store(workspace)
    studio = _studio(state)
    known = {row["id"]: row for row in studio.items("workflow")}
    rows = []
    for row in store["schedules"]:
        record = known.get(row["workflow_id"])
        rows.append({
            **row,
            "described": _describe(row["trigger"]),
            "version_present": record is not None,
            "version_name": str(_definition(record).get("name") or "")[:120] if record else "",
            "locked_out": bool(row["enabled"] and record is None),
        })
    return {"schema": SCHEMA, "observed_at": time.time(), "schedules": rows,
            "revision": store["revision"], "last_tick_at": store["last_tick_at"],
            "error": store.get("error", ""), "limits": {"schedules": MAX_SCHEDULES,
                                                        "interval": [MIN_INTERVAL, MAX_INTERVAL]},
            "store": f"{STORE_DIR}/{SCHEDULES_FILE}"}


def _replace(state, mutator) -> dict[str, Any]:
    """Apply a change to the schedule list under the workspace lock, with history."""
    workspace = Path(state.workspace)
    store = load_store(workspace)
    if store["error"] and store["schedules"]:
        raise ValueError("The schedule store is unreadable; repair it before changing schedules.")
    rows = [dict(row) for row in store["schedules"]]
    result = mutator(rows)
    _history_snapshot(workspace, store["schedules"], result.get("reason", "schedule change"))
    written = _write_store(workspace, rows, store["last_tick_at"])
    return {"schema": SCHEMA, "observed_at": time.time(), "revision": written["revision"],
            "schedules": rows, "changed": result}


def save_schedule(state, payload: dict[str, Any]) -> dict[str, Any]:
    studio = _studio(state)
    if getattr(state, "demo", False):
        raise ValueError("Live mode is required to schedule a workflow.")
    _need(payload, SCHEDULE_TOKEN, "this schedule may start the pinned version later")
    workflow_id = payload.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise ValueError("A workflow version id is required")
    record = studio.find("workflow", workflow_id)
    trigger = _compile_trigger(payload.get("trigger"))
    if trigger["kind"] == "manual" and payload.get("enabled"):
        raise ValueError("A manual trigger cannot be enabled; use Run now instead.")
    enabled = bool(payload.get("enabled", True))
    store = load_store(Path(state.workspace))
    if payload.get("revision") not in {None, store["revision"]}:
        raise ValueError("The schedule store changed; reload before saving.")
    ident = _schedule_id(workflow_id, trigger)
    now = time.time()

    def mutate(rows):
        existing = next((row for row in rows if row["id"] == ident), None)
        if existing is None:
            if len(rows) >= MAX_SCHEDULES:
                raise ValueError(f"At most {MAX_SCHEDULES} schedules are kept")
            rows.append(_compile_schedule({
                "id": ident, "family": record.get("family"), "workflow_id": workflow_id,
                "version": record.get("version"), "name": str(_definition(record).get("name") or ""),
                "trigger": trigger, "enabled": enabled, "created": now, "updated": now,
                "next_run_at": next_run(trigger, now) if enabled else None,
                "last": None, "runs": [], "missed": 0, "last_skip": None,
            }))
            action = "created"
        else:
            existing.update(enabled=enabled, trigger=trigger, workflow_id=workflow_id,
                            version=record.get("version"), name=str(_definition(record).get("name") or ""),
                            updated=now)
            existing["next_run_at"] = next_run(trigger, now) if enabled else None
            action = "updated"
        return {"reason": f"{action} schedule for {workflow_id}", "action": action, "id": ident}

    body = _replace(state, mutate)
    body["note"] = ("Saved. The schedule is pinned to this version and fires only while the server runs."
                    if enabled else "Saved disabled; nothing will fire until you enable it.")
    return body


def schedule_token(identifier: str, action: str) -> str:
    verb = {"enable": "ENABLE SCHEDULE", "disable": "DISABLE SCHEDULE",
            "remove": "REMOVE SCHEDULE", "run": "RUN SCHEDULE"}[action]
    return f"{verb} {identifier}"


def toggle_schedule(state, payload: dict[str, Any]) -> dict[str, Any]:
    ident = payload.get("id")
    if not isinstance(ident, str) or not _SCHEDULE_RE.fullmatch(ident):
        raise ValueError("A schedule id is required")
    enabled = payload.get("enabled")
    if type(enabled) is not bool:
        raise ValueError("enabled must be true or false")
    action = "enable" if enabled else "disable"
    _need(payload, schedule_token(ident, action), "the schedule changes state now")
    store = load_store(Path(state.workspace))
    if payload.get("revision") not in {None, store["revision"]}:
        raise ValueError("The schedule store changed; reload before changing it.")
    now = time.time()

    def mutate(rows):
        row = next((item for item in rows if item["id"] == ident), None)
        if row is None:
            raise ValueError("Unknown schedule")
        row["enabled"] = enabled
        row["updated"] = now
        row["next_run_at"] = next_run(row["trigger"], now) if enabled else None
        row["last_skip"] = None if enabled else row.get("last_skip")
        return {"reason": f"{action} {ident}", "action": action, "id": ident}

    body = _replace(state, mutate)
    body["note"] = ("Enabled: the next window will start the pinned version while the server runs."
                    if enabled else "Disabled: nothing will fire until you enable it again.")
    return body


def remove_schedule(state, payload: dict[str, Any]) -> dict[str, Any]:
    ident = payload.get("id")
    if not isinstance(ident, str) or not _SCHEDULE_RE.fullmatch(ident):
        raise ValueError("A schedule id is required")
    _need(payload, schedule_token(ident, "remove"), "the schedule record is deleted")

    def mutate(rows):
        before = len(rows)
        rows[:] = [row for row in rows if row["id"] != ident]
        if len(rows) == before:
            raise ValueError("Unknown schedule")
        return {"reason": f"remove {ident}", "action": "removed", "id": ident}

    body = _replace(state, mutate)
    body["note"] = "Removed. Saved workflow versions and past runs are untouched."
    return body


def run_schedule_now(state, payload: dict[str, Any]) -> dict[str, Any]:
    """Start one schedule immediately, through the same consented run path."""
    ident = payload.get("id")
    if not isinstance(ident, str) or not _SCHEDULE_RE.fullmatch(ident):
        raise ValueError("A schedule id is required")
    _need(payload, schedule_token(ident, "run"), "the pinned version starts now")
    store = load_store(Path(state.workspace))
    row = next((item for item in store["schedules"] if item["id"] == ident), None)
    if row is None:
        raise ValueError("Unknown schedule")
    body = execute_schedule(state, row, reason="run now")
    if body.get("skipped"):
        raise ValueError(body["reason"])
    return body


# ── audit ──────────────────────────────────────────────────────────────── #

_audit_lock = threading.Lock()
_audit_at: dict[str, float] = {}


def audit_read(state, action: str, client: str, *, every: float = 300.0) -> bool:
    """Sample reads into the shared API audit trail (never blocks the answer)."""
    from . import webapi

    now = time.time()
    key = f"{client}|{action}"
    with _audit_lock:
        if every > 0 and now - _audit_at.get(key, 0.0) < every:
            return False
        _audit_at[key] = now
    try:
        webapi.audit_api(Path(state.workspace), action, actor="user", target="dashboard", result="ok",
                         risk="low", detail={"client": client})
    except (OSError, ValueError):
        return False
    return True


def audit_action(state, action: str, client: str, data: dict[str, Any]) -> None:
    """Every mutation is recorded; a failure to write the trail is reported, not hidden."""
    from . import webapi

    detail: dict[str, Any] = {"client": client, "action": action}
    for key in ("changed", "started", "cancelled", "saved"):
        value = data.get(key)
        if isinstance(value, dict):
            detail[key] = {k: v for k, v in value.items() if isinstance(v, (str, int, float, bool))}
    try:
        webapi.audit_api(Path(state.workspace), f"workflow.{action.replace('/', '.')}", actor="user",
                         target=str(detail.get("changed", {}).get("id", "workflow"))[:120],
                         result="ok", risk="high", detail=detail)
    except (OSError, ValueError) as exc:
        raise ValueError(f"The audit trail could not be written: {str(exc)[:200]}") from None


# ── the scheduler ──────────────────────────────────────────────────────── #


def execute_schedule(state, row: dict[str, Any], *, reason: str) -> dict[str, Any]:
    """Start one scheduled run or report exactly why it was not started."""
    from .computer_control import EmergencyStop

    studio = _studio(state)
    if getattr(state, "demo", False):
        return {"skipped": True, "reason": "Demo mode does not run workflows."}
    if EmergencyStop().stopped():
        return {"skipped": True, "reason": "Emergency stop is latched; scheduled work stays blocked."}
    if getattr(state, "queue_paused", False):
        return {"skipped": True, "reason": "The queue is paused."}
    if getattr(state, "workflow_busy", False) or getattr(state, "active_task", None) or getattr(state, "task_queue", None):
        return {"skipped": True, "deferred": True, "reason": "Another run or workflow is in progress."}
    try:
        studio.find("workflow", row["workflow_id"])
    except ValueError:
        return {"skipped": True, "reason": "The pinned workflow version no longer exists; the schedule is blocked."}
    try:
        result = studio.workflows.run({"id": row["workflow_id"], "confirm": RUN_TOKEN})
    except ValueError as exc:
        return {"skipped": True, "deferred": True, "reason": str(exc)[:300]}
    started = result.get("data") if isinstance(result, dict) and "data" in result else result
    run_id = str((started or {}).get("run_id") or "")
    return {"schema": SCHEMA, "observed_at": time.time(), "skipped": False, "reason": reason,
            "run_id": run_id, "workflow_id": row["workflow_id"],
            "note": "Started through the same reviewed run path as the Run button."}


def tick(state, *, now: float | None = None) -> dict[str, Any]:
    """One scheduler pass: start due work, record skips and advance windows."""
    from filelock import FileLock

    workspace = Path(state.workspace)
    moment = float(now if now is not None else time.time())
    store = load_store(workspace)
    actions: list[dict[str, Any]] = []
    if store["error"] and not store["schedules"]:
        return {"schema": SCHEMA, "observed_at": moment, "actions": [],
                "error": store["error"], "ran": 0, "skipped": 0}
    rows = [dict(row) for row in store["schedules"]]
    store_dir = _store(workspace)
    store_dir.mkdir(parents=True, exist_ok=True)
    lock_path = store_dir / LOCK_FILE
    try:
        lock = FileLock(str(lock_path), timeout=0)
        lock.acquire()
    except (TimeoutError, OSError):
        return {"schema": SCHEMA, "observed_at": moment, "actions": [], "busy": True,
                "note": "Another server owns the scheduler lock; this pass did nothing."}
    changed = False
    try:
        last_tick = float(store.get("last_tick_at") or 0.0)
        down_gap = bool(last_tick) and (moment - last_tick) > max(MISSED_GRACE, 60.0)
        for row in rows:
            if not row["enabled"] or not row.get("next_run_at"):
                continue
            due = float(row["next_run_at"])
            if due > moment:
                continue
            if down_gap and due < moment - MISSED_GRACE:
                # The window opened while this process was not running and is
                # older than the grace window: record it as missed and move on.
                # Never start stale work on boot.
                missed = row.get("missed", 0) + 1
                row.update(missed=missed, last_skip={"at": moment, "reason": "Window passed while the server was not running"},
                           next_run_at=next_run(row["trigger"], moment))
                actions.append({"id": row["id"], "action": "missed", "reason": row["last_skip"]["reason"]})
                changed = True
                continue
            if moment - due > DEFER_LIMIT:
                row.update(last_skip={"at": moment, "reason": "The window stayed blocked for over 30 minutes"},
                           next_run_at=next_run(row["trigger"], moment), missed=row.get("missed", 0) + 1)
                actions.append({"id": row["id"], "action": "skipped", "reason": row["last_skip"]["reason"]})
                changed = True
                continue
            outcome = execute_schedule(state, row, reason="schedule")
            if outcome.get("skipped") and not outcome.get("deferred"):
                row.update(last_skip={"at": moment, "reason": outcome["reason"]},
                           next_run_at=next_run(row["trigger"], moment))
                actions.append({"id": row["id"], "action": "skipped", "reason": outcome["reason"]})
                changed = True
                continue
            if outcome.get("deferred"):
                row.update(last_skip={"at": moment, "reason": outcome["reason"]})
                actions.append({"id": row["id"], "action": "deferred", "reason": outcome["reason"]})
                changed = True
                continue
            run_id = outcome.get("run_id") or ""
            runs = [run_id, *row.get("runs", [])][:MAX_RUNS_KEPT] if run_id else list(row.get("runs", []))
            row.update(runs=runs, last={"at": moment, "run_id": run_id, "status": "started",
                                        "source": "schedule"}, last_skip=None,
                       next_run_at=next_run(row["trigger"], moment), updated=moment)
            actions.append({"id": row["id"], "action": "ran", "run_id": run_id})
            changed = True
        if changed or abs(moment - last_tick) > 1:
            _write_store(workspace, rows, moment)
    finally:
        lock.release()
    ran = sum(1 for action in actions if action["action"] == "ran")
    return {"schema": SCHEMA, "observed_at": moment, "actions": actions, "ran": ran,
            "skipped": sum(1 for action in actions if action["action"] in {"skipped", "missed", "deferred"}),
            "next": min((row["next_run_at"] for row in rows
                         if row["enabled"] and row.get("next_run_at")), default=None)}


class WorkflowScheduler:
    """Background ticker for the persisted schedules; one per server process."""

    def __init__(self, state, *, interval: float | None = None):
        self.state = state
        self.workspace = Path(state.workspace)
        raw = os.environ.get("NEXUS_WORKFLOW_TICK")
        try:
            self.interval = float(interval if interval is not None else (raw or 20.0))
        except ValueError:
            self.interval = 20.0
        self.interval = min(300.0, max(1.0, self.interval))
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.last_pass: dict[str, Any] | None = None
        self.passes = 0
        self.error = ""

    # ── lifecycle ──────────────────────────────────────────────────────── #
    def has_schedules(self) -> bool:
        try:
            return bool(load_store(self.workspace)["schedules"])
        except (OSError, ValueError):
            return False

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="nexus-workflow-scheduler", daemon=True)
            self._thread.start()
            return True

    def ensure_started(self) -> bool:
        """Start on demand when a schedule exists (also after a restart)."""
        if not self.has_schedules():
            return False
        return self.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.has_schedules():
                    self.last_pass = tick(self.state)
                    self.passes += 1
                    self.error = ""
            except (OSError, ValueError) as exc:      # never let one bad pass kill the ticker
                self.error = str(exc)[:300]
            self._stop.wait(self.interval)

    # ── status for the page ────────────────────────────────────────────── #
    def status(self) -> dict[str, Any]:
        store = load_store(self.workspace)
        return {"schema": SCHEMA, "running": self.running, "interval_s": self.interval,
                "passes": self.passes, "last_pass": self.last_pass,
                "last_tick_at": store["last_tick_at"], "error": self.error or store.get("error", ""),
                "schedules": len(store["schedules"]),
                "enabled": sum(1 for row in store["schedules"] if row["enabled"])}


def scheduler_for(state) -> WorkflowScheduler:
    """The per-server scheduler, created on first use."""
    scheduler = getattr(state, "workflow_scheduler", None)
    if scheduler is None:
        scheduler = WorkflowScheduler(state)
        state.workflow_scheduler = scheduler
    return scheduler
