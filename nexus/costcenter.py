"""Cost, latency and resource ledger — measured, never estimated by guesswork.

Every number on the Cost & models page comes from something that actually
happened:

* **spend and tokens** — the per-call records the agent already writes into
  ``session_observations`` (``models[]``), each one carrying the price snapshot
  that was in force at that moment and the cost it computed, or ``null`` when
  either the usage or the rate was unknown. An unknown call is counted as
  *unpriced*; it is never quietly treated as free.
* **latency** — the gap between the ``model_wire`` and ``model_response`` events
  of the same call in the same trace, in milliseconds.
* **resources** — real ``psutil`` / ``nvidia-smi`` samples, stored with the time
  they were taken, and only while someone is watching the page.

The ledger is a small SQLite file under ``.nexus/cost`` built by importing those
traces incrementally: a run is imported once, and the import is keyed by its
trace id so re-reading is cheap and idempotent.

A budget is policy, not a suggestion: when ``budget.enabled`` and ``stop`` are
both on, :func:`enforce` raises :class:`BudgetExceeded` *before* the next model
call, and the run ends with an explicit "budget stop" reason instead of a
provider error.
"""

from __future__ import annotations

import decimal
import hashlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = 1

DEFAULT_POLICY: dict[str, Any] = {
    "schema": SCHEMA,
    "budget": {
        "enabled": False,          # a budget nobody asked for must not stop work
        "daily_usd": 5.0,
        "monthly_usd": 50.0,
        "stop": True,              # a budget that cannot stop is decoration
        "unknown_price_blocks": False,
    },
    "resources": {"enabled": True, "interval_s": 15, "keep_hours": 48},
}

MAX_DAILY, MAX_MONTHLY = 0.0, 1_000_000.0
MIN_INTERVAL, MAX_INTERVAL = 5, 300
MIN_KEEP_HOURS, MAX_KEEP_HOURS = 1, 720

MAX_IMPORT_BODIES = 1000
MAX_ROWS = 200_000
MAX_RESOURCE_ROWS = 20_000
MAX_COMPARE_MODELS = 4
MIN_COMPARE_MODELS = 2
MAX_COMPARE_PROMPT = 2000
MAX_COMPARE_TOKENS = 512
MIN_COMPARE_TOKENS = 16
COMPARE_TIMEOUT = 25.0
MAX_COMPARE_TEXT = 400
MAX_ROWS_OUT = 500
SYNC_SECONDS = 3.0

SAVE_TOKEN = "SAVE COST POLICY"
COMPARE_TOKEN = "START COMPARISON"
CLEAR_TOKEN = "CLEAR COST HISTORY"

ALLOWED_TABLES = "run call resource compare setting".split()


class BudgetExceeded(RuntimeError):
    """Raised before a model call that the configured budget does not allow."""


# ── paths and policy ───────────────────────────────────────────────────── #


def store_dir(workspace: Path | str) -> Path:
    return Path(workspace) / ".nexus" / "cost"


def db_path(workspace: Path | str) -> Path:
    return store_dir(workspace) / "cost.db"


def policy_path(workspace: Path | str) -> Path:
    return store_dir(workspace) / "policy.json"


def display_path(workspace: Path, path: Path) -> str:
    try:
        return str(path.relative_to(Path(workspace).resolve()))
    except ValueError:
        return str(path)


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"{path.name} must not be a symlink")
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(data)
    os.chmod(temp, 0o600)
    temp.replace(path)


def _validated(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict):
        raise ValueError("The cost policy must be an object")
    unknown = set(policy) - set(DEFAULT_POLICY)
    if unknown:
        raise ValueError(f"Unknown policy key(s): {', '.join(sorted(unknown))[:120]}")
    budget = policy.get("budget", {})
    resources = policy.get("resources", {})
    if not isinstance(budget, dict) or not isinstance(resources, dict):
        raise ValueError("budget and resources must be objects")
    for name, part in (("budget", budget), ("resources", resources)):
        unknown = set(part) - set(DEFAULT_POLICY[name])
        if unknown:
            raise ValueError(f"Unknown {name} key(s): {', '.join(sorted(unknown))[:120]}")
    clean = {"schema": SCHEMA, "budget": {}, "resources": {}}
    for key, default in DEFAULT_POLICY["budget"].items():
        value = budget.get(key, default)
        if key in {"enabled", "stop", "unknown_price_blocks"}:
            if type(value) is not bool:
                raise ValueError(f"budget.{key} must be true or false")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"budget.{key} must be a finite number of US dollars")
            if value < 0:
                raise ValueError(f"budget.{key} cannot be negative")
        clean["budget"][key] = value
    if clean["budget"]["daily_usd"] > clean["budget"]["monthly_usd"]:
        raise ValueError("The daily budget cannot exceed the monthly budget")
    if clean["budget"]["daily_usd"] > MAX_MONTHLY or clean["budget"]["monthly_usd"] > MAX_MONTHLY:
        raise ValueError(f"A budget above ${MAX_MONTHLY:,.0f} looks like a typo")
    for key, default in DEFAULT_POLICY["resources"].items():
        value = resources.get(key, default)
        if key == "enabled":
            if type(value) is not bool:
                raise ValueError("resources.enabled must be true or false")
        elif type(value) is not int or isinstance(value, bool):
            raise ValueError(f"resources.{key} must be a whole number")
        elif key == "interval_s" and not MIN_INTERVAL <= value <= MAX_INTERVAL:
            raise ValueError(f"resources.interval_s must be between {MIN_INTERVAL} and {MAX_INTERVAL}")
        elif key == "keep_hours" and not MIN_KEEP_HOURS <= value <= MAX_KEEP_HOURS:
            raise ValueError(f"resources.keep_hours must be between {MIN_KEEP_HOURS} and {MAX_KEEP_HOURS}")
        clean["resources"][key] = value
    return clean


def load_policy(workspace: Path | str) -> dict[str, Any]:
    path = policy_path(workspace)
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_POLICY))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"The cost policy file is unreadable ({type(exc).__name__}); fix or delete it") from exc
    return _validated(raw)


_POLICY_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _cached_policy(workspace: Path | str) -> dict[str, Any] | None:
    """Hot-path read used before every model call: 2 s cache, mtime keyed."""
    path = policy_path(workspace)
    key = str(path)
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    hit = _POLICY_CACHE.get(key)
    if hit and hit[0] == stamp and time.time() - hit[1].get("_read_at", 0) < 2.0:
        return hit[1]
    try:
        policy = load_policy(workspace)
    except ValueError:
        return None
    policy["_read_at"] = time.time()
    _POLICY_CACHE[key] = (stamp, policy)
    return policy


def policy_bytes(policy: dict[str, Any]) -> bytes:
    return (json.dumps(policy, indent=2, sort_keys=True) + "\n").encode()


def revision_of(workspace: Path | str) -> str:
    path = policy_path(workspace)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def policy_view(workspace: Path | str) -> dict[str, Any]:
    try:
        policy = load_policy(workspace)
        error = ""
    except ValueError as exc:
        policy, error = json.loads(json.dumps(DEFAULT_POLICY)), str(exc)
    return {
        "schema": SCHEMA, "observed_at": time.time(), "policy": policy, "error": error,
        "revision": revision_of(workspace),
        "file": display_path(Path(workspace), policy_path(workspace)),
        "file_exists": policy_path(workspace).exists(),
        "tokens": {"save": SAVE_TOKEN, "compare": COMPARE_TOKEN, "clear": CLEAR_TOKEN},
        "limits": {"daily": MAX_DAILY, "monthly": MAX_MONTHLY,
                   "interval": [MIN_INTERVAL, MAX_INTERVAL], "keep": [MIN_KEEP_HOURS, MAX_KEEP_HOURS],
                   "compare_models": MAX_COMPARE_MODELS, "compare_prompt": MAX_COMPARE_PROMPT,
                   "compare_tokens": [MIN_COMPARE_TOKENS, MAX_COMPARE_TOKENS]},
    }


def save_policy(state, payload: dict[str, Any]) -> dict[str, Any]:
    from . import securitycenter

    workspace = Path(state.workspace)
    if str(payload.get("confirmation") or "").strip() != SAVE_TOKEN:
        raise ValueError(f"Type {SAVE_TOKEN} to save the cost policy")
    if str(payload.get("revision") or "") != revision_of(workspace):
        raise ValueError("This policy changed since you read it; reload before saving")
    unknown = set(payload) - {"confirmation", "revision", "budget", "resources"}
    if unknown:
        raise ValueError(f"Unknown policy key(s): {', '.join(sorted(unknown))[:120]}")
    current = load_policy(workspace)
    merged = {"schema": SCHEMA, "budget": dict(current["budget"]), "resources": dict(current["resources"])}
    for section in ("budget", "resources"):
        incoming = payload.get(section)
        if incoming is None:
            continue
        if not isinstance(incoming, dict):
            raise ValueError(f"{section} must be an object")
        for key, value in incoming.items():
            if key not in merged[section]:
                raise ValueError(f"Unknown {section} key: {key}")
            merged[section][key] = value
    clean = _validated(merged)
    _atomic_bytes(policy_path(workspace), policy_bytes(clean))
    _POLICY_CACHE.pop(str(policy_path(workspace)), None)
    securitycenter.audit_action(
        state, "cost.policy.save", "user",
        {"daily_usd": clean["budget"]["daily_usd"], "monthly_usd": clean["budget"]["monthly_usd"],
         "enabled": clean["budget"]["enabled"], "stop": clean["budget"]["stop"]},
        risk="medium", target="cost")
    return {**policy_view(workspace), "saved": True}


# ── the ledger ─────────────────────────────────────────────────────────── #


def _connect(workspace: Path | str) -> sqlite3.Connection:
    path = db_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("The cost ledger must not be a symlink")
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    os.chmod(path, 0o600)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS run (
            id TEXT PRIMARY KEY, started REAL, day TEXT, role TEXT, agent TEXT,
            task TEXT, duration_s REAL, ok INTEGER, imported REAL);
        CREATE TABLE IF NOT EXISTS call (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, seq INTEGER, at REAL, day TEXT,
            agent TEXT, role TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
            cost_usd REAL, priced INTEGER, latency_ms REAL, ok INTEGER,
            UNIQUE(run_id, seq));
        CREATE INDEX IF NOT EXISTS idx_call_day ON call(day);
        CREATE INDEX IF NOT EXISTS idx_call_model ON call(model);
        CREATE TABLE IF NOT EXISTS resource (
            at REAL PRIMARY KEY, cpu REAL, ram_used INTEGER, ram_total INTEGER,
            gpu_util REAL, gpu_mem_used INTEGER, gpu_mem_total INTEGER);
        CREATE TABLE IF NOT EXISTS compare (
            id TEXT PRIMARY KEY, created REAL, prompt TEXT, models TEXT, status TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    connection.commit()
    return connection


def _setting(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_setting(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute("INSERT OR REPLACE INTO setting(key, value) VALUES (?,?)", (key, value))


def _stat(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


_SYNC_LOCK: dict[str, float] = {}


def sync(workspace: Path | str, *, force: bool = False) -> dict[str, Any]:
    """Import any trace that is not in the ledger yet. Cheap and idempotent."""
    workspace = Path(workspace)
    key = str(workspace)
    now = time.time()
    if not force and now - _SYNC_LOCK.get(key, 0.0) < SYNC_SECONDS:
        return {"imported": 0, "skipped": 0, "cached": True}
    _SYNC_LOCK[key] = now
    if not db_path(workspace).exists() and not force:
        # No ledger yet: only build one when there is something to import.
        if not _has_traces(workspace):
            return {"imported": 0, "skipped": 0, "cached": False}
    from .sessions import SessionStore

    store = SessionStore()
    bodies, truncated = store.observations(workspace, days=365, limit=MAX_IMPORT_BODIES)
    connection = _connect(workspace)
    imported = skipped = 0
    try:
        if _stat(db_path(workspace)) > 200 * 1024 * 1024:
            raise ValueError("The cost ledger is larger than 200 MB; clear it first")
        known = {row["id"] for row in connection.execute("SELECT id FROM run")}
        cleared = float(_setting(connection, "cleared_at", "0") or 0.0)
        for body in bodies:
            if cleared and float(body.get("started") or 0.0) <= cleared:
                skipped += 1
                continue
            ident = str(body.get("id") or "")
            if not ident or ident in known:
                skipped += 1
                continue
            _import_run(connection, workspace, body)
            imported += 1
        connection.commit()
        _set_setting(connection, "last_sync", str(now))
    finally:
        connection.close()
    return {"imported": imported, "skipped": skipped, "truncated": bool(truncated), "cached": False}


def _has_traces(workspace: Path) -> bool:
    from .sessions import SessionStore

    try:
        bodies, _ = SessionStore().observations(workspace, days=365, limit=1)
    except (OSError, ValueError):
        return False
    return bool(bodies)


def _import_run(connection: sqlite3.Connection, workspace: Path, body: dict[str, Any]) -> None:
    ident = str(body.get("id"))
    started = float(body.get("started") or 0.0)
    day = time.strftime("%Y-%m-%d", time.gmtime(started or time.time()))
    outcome = body.get("outcome") or {}
    models = body.get("models") or []
    role = str(outcome.get("role") or "")
    agent = str(outcome.get("agent") or (models[0].get("agent") if models else "") or "")
    task = str(body.get("trigger") or "")[:400]
    # Pair wire -> response per agent so the latency is the real call, not the run.
    pending: dict[str, list[float]] = {}
    answered: list[tuple[str, float, float]] = []
    for event in body.get("events") or []:
        kind = event.get("kind")
        at = float(event.get("at") or 0.0)
        if kind == "model_wire":
            pending.setdefault(str((event.get("data") or {}).get("agent") or ""), []).append(at)
        elif kind == "model_response":
            name = str(((event.get("data") or {}).get("usage") or {}).get("agent") or "")
            queue = pending.get(name) or []
            if queue:
                wire = queue.pop(0)
                if at >= wire:
                    answered.append((name, at, round((at - wire) * 1000.0, 1)))
    connection.execute(
        "INSERT OR REPLACE INTO run(id, started, day, role, agent, task, duration_s, ok, imported)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (ident, started, day, role, agent, task, body.get("duration_s"),
         0 if body.get("state") == "error" else 1, time.time()))
    used: dict[str, int] = {}
    for seq, item in enumerate(models[:500]):
        name = str(item.get("agent") or agent)
        candidates = [row for row in answered if row[0] == name]
        index = used.get(name, 0)
        used[name] = index + 1
        at, latency = (candidates[index][1], candidates[index][2]) if index < len(candidates) else (0.0, None)
        cost = item.get("calculated_model_cost_usd")
        priced = 1 if isinstance(cost, (int, float)) and math.isfinite(float(cost)) else 0
        connection.execute(
            "INSERT OR REPLACE INTO call(run_id, seq, at, day, agent, role, model, input_tokens,"
            " output_tokens, cost_usd, priced, latency_ms, ok) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ident, seq, at or started, str(item.get("day") or day), name, role,
             str(item.get("model") or ""), item.get("input_tokens"), item.get("output_tokens"),
             float(cost) if priced else None, priced, latency, 1))
    _prune(connection)


def _prune(connection: sqlite3.Connection) -> None:
    total = connection.execute("SELECT COUNT(*) FROM call").fetchone()[0]
    if total > MAX_ROWS:
        connection.execute(
            "DELETE FROM call WHERE id IN (SELECT id FROM call ORDER BY id LIMIT ?)",
            (total - MAX_ROWS,))
    resources = connection.execute("SELECT COUNT(*) FROM resource").fetchone()[0]
    if resources > MAX_RESOURCE_ROWS:
        connection.execute(
            "DELETE FROM resource WHERE at IN (SELECT at FROM resource ORDER BY at LIMIT ?)",
            (resources - MAX_RESOURCE_ROWS,))


def _db_bytes(workspace: Path) -> int:
    return _stat(db_path(workspace))


# ── aggregates ─────────────────────────────────────────────────────────── #


def _day_start(now: float | None = None) -> float:
    stamp = time.time() if now is None else now
    local = time.localtime(stamp)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))


def _month_start(now: float | None = None) -> float:
    stamp = time.time() if now is None else now
    local = time.localtime(stamp)
    return time.mktime((local.tm_year, local.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def percentile(values: list[float], fraction: float) -> float | None:
    """Linear-interpolated percentile; ``None`` for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    position = (len(ordered) - 1) * min(max(fraction, 0.0), 1.0)
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight, 1)


def _window(days: int) -> float:
    if type(days) is not int or not 1 <= days <= 365:
        raise ValueError("days must be a whole number between 1 and 365")
    return _day_start() - (days - 1) * 86400


def spend(workspace: Path | str, since: float) -> dict[str, Any]:
    """Priced spend and unpriced call count since a timestamp."""
    workspace = Path(workspace)
    if not db_path(workspace).exists():
        return {"usd": 0.0, "priced": 0, "unpriced": 0, "calls": 0, "input": 0, "output": 0}
    connection = _connect(workspace)
    try:
        row = connection.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS usd,"
            " COALESCE(SUM(CASE WHEN priced=1 THEN 1 ELSE 0 END), 0) AS priced,"
            " COALESCE(SUM(CASE WHEN priced=0 THEN 1 ELSE 0 END), 0) AS unpriced,"
            " COUNT(*) AS calls, COALESCE(SUM(input_tokens), 0) AS input,"
            " COALESCE(SUM(output_tokens), 0) AS output FROM call WHERE at >= ?", (since,)).fetchone()
    finally:
        connection.close()
    return {"usd": round(float(row["usd"]), 6), "priced": int(row["priced"]), "unpriced": int(row["unpriced"]),
            "calls": int(row["calls"]), "input": int(row["input"]), "output": int(row["output"])}


def _rows(workspace: Path, days: int, group: str) -> list[dict[str, Any]]:
    columns = {"model": "model", "agent": "agent", "role": "role", "day": "day"}
    if group not in columns:
        raise ValueError("Group by model, agent, role or day")
    column = columns[group]
    connection = _connect(workspace)
    try:
        rows = connection.execute(
            f"SELECT {column} AS name, COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input,"
            f" COALESCE(SUM(output_tokens),0) AS output, COALESCE(SUM(cost_usd),0) AS usd,"
            f" COALESCE(SUM(CASE WHEN priced=0 THEN 1 ELSE 0 END),0) AS unpriced,"
            f" MIN(at) AS first_at, MAX(at) AS last_at FROM call WHERE at >= ?"
            f" GROUP BY {column} ORDER BY calls DESC LIMIT {MAX_ROWS_OUT}", (_window(days),)).fetchall()
    finally:
        connection.close()
    return [{"name": row["name"] or "(unknown)", "calls": int(row["calls"]), "input": int(row["input"]),
             "output": int(row["output"]), "usd": round(float(row["usd"]), 6),
             "unpriced": int(row["unpriced"]), "first_at": row["first_at"], "last_at": row["last_at"]}
            for row in rows]


def latency(workspace: Path | str, days: int = 7, group: str = "model") -> dict[str, Any]:
    """Real per-call latency percentiles, measured wire -> response."""
    workspace = Path(workspace)
    window = _window(days)
    if not db_path(workspace).exists():
        return {"group": group, "rows": [], "overall": {"calls": 0, "p50": None, "p90": None, "p99": None},
                "samples": 0}
    if group not in {"model", "agent", "role"}:
        raise ValueError("Group latency by model, agent or role")
    connection = _connect(workspace)
    try:
        rows = connection.execute(
            f"SELECT {group} AS name, latency_ms FROM call WHERE at >= ? AND latency_ms IS NOT NULL",
            (window,)).fetchall()
    finally:
        connection.close()
    buckets: dict[str, list[float]] = {}
    for row in rows:
        buckets.setdefault(str(row["name"] or "(unknown)"), []).append(float(row["latency_ms"]))
    out = []
    for name, values in buckets.items():
        out.append({"name": name, "calls": len(values), "p50": percentile(values, 0.5),
                    "p90": percentile(values, 0.9), "p99": percentile(values, 0.99),
                    "min": round(min(values), 1), "max": round(max(values), 1)})
    out.sort(key=lambda item: (-item["calls"], item["name"]))
    everything = [value for values in buckets.values() for value in values]
    return {"group": group, "rows": out[:MAX_ROWS_OUT],
            "overall": {"calls": len(everything), "p50": percentile(everything, 0.5),
                        "p90": percentile(everything, 0.9), "p99": percentile(everything, 0.99)},
            "samples": len(everything)}


# ── budget ─────────────────────────────────────────────────────────────── #


def budget_state(workspace: Path | str) -> dict[str, Any]:
    workspace = Path(workspace)
    try:
        policy = load_policy(workspace)["budget"]
        error = ""
    except ValueError as exc:
        policy, error = json.loads(json.dumps(DEFAULT_POLICY["budget"])), str(exc)
    today = spend(workspace, _day_start())
    month = spend(workspace, _month_start())
    daily = float(policy["daily_usd"])
    monthly = float(policy["monthly_usd"])
    return {
        "enabled": bool(policy["enabled"]), "stop": bool(policy["stop"]),
        "unknown_price_blocks": bool(policy["unknown_price_blocks"]),
        "daily_usd": daily, "monthly_usd": monthly,
        "today": {**today, "budget": daily, "remaining": round(max(0.0, daily - today["usd"]), 6)},
        "month": {**month, "budget": monthly, "remaining": round(max(0.0, monthly - month["usd"]), 6)},
        "error": error,
        "enforced": bool(policy["enabled"] and policy["stop"]),
    }


def enforce(workspace: Path | str) -> None:
    """Called before every model call. Raises only when a budget is really on."""
    policy = _cached_policy(workspace)
    if not policy or not policy["budget"]["enabled"] or not policy["budget"]["stop"]:
        return
    sync(workspace)
    state = budget_state(workspace)
    if state["today"]["usd"] >= state["daily_usd"] - 1e-9:
        raise BudgetExceeded(
            f"daily budget of ${state['daily_usd']:.2f} reached (${state['today']['usd']:.4f} spent today)")
    if state["month"]["usd"] >= state["monthly_usd"] - 1e-9:
        raise BudgetExceeded(
            f"monthly budget of ${state['monthly_usd']:.2f} reached (${state['month']['usd']:.4f} this month)")
    if state["unknown_price_blocks"] and state["today"]["unpriced"]:
        raise BudgetExceeded(
            f"{state['today']['unpriced']} call(s) today have no verified price; "
            "an unpriced budget cannot be verified")


# ── providers ──────────────────────────────────────────────────────────── #


def providers(state) -> dict[str, Any]:
    """Dropdown data: configured agents only. No network probe on page load."""
    from .workbench import local_endpoint

    config = state.config
    settings = state.workbench.settings()
    pricing = settings.get("pricing") or {}
    options = []
    for name, agent in sorted(config.agents.items()):
        rate = pricing.get(agent.model) or {}
        options.append({
            "name": name, "provider": agent.provider, "model": agent.model or "(default)",
            "label": agent.label or name, "local": local_endpoint(agent.effective_base_url or ""),
            "key_present": bool(agent.resolved_api_key), "optional": bool(agent.optional),
            "fallback": agent.fallback or "", "priced": bool(rate),
            "rate_source": str(rate.get("source") or ""),
        })
    priced_models = [{"model": model, "source": str(rate.get("source") or ""),
                      "input_per_million": rate.get("input_per_million"),
                      "output_per_million": rate.get("output_per_million")}
                     for model, rate in sorted(pricing.items())]
    return {"schema": SCHEMA, "observed_at": time.time(), "agents": options,
            "priced_models": priced_models, "local_only": bool(settings.get("local_only")),
            "note": "Configured endpoints only. A key being present does not prove quota or balance."}


# ── comparison runner ──────────────────────────────────────────────────── #


def _price_for(state, model: str) -> dict[str, Any] | None:
    pricing = (state.workbench.settings().get("pricing") or {})
    rate = pricing.get(model)
    if not isinstance(rate, dict):
        return None
    try:
        values = {key: float(rate[key]) for key in ("input_per_million", "output_per_million")}
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and value >= 0 for value in values.values()):
        return None
    return {**values, "source": str(rate.get("source") or "")}


def compare(state, payload: dict[str, Any]) -> dict[str, Any]:
    """Run the same prompt on 2-4 named agents. Real calls, real charges."""
    workspace = Path(state.workspace)
    if str(payload.get("confirmation") or "").strip() != COMPARE_TOKEN:
        raise ValueError(f"Type {COMPARE_TOKEN} to run a comparison")
    names = payload.get("agents")
    if not isinstance(names, list) or not MIN_COMPARE_MODELS <= len(names) <= MAX_COMPARE_MODELS:
        raise ValueError(f"Choose between {MIN_COMPARE_MODELS} and {MAX_COMPARE_MODELS} models")
    names = [str(name) for name in names]
    if len(set(names)) != len(names):
        raise ValueError("Choose each model once")
    for name in names:
        if name not in state.config.agents:
            raise ValueError(f"Unknown agent: {name}")
    prompt = str(payload.get("prompt") or "").strip()
    if not 1 <= len(prompt) <= MAX_COMPARE_PROMPT:
        raise ValueError(f"Write a prompt of 1-{MAX_COMPARE_PROMPT} characters")
    raw_tokens = payload.get("max_tokens", 128)
    if type(raw_tokens) is not int or not MIN_COMPARE_TOKENS <= raw_tokens <= MAX_COMPARE_TOKENS:
        raise ValueError(f"max_tokens must be between {MIN_COMPARE_TOKENS} and {MAX_COMPARE_TOKENS}")
    enforce(workspace)  # a comparison spends money like any other call

    def run(ident: str) -> dict[str, Any]:
        from .providers import ProviderRegistry

        results = []
        for name in names:
            cfg = state.config.agents[name]
            attempt = {"agent": name, "provider": cfg.provider, "model": cfg.model, "ok": False,
                       "latency_ms": None, "input_tokens": None, "output_tokens": None,
                       "cost_usd": None, "text": "", "error": "", "price_source": ""}
            started = time.monotonic()
            try:
                import dataclasses

                from .types import Message

                copy = dataclasses.replace(cfg, timeout=COMPARE_TIMEOUT)
                provider = ProviderRegistry({**state.config.agents, name: copy}).get(name)
                completion = provider.complete(
                    "You are a concise assistant. Answer in one short paragraph.",
                    [Message(role="user", content=prompt)], None,
                    max_tokens=raw_tokens, temperature=0.0)
                attempt["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
                attempt["ok"] = True
                attempt["model"] = completion.model or cfg.model
                attempt["input_tokens"] = int(completion.input_tokens or 0)
                attempt["output_tokens"] = int(completion.output_tokens or 0)
                attempt["text"] = str(completion.content or "")[:MAX_COMPARE_TEXT]
                rate = _price_for(state, attempt["model"])
                if rate:
                    attempt["cost_usd"] = float(
                        (decimal.Decimal(str(rate["input_per_million"])) * attempt["input_tokens"]
                         + decimal.Decimal(str(rate["output_per_million"])) * attempt["output_tokens"])
                        / decimal.Decimal(1000000))
                    attempt["price_source"] = rate["source"]
            except Exception as exc:  # noqa: BLE001 - one model failing must not hide the others
                attempt["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
                attempt["error"] = str(exc)[:400]
            results.append(attempt)
        body = {"schema": SCHEMA, "id": ident, "created": time.time(), "prompt": prompt,
                "max_tokens": raw_tokens, "results": results,
                "note": "Real inference on each endpoint. Charges apply. Latency is measured around the "
                        "call, not a provider-reported number."}
        connection = _connect(workspace)
        try:
            connection.execute("INSERT OR REPLACE INTO compare(id, created, prompt, models, status, body)"
                               " VALUES (?,?,?,?,?,?)",
                               (ident, body["created"], prompt, json.dumps(names), "done",
                                json.dumps(body, default=str)))
            connection.commit()
        finally:
            connection.close()
        return body

    job = state.workbench.launch_job("compare", run)
    _audit(state, "cost.compare", "user", {"agents": names, "max_tokens": raw_tokens,
                                           "prompt_chars": len(prompt)}, risk="medium", target="cost")
    return {"schema": SCHEMA, "observed_at": time.time(), "job": job}


def compare_status(state, ident: str) -> dict[str, Any]:
    """Job state while it runs, the stored body once it is done."""
    job = (getattr(state.workbench, "jobs", {}) or {}).get(ident)
    try:
        body = compare_state(state.workspace, ident)
    except ValueError:
        if job is None:
            raise
        return {"schema": SCHEMA, "observed_at": time.time(), "id": ident,
                "status": job.get("status", "unknown"), "started": job.get("started"),
                "result": None, "error": job.get("error", "")}
    return {"schema": SCHEMA, "observed_at": time.time(), "id": ident, "status": "done",
            "started": body.get("created"), "result": body, "error": ""}


def compare_state(workspace: Path | str, ident: str) -> dict[str, Any]:
    workspace = Path(workspace)
    connection = _connect(workspace)
    try:
        row = connection.execute("SELECT body FROM compare WHERE id=?", (ident,)).fetchone()
    finally:
        connection.close()
    if not row:
        raise ValueError("No stored comparison with that id")
    return json.loads(row["body"])


def comparisons(workspace: Path | str, limit: int = 20) -> list[dict[str, Any]]:
    workspace = Path(workspace)
    if not db_path(workspace).exists():
        return []
    limit = max(1, min(int(limit or 20), 100))
    connection = _connect(workspace)
    try:
        rows = connection.execute(
            "SELECT id, created, prompt, models, status FROM compare ORDER BY created DESC LIMIT ?",
            (limit,)).fetchall()
    finally:
        connection.close()
    return [{"id": row["id"], "created": row["created"], "prompt": row["prompt"][:200],
             "models": json.loads(row["models"]), "status": row["status"]} for row in rows]


# ── resources ──────────────────────────────────────────────────────────── #


def resources(state, *, days_hours: int = 24) -> dict[str, Any]:
    """Sample now (rate-limited) and return the stored history."""
    workspace = Path(state.workspace)
    try:
        policy = load_policy(workspace)["resources"]
        error = ""
    except ValueError as exc:
        policy, error = json.loads(json.dumps(DEFAULT_POLICY["resources"])), str(exc)
    sample = None
    if policy["enabled"]:
        sample = _sample(state, policy["interval_s"], policy["keep_hours"])
    if not db_path(workspace).exists():
        history: list[dict[str, Any]] = []
    else:
        connection = _connect(workspace)
        try:
            rows = connection.execute(
                "SELECT at, cpu, ram_used, ram_total, gpu_util, gpu_mem_used, gpu_mem_total FROM resource"
                " WHERE at >= ? ORDER BY at", (time.time() - 86400,)).fetchall()
        finally:
            connection.close()
        history = [dict(row) for row in rows]
    return {"schema": SCHEMA, "observed_at": time.time(), "policy": policy, "error": error,
            "sample": sample, "history": history,
            "note": "Samples are taken while this page is open. An idle dashboard records nothing, "
                    "which is why a gap is honest rather than zero."}


def _sample(state, interval: int, keep_hours: int) -> dict[str, Any] | None:
    workspace = Path(state.workspace)
    last = _SYNC_LOCK.get("sample:" + str(workspace), 0.0)
    if time.time() - last < max(1, interval):
        return None
    _SYNC_LOCK["sample:" + str(workspace)] = time.time()
    measured = state.workbench.metrics()
    data = measured["data"]
    ram = data.get("ram") or {}
    gpu = (data.get("gpu_devices") or [{}])[0]
    at = time.time()
    connection = _connect(workspace)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO resource(at, cpu, ram_used, ram_total, gpu_util, gpu_mem_used,"
            " gpu_mem_total) VALUES (?,?,?,?,?,?,?)",
            (at, data.get("cpu_percent"), ram.get("used"), ram.get("total"),
             gpu.get("utilization_percent"), gpu.get("memory_used_mib"), gpu.get("memory_total_mib")))
        connection.execute("DELETE FROM resource WHERE at < ?", (at - keep_hours * 3600,))
        connection.commit()
    finally:
        connection.close()
    return {"at": at, "cpu": data.get("cpu_percent"), "ram_used": ram.get("used"),
            "ram_total": ram.get("total"), "gpu_util": gpu.get("utilization_percent"),
            "source": measured.get("source"), "error": data.get("measurement_error")}


# ── cleanup and overview ───────────────────────────────────────────────── #


def cleanup(state, payload: dict[str, Any]) -> dict[str, Any]:
    from . import securitycenter

    workspace = Path(state.workspace)
    if str(payload.get("confirmation") or "").strip() != CLEAR_TOKEN:
        raise ValueError(f"Type {CLEAR_TOKEN} to clear the cost history")
    if not db_path(workspace).exists():
        raise ValueError("There is no cost history to clear")
    before = {table: 0 for table in ALLOWED_TABLES}
    connection = _connect(workspace)
    try:
        for table in ALLOWED_TABLES:
            before[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if not any(before[table] for table in ALLOWED_TABLES if table != "setting"):
            raise ValueError("There is no cost history to clear")
        for table in ALLOWED_TABLES:
            connection.execute(f"DELETE FROM {table}")
        _set_setting(connection, "cleared_at", str(time.time()))
        connection.execute("DELETE FROM setting WHERE key = 'last_sync'")
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    _SYNC_LOCK.pop(str(workspace), None)
    securitycenter.audit_action(state, "cost.clear", "user", {"rows": before}, risk="medium", target="cost")
    return {"schema": SCHEMA, "removed": before, "store": {"path": display_path(workspace, db_path(workspace)),
                                                           "bytes": _db_bytes(workspace)}}


def audit_read(state, action: str, client: str, every: float = 120.0) -> None:
    """Sample page reads into the same trail the writes use (never blocks)."""
    from . import securitycenter

    try:
        securitycenter.audit_read(state, f"cost.{action}", client or "unknown", every=every)
    except (OSError, ValueError):
        pass


def _audit(state, action: str, actor: str, detail: dict, *, risk: str = "low", target: str = "dashboard") -> None:
    from . import securitycenter

    try:
        securitycenter.audit_action(state, action, actor, detail, risk=risk, target=target)
    except (OSError, ValueError):
        pass


def overview(state) -> dict[str, Any]:
    workspace = Path(state.workspace)
    sync(workspace)
    view = policy_view(workspace)
    budget = budget_state(workspace)
    today = budget["today"]
    month = budget["month"]
    days = 30
    if db_path(workspace).exists():
        connection = _connect(workspace)
        try:
            runs = connection.execute("SELECT COUNT(*) AS n, COALESCE(SUM(duration_s),0) AS d FROM run"
                                      " WHERE started >= ?", (_window(days),)).fetchone()
            models = connection.execute(
                "SELECT COUNT(DISTINCT model) AS n FROM call WHERE at >= ?", (_window(days),)).fetchone()
            first = connection.execute("SELECT MIN(at) AS first_at FROM call").fetchone()["first_at"]
        finally:
            connection.close()
    else:
        runs, models, first = {"n": 0, "d": 0}, {"n": 0}, None
    latency_all = latency(workspace, 7, "model")
    return {
        "schema": SCHEMA, "observed_at": time.time(), "demo": bool(getattr(state, "demo", False)),
        "policy": view["policy"], "policy_error": view["error"], "revision": view["revision"],
        "file": view["file"], "file_exists": view["file_exists"], "tokens": view["tokens"],
        "limits": view["limits"], "budget": budget,
        "totals": {"runs_30d": int(runs["n"]), "duration_30d": round(float(runs["d"] or 0), 1),
                   "models_30d": int(models["n"]), "calls_mtd": month["calls"],
                   "first_sample_at": first},
        "spend": {"today": today, "month": month,
                  "priced_share": (round(today["priced"] / today["calls"], 3) if today["calls"] else None)},
        "latency": latency_all,
        "store": {"path": display_path(workspace, db_path(workspace)), "exists": db_path(workspace).exists(),
                  "bytes": _db_bytes(workspace)},
        "notes": ["traces_only", "unknown_is_not_free", "latency_wire_to_response", "resources_while_open"],
    }


def breakdown(state, days: int = 7, group: str = "model") -> dict[str, Any]:
    workspace = Path(state.workspace)
    _window(days)
    if group not in {"model", "agent", "role", "day"}:
        raise ValueError("Group by model, agent, role or day")
    sync(workspace)
    rows = _rows(workspace, days, group) if db_path(workspace).exists() else []
    return {"schema": SCHEMA, "observed_at": time.time(), "days": days, "group": group, "rows": rows,
            "totals": {"calls": sum(row["calls"] for row in rows),
                       "usd": round(sum(row["usd"] for row in rows), 6),
                       "unpriced": sum(row["unpriced"] for row in rows)}}


def latency_view(state, days: int = 7, group: str = "model") -> dict[str, Any]:
    workspace = Path(state.workspace)
    _window(days)
    if group not in {"model", "agent", "role"}:
        raise ValueError("Group latency by model, agent or role")
    sync(workspace)
    found = latency(workspace, days, group)
    return {"schema": SCHEMA, "observed_at": time.time(), "days": days, **found}


def report(state, days: int = 7) -> dict[str, Any]:
    """Every table the page opens with, in one round trip and one sync."""
    workspace = Path(state.workspace)
    sync(workspace)
    _window(days)
    if db_path(workspace).exists():
        rows = {group: _rows(workspace, days, group) for group in ("model", "agent", "role", "day")}
        measured = latency(workspace, days, "model")
    else:
        rows = {group: [] for group in ("model", "agent", "role", "day")}
        measured = latency(workspace, days, "model")
    totals = {group: {"calls": sum(row["calls"] for row in items),
                      "usd": round(sum(row["usd"] for row in items), 6),
                      "unpriced": sum(row["unpriced"] for row in items)} for group, items in rows.items()}
    return {"schema": SCHEMA, "observed_at": time.time(), "days": days,
            "by_model": rows["model"], "by_agent": rows["agent"], "by_role": rows["role"],
            "by_day": rows["day"], "totals": totals, "latency": measured}
