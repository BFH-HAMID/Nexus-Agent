"""Router Studio: inspect, test and tune how a task becomes a role.

The runtime router (`nexus/router.py`) is deliberately small and offline: it
scores every role's keywords, defers ties to a cheap classifier, and falls back
to ``general``. This module is the reviewed surface around it:

``GET  /api/router/overview``  policy, live settings, roster counts, chains
``GET  /api/router/rules``     the editable rule book plus every role name
``GET  /api/router/chains``    each agent's fallback ladder, validated
``GET  /api/router/models``    cost/speed/quality weighted model ranking
``GET  /api/router/accuracy``  explicit feedback + reported run outcomes
``GET  /api/router/history``   recorded policy versions (newest first)
``GET  /api/router/lab``       the last offline routing test runs
``POST /api/router/simulate``  one dry run - no provider is constructed
``POST /api/router/rule``      prepare/save/remove/toggle one routing rule
``POST /api/router/weights``   save the three decision weights
``POST /api/router/enable``    master switch for custom rules
``POST /api/router/lab``       score a batch of tasks against expected roles
``POST /api/router/restore``   restore a recorded policy version

Everything the panel can change lives in ``<workspace>/.nexus/router`` - runtime
state, never committed. A missing or damaged policy file is ignored, so the
built-in router keeps working; rules are trusted only after the shape, the
role name and the typed confirmation have been checked.

No function here calls a model. ``provider_calls`` in every preview is a real
zero, and the classifier stage is only *predicted*, never invoked.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .types import VALID_ROLE_RE

SCHEMA = 1
POLICY_SCHEMA = 1
STORE_DIR = ".nexus/router"
POLICY_FILE = "policy.json"
LAB_FILE = "lab.json"
HISTORY_DIR = "history"
MAX_RULES = 60
MAX_RULE_PHRASE = 160
MAX_RULE_NOTE = 200
MAX_TASK = 4000
MAX_CASES = 25
MAX_CASE_TASK = 500
MAX_VERSIONS = 20
MAX_LAB_RUNS = 10
MAX_CANDIDATES = 8
MAX_CHAIN = 8
WEIGHTS = ("cost", "speed", "quality")
DEFAULT_WEIGHTS = {"cost": 40, "speed": 40, "quality": 20}
MODES = ("contains", "words")
RULE_ID_RE = re.compile(r"^r-[0-9a-f]{8}$")
VERSION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{8}(?:-[0-9]+)?$")

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, int, Policy]] = {}
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


def _number(value: Any, low: float, high: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{label} must be between {low:g} and {high:g}")
    return round(number, 2)


def _integer(value: Any, low: int, high: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be a whole number")
    if not low <= value <= high:
        raise ValueError(f"{label} must be between {low} and {high}")
    return value


def _store(workspace: Path) -> Path:
    return Path(workspace) / STORE_DIR


def policy_path(workspace: Path) -> Path:
    return _store(workspace) / POLICY_FILE


def lab_path(workspace: Path) -> Path:
    return _store(workspace) / LAB_FILE


def history_path(workspace: Path) -> Path:
    return _store(workspace) / HISTORY_DIR


def _mask_values(state) -> list[str]:
    """Workspace secrets that must never reach the browser (token included)."""
    from .redaction import secrets

    values = [str(getattr(state, "token", "") or "")] + list(secrets(getattr(state, "workspace", None)))
    return [value for value in values if isinstance(value, str) and len(value) >= 8]


def _clean(state, value: Any) -> Any:
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


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    """Write one bounded JSON document, atomically and owner-only."""
    parent = path.parent
    if parent.is_symlink() or path.is_symlink():
        raise ValueError("Routing state symlinks are forbidden")
    parent.mkdir(parents=True, exist_ok=True)
    if not parent.resolve().is_relative_to(parent.parent.resolve()):
        raise ValueError("Routing state must stay inside the workspace")
    fd, temp = tempfile.mkstemp(dir=str(parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _read_json(path: Path, limit: int = 500_000) -> Any:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Routing state must be a bounded regular file")
    return json.loads(path.read_text(encoding="utf-8"))


# ── rules and policy ───────────────────────────────────────────────────── #


@dataclass(frozen=True)
class Rule:
    id: str
    phrase: str
    role: str
    weight: float = 2.0
    priority: int = 5
    mode: str = "contains"
    enabled: bool = True
    note: str = ""
    created_at: float | None = None

    def matches(self, task: str) -> bool:
        haystack = (task or "").casefold()
        if not haystack or not self.phrase:
            return False
        if self.mode == "words":
            words = {piece for piece in re.split(r"[^\w]+", haystack) if piece}
            parts = [piece for piece in re.split(r"[^\w]+", self.phrase.casefold()) if piece]
            return bool(parts) and all(piece in words for piece in parts)
        return self.phrase.casefold() in haystack

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phrase": self.phrase,
            "role": self.role,
            "weight": self.weight,
            "priority": self.priority,
            "mode": self.mode,
            "enabled": self.enabled,
            "note": self.note,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Policy:
    enabled: bool
    weights: dict[str, int]
    rules: tuple[Rule, ...]
    updated_at: float | None = None
    error: str = ""
    revision: str = ""

    @property
    def enabled_rules(self) -> tuple[Rule, ...]:
        if not self.enabled:
            return ()
        return tuple(rule for rule in self.rules if rule.enabled)


def revision_of(enabled: bool, weights: dict[str, int], rules: tuple[Rule, ...] | list[Rule]) -> str:
    payload = {
        "schema": POLICY_SCHEMA,
        "enabled": bool(enabled),
        "weights": {key: int(weights.get(key, DEFAULT_WEIGHTS[key])) for key in WEIGHTS},
        "rules": [rule.to_dict() if isinstance(rule, Rule) else dict(rule) for rule in rules],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()[:16]


def _default_policy(error: str = "") -> Policy:
    weights = dict(DEFAULT_WEIGHTS)
    return Policy(enabled=False, weights=weights, rules=(), updated_at=None, error=error,
                  revision=revision_of(False, weights, ()))


def _weight_value(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Weight {label} must be a whole number 0-100")
    if not 0 <= value <= 100:
        raise ValueError(f"Weight {label} must be between 0 and 100")
    return value


def _rule_phrase(value: Any) -> str:
    return _text(value, MAX_RULE_PHRASE, "Rule phrase")


def _rule_role(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Rule needs a target role")
    role = value.strip().lower()
    if not role or not VALID_ROLE_RE.fullmatch(role):
        raise ValueError("Rule target must be a role slug (lowercase letters, digits and dashes)")
    return role


def _rule_id(value: Any) -> str:
    if not isinstance(value, str) or not RULE_ID_RE.fullmatch(value):
        raise ValueError("Rule id must look like r-0123abcd")
    return value


def _rule_mode(value: Any) -> str:
    if value not in MODES:
        raise ValueError("Rule mode must be 'contains' or 'words'")
    return str(value)


def propose_id(role: str, phrase: str, mode: str = "contains") -> str:
    """Stable id for one (role, phrase, mode); editing a rule keeps its id."""
    digest = hashlib.sha256(f"{role}\0{phrase.casefold()}\0{mode}".encode()).hexdigest()
    return "r-" + digest[:8]


def compile_rule(raw: Any) -> Rule:
    """Shape-only validation used when reading the file; roles are checked later."""
    if not isinstance(raw, dict):
        raise ValueError("Each rule must be a JSON object")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("Rule enabled must be true or false")
    note = raw.get("note", "")
    if not isinstance(note, str) or len(note) > MAX_RULE_NOTE:
        raise ValueError(f"Rule note must be at most {MAX_RULE_NOTE} characters")
    created = raw.get("created_at")
    created_at = float(created) if isinstance(created, (int, float)) and not isinstance(created, bool) else None
    return Rule(
        id=_rule_id(raw.get("id")),
        phrase=_rule_phrase(raw.get("phrase")),
        role=_rule_role(raw.get("role")),
        weight=_number(raw.get("weight", 2.0), 0.5, 10.0, "Rule weight"),
        priority=_integer(raw.get("priority", 5), 1, 9, "Rule priority"),
        mode=_rule_mode(raw.get("mode", "contains")),
        enabled=enabled,
        note=note,
        created_at=created_at,
    )


def compile_policy(raw: Any) -> Policy:
    """Parse a stored policy. Structural damage is fatal; one bad rule is not."""
    if not isinstance(raw, dict):
        raise ValueError("Routing policy must be a JSON object")
    if raw.get("schema") != POLICY_SCHEMA:
        raise ValueError("Unsupported routing policy schema")
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("Routing policy enabled must be true or false")
    weights = raw.get("weights")
    if not isinstance(weights, dict) or set(weights) - set(WEIGHTS):
        raise ValueError("Routing weights must name cost, speed and quality only")
    parsed = {key: _weight_value(weights.get(key, DEFAULT_WEIGHTS[key]), key) for key in WEIGHTS}
    if not any(parsed.values()):
        raise ValueError("At least one routing weight must be above zero")
    raw_rules = raw.get("rules", [])
    if not isinstance(raw_rules, list) or len(raw_rules) > MAX_RULES:
        raise ValueError(f"Routing rules must be a list of at most {MAX_RULES} entries")
    rules: list[Rule] = []
    seen: set[str] = set()
    ignored = 0
    for item in raw_rules:
        try:
            rule = compile_rule(item)
        except ValueError:
            ignored += 1
            continue
        if rule.id in seen:
            ignored += 1
            continue
        seen.add(rule.id)
        rules.append(rule)
    updated = raw.get("updated_at")
    updated_at = float(updated) if isinstance(updated, (int, float)) and not isinstance(updated, bool) else None
    return Policy(enabled=enabled, weights=parsed, rules=tuple(rules), updated_at=updated_at,
                  error=f"{ignored} malformed rule(s) were ignored." if ignored else "",
                  revision=revision_of(enabled, parsed, rules))


def load_policy(workspace: Path) -> Policy:
    """Read the policy, cached by (mtime, size). A bad file never breaks routing."""
    path = policy_path(workspace)
    try:
        stat = path.stat()
    except OSError:
        return _default_policy()
    key = str(path)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == stat.st_mtime and hit[1] == stat.st_size:
            return hit[2]
    try:
        raw = _read_json(path)
        if raw is None:
            policy = _default_policy()
        else:
            policy = compile_policy(raw)
    except (OSError, ValueError) as exc:
        policy = _default_policy(error=f"Routing policy ignored ({type(exc).__name__}).")
    with _cache_lock:
        _cache[key] = (stat.st_mtime, stat.st_size, policy)
    return policy


def active_rules(workspace: Path) -> tuple[Rule, ...]:
    """Rules the live router may use. Empty when the master switch is off."""
    return load_policy(workspace).enabled_rules


def _forget_cache(workspace: Path) -> None:
    with _cache_lock:
        _cache.pop(str(policy_path(workspace)), None)


def _write_policy(state, *, enabled: bool, weights: dict[str, int], rules: list[Rule], reason: str) -> Policy:
    if getattr(state, "demo", False):
        raise ValueError("Routing policy cannot change in demo mode")
    candidate = Policy(enabled=enabled, weights={key: _weight_value(weights.get(key, DEFAULT_WEIGHTS[key]), key)
                                                  for key in WEIGHTS},
                       rules=tuple(rules), updated_at=time.time(), error="")
    if not any(candidate.weights.values()):
        raise ValueError("At least one routing weight must be above zero")
    if len(candidate.rules) > MAX_RULES:
        raise ValueError(f"At most {MAX_RULES} rules can be stored")
    workspace = Path(state.workspace)
    previous = load_policy(workspace)
    if policy_path(workspace).exists():
        _snapshot(workspace, previous, reason)
    _atomic_json(policy_path(workspace), {
        "schema": POLICY_SCHEMA,
        "enabled": candidate.enabled,
        "weights": candidate.weights,
        "rules": [rule.to_dict() for rule in candidate.rules],
        "updated_at": candidate.updated_at,
    })
    _forget_cache(workspace)
    return load_policy(workspace)


# ── history ────────────────────────────────────────────────────────────── #


def _snapshot(workspace: Path, policy: Policy, reason: str) -> str | None:
    if policy.updated_at is None and not policy.rules:
        return None
    directory = history_path(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    base = f"{stamp}-{policy.revision[:8]}"
    version = base
    suffix = 1
    while (directory / f"{version}.json").exists():
        version = f"{base}-{suffix}"
        suffix += 1
    _atomic_json(directory / f"{version}.json", {
        "schema": SCHEMA,
        "reason": reason[:120],
        "saved_at": time.time(),
        "policy": {
            "schema": POLICY_SCHEMA,
            "enabled": policy.enabled,
            "weights": policy.weights,
            "rules": [rule.to_dict() for rule in policy.rules],
            "updated_at": policy.updated_at,
        },
    })
    _prune_history(workspace)
    return version


def _prune_history(workspace: Path) -> None:
    directory = history_path(workspace)
    files = sorted(directory.glob("*.json"), reverse=True)
    for stale in files[MAX_VERSIONS:]:
        try:
            stale.unlink()
        except OSError:
            pass


def versions(workspace: Path) -> list[dict[str, Any]]:
    directory = history_path(workspace)
    if not directory.exists():
        return []
    rows: list[dict[str, Any]] = []
    damaged = 0
    for path in sorted(directory.glob("*.json"), reverse=True)[:MAX_VERSIONS]:
        try:
            data = _read_json(path, 200_000)
            policy = compile_policy((data or {}).get("policy"))
        except (OSError, ValueError):
            damaged += 1
            continue
        rows.append({
            "id": path.stem,
            "at": float((data or {}).get("saved_at") or 0),
            "reason": str((data or {}).get("reason") or ""),
            "revision": policy.revision,
            "rules": len(policy.rules),
            "enabled": policy.enabled,
        })
    rows.sort(key=lambda row: (row["at"], row["id"]), reverse=True)
    if damaged:
        rows.append({"id": "", "at": 0, "reason": f"{damaged} damaged version file(s) ignored", "revision": "",
                     "rules": 0, "enabled": False, "damaged": True})
    return rows


def restore(state, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required")
    version = payload.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ValueError("A recorded version id is required")
    if getattr(state, "demo", False):
        raise ValueError("Routing policy cannot change in demo mode")
    path = history_path(Path(state.workspace)) / f"{version}.json"
    if not path.exists():
        raise ValueError("No such recorded routing version")
    try:
        data = _read_json(path, 200_000)
        restored = compile_policy((data or {}).get("policy"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"That routing version is unreadable ({type(exc).__name__})") from exc
    policy = load_policy(Path(state.workspace))
    expected = payload.get("confirmation")
    if expected != f"RESTORE ROUTER {version}":
        raise ValueError(f"Type RESTORE ROUTER {version} to confirm")
    if payload.get("revision") not in (None, policy.revision):
        raise ValueError("The routing policy changed since this page loaded; refresh and review again.")
    updated = _write_policy(state, enabled=restored.enabled, weights=restored.weights,
                            rules=list(restored.rules), reason=f"restore {version}")
    audit(state, _client(payload, state), "routerstudio.restore", every=0)
    return {"schema": SCHEMA, "observed_at": time.time(), "restored": version,
            "policy": policy_view(state, updated), "revision": updated.revision}


# ── read surfaces ──────────────────────────────────────────────────────── #


def _client(payload: dict[str, Any] | None, state) -> str:
    value = (payload or {}).get("_client") if isinstance(payload, dict) else ""
    return str(value or "local")[:120]


def policy_view(state, policy: Policy | None = None) -> dict[str, Any]:
    policy = policy or load_policy(Path(state.workspace))
    return {
        "schema": SCHEMA,
        "enabled": policy.enabled,
        "weights": dict(policy.weights),
        "rules": [rule.to_dict() for rule in policy.rules],
        "revision": policy.revision,
        "updated_at": policy.updated_at,
        "error": policy.error,
        "limits": {"rules": MAX_RULES, "phrase": MAX_RULE_PHRASE, "note": MAX_RULE_NOTE,
                   "min_weight": 0.5, "max_weight": 10.0, "min_priority": 1, "max_priority": 9},
        "store": f"{STORE_DIR}/{POLICY_FILE} (workspace runtime state, never committed)",
    }


def overview(state) -> dict[str, Any]:
    cfg = state.config
    policy = load_policy(Path(state.workspace))
    settings = cfg.settings
    try:
        local_only = bool(state.workbench.settings().get("local_only"))
    except (OSError, ValueError):
        local_only = None
    chains_data = chains(state)
    lab_data = lab(state)
    return _clean(state, {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "demo": bool(getattr(state, "demo", False)),
        "policy": policy_view(state, policy),
        "settings": {
            "auto_route": bool(settings.auto_route),
            "router_min_score": float(settings.router_min_score),
            "router_llm_fallback": bool(settings.router_llm_fallback),
            "router_agent": settings.router_agent or "",
            "classifier_agent": settings.router_agent or cfg.default_agent,
            "default_agent": cfg.default_agent,
            "prefer_local": bool(settings.prefer_local),
            "local_only": local_only,
        },
        "roster": {
            "roles": len(cfg.roles.roles),
            "agents": len(cfg.agents),
            "categories": len({role.category for role in cfg.roles.roles.values() if role.category}),
            "keywords": sum(len(role.keywords) for role in cfg.roles.roles.values()),
        },
        "chains": chains_data["summary"],
        "lab": None if not lab_data["runs"] else {
            "id": lab_data["runs"][0]["id"], "at": lab_data["runs"][0]["at"],
            "cases": lab_data["runs"][0]["total"], "accuracy": lab_data["runs"][0]["accuracy"],
        },
        "source": "agents.yaml, roles.yaml and the workspace routing policy; nothing is guessed",
    })


def rules(state) -> dict[str, Any]:
    policy = load_policy(Path(state.workspace))
    return _clean(state, {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "revision": policy.revision,
        "enabled": policy.enabled,
        "rules": [rule.to_dict() for rule in policy.rules],
        "role_names": sorted(state.config.roles.roles),
        "limits": {"rules": MAX_RULES, "phrase": MAX_RULE_PHRASE, "note": MAX_RULE_NOTE},
        "error": policy.error,
        "store": f"{STORE_DIR}/{POLICY_FILE}",
    })


def chains(state) -> dict[str, Any]:
    """Every agent's fallback ladder, read-only, with its real hazards."""
    cfg = state.config
    from .workbench import local_endpoint

    rows = []
    broken = 0
    loops = 0
    missing: set[str] = set()
    for name in sorted(cfg.agents):
        issues: list[dict[str, str]] = []
        nodes: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor = name
        while cursor:
            if cursor in seen:
                issues.append({"code": "loop", "agent": cursor,
                               "message": f"{cursor} is already in this ladder; the runtime stops there"})
                loops += 1
                break
            seen.add(cursor)
            agent = cfg.agents.get(cursor)
            if agent is None:
                issues.append({"code": "unknown", "agent": cursor,
                               "message": f"fallback names an agent that does not exist: {cursor}"})
                nodes.append({"name": cursor, "known": False})
                broken += 1
                break
            local = bool(agent.effective_base_url) and local_endpoint(agent.effective_base_url or "")
            node = {
                "name": agent.name,
                "known": True,
                "provider": agent.provider,
                "model": agent.model,
                "local": local,
                "key_present": bool(agent.resolved_api_key),
                "optional": bool(agent.optional),
                "is_router": agent.name == (cfg.settings.router_agent or cfg.default_agent),
                "is_default": agent.name == cfg.default_agent,
            }
            if not node["key_present"] and not local:
                missing.add(agent.name)
                issues.append({"code": "missing_key", "agent": agent.name,
                               "message": f"no credential is configured for {agent.name}"})
            nodes.append(node)
            if not agent.fallback:
                break
            if agent.fallback not in cfg.agents:
                issues.append({"code": "unknown", "agent": agent.fallback,
                               "message": f"{agent.name} falls back to an unknown agent: {agent.fallback}"})
                nodes.append({"name": agent.fallback, "known": False})
                broken += 1
                break
            cursor = agent.fallback
            if len(nodes) >= MAX_CHAIN:
                issues.append({"code": "long", "agent": agent.name,
                               "message": f"chain is longer than {MAX_CHAIN} hops"})
                break
        rows.append({"agent": name, "nodes": nodes, "issues": issues, "length": len(nodes)})
    return _clean(state, {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "agents": rows,
        "summary": {
            "agents": len(rows),
            "with_fallback": sum(1 for row in rows if row["length"] > 1),
            "broken": broken,
            "loops": loops,
            "missing_keys": len(missing),
            "router_agent": cfg.settings.router_agent or cfg.default_agent,
            "default_agent": cfg.default_agent,
        },
        "note": "Read-only view of agents.yaml. Fallbacks are edited in the Workbench; nothing here calls a model.",
    })


# ── model ranking (cost / speed / quality) ─────────────────────────────── #


def _rate(pricing: Any, model: str) -> dict[str, Any] | None:
    data = pricing.get(model) if isinstance(pricing, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("source"), str) or not data["source"].strip():
        return None
    values = []
    for key in ("input_per_million", "output_per_million"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(float(value)) or float(value) < 0:
            return None
        values.append(float(value))
    return {"source": data["source"][:120], "input_per_million": values[0], "output_per_million": values[1],
            "per_million": values[0] + values[1]}


def _normalise(values: dict[str, float | None], *, lower_better: bool) -> dict[str, float]:
    known = [value for value in values.values() if value is not None]
    if not known:
        return {}
    low, high = min(known), max(known)
    if high == low:
        return {name: 1.0 for name, value in values.items() if value is not None}
    span = high - low
    return {name: (1.0 - (value - low) / span) if lower_better else ((value - low) / span)
            for name, value in values.items() if value is not None}


def model_ranking(state) -> dict[str, Any]:
    """Rank configured models on operator prices, session duration and reported outcomes.

    Missing measurements are ``null`` and reduce the coverage of a row; they are
    never replaced by a guess. The weights only order this table - they do not
    change a live request.
    """
    policy = load_policy(Path(state.workspace))
    try:
        pricing = state.workbench.settings().get("pricing") or {}
    except (OSError, ValueError):
        pricing = {}
    stats = routing_stats(state)
    measured = {row["agent"]: row for row in stats["agents"]}
    rows = []
    for name, agent in sorted(state.config.agents.items()):
        rate = _rate(pricing, agent.model)
        observation = measured.get(name) or {}
        duration = observation.get("avg_duration_s")
        quality = observation.get("reported_ok_rate")
        rows.append({
            "agent": name,
            "provider": agent.provider,
            "model": agent.model,
            "price": rate,
            "cost_basis": None if rate is None else rate["per_million"],
            "avg_duration_s": duration,
            "runs_observed": int(observation.get("runs") or 0),
            "reported_ok_rate": quality,
            "key_present": bool(agent.resolved_api_key),
            "coverage": 0,
            "score": None,
            "dimensions": {},
        })
    cost_norm = _normalise({row["agent"]: row["cost_basis"] for row in rows}, lower_better=True)
    speed_norm = _normalise({row["agent"]: row["avg_duration_s"] for row in rows}, lower_better=True)
    quality_norm = _normalise({row["agent"]: row["reported_ok_rate"] for row in rows}, lower_better=False)
    weights = policy.weights
    for row in rows:
        dimensions = {
            "cost": cost_norm.get(row["agent"]),
            "speed": speed_norm.get(row["agent"]),
            "quality": quality_norm.get(row["agent"]),
        }
        row["dimensions"] = {key: (None if value is None else round(value, 3)) for key, value in dimensions.items()}
        available = {key: value for key, value in dimensions.items() if value is not None}
        row["coverage"] = len(available)
        total_weight = sum(weights[key] for key in available)
        if total_weight:
            row["score"] = round(100.0 * sum(weights[key] * value for key, value in available.items()) / total_weight, 1)
    rows.sort(key=lambda row: (row["score"] is None, -(row["score"] or 0.0), -row["coverage"], row["agent"]))
    recommendation = None
    for row in rows:
        if row["score"] is not None and row["coverage"] >= 2:
            recommendation = {"agent": row["agent"], "score": row["score"], "coverage": row["coverage"],
                              "dimensions": [key for key, value in row["dimensions"].items() if value is not None]}
            break
    return _clean(state, {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "weights": dict(weights),
        "agents": rows,
        "recommendation": recommendation,
        "current": state.config.settings.router_agent or state.config.default_agent,
        "pricing_configured": sum(1 for row in rows if row["price"]),
        "basis": ("Operator-entered prices (/api/workbench/settings); average session duration from recorded runs "
                  "(not model latency); reported outcomes from sessions.db (agent-reported, not independent verification)."),
        "limits": "Weights only order this table. Nothing here changes agents.yaml or a live request.",
    })


# ── statistics from sessions.db ────────────────────────────────────────── #


def routing_stats(state) -> dict[str, Any]:
    from .sessions import SessionStore

    try:
        return SessionStore().routing_stats(state.workspace, days=90)
    except (OSError, ValueError) as exc:
        return {
            "days": 90, "totals": {"runs": 0, "reported_ok": 0, "duration_known": 0, "duration_s": 0.0},
            "roles": [], "agents": [], "truncated_roles": False, "truncated_agents": False,
            "feedback": {"observations": 0, "approved": 0, "approval_rate": None, "last_at": None, "roles": []},
            "error": f"Run history unavailable ({type(exc).__name__}).",
            "source": "sessions.db (isolated workspace store)",
        }


def accuracy(state) -> dict[str, Any]:
    stats = routing_stats(state)
    last_lab = lab(state)["runs"]
    feedback = stats["feedback"]
    roles = [{**row, "source": "recorded sessions + explicit feedback; not independent verification"}
             for row in stats["roles"][:20]]
    return _clean(state, {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "window_days": stats.get("days", 90),
        "totals": stats["totals"],
        "feedback": feedback,
        "roles": roles,
        "lab": None if not last_lab else last_lab[0],
        "checked": [
            {"code": "feedback",
             "text": "explicit routing feedback recorded in sessions.db"},
            {"code": "outcomes",
             "text": "reported run outcomes grouped by role and agent"},
            {"code": "lab",
             "text": "offline keyword/rule accuracy from the Router Lab"},
        ],
        "not_checked": [
            {"code": "classifier",
             "text": "classifier-stage decisions (the router never records which model answer was used per run)"},
            {"code": "best_role",
             "text": "whether a successful run used the best possible role (no independent grader exists)"},
            {"code": "latency",
             "text": "model latency (only whole-session duration is recorded)"},
            {"code": "invoices",
             "text": "provider invoices (prices are operator-entered, cost is not billed usage)"},
        ],
        "source": stats.get("source", "sessions.db"),
        "error": stats.get("error", ""),
    })


# ── dry runs and the lab ───────────────────────────────────────────────── #


def _resolve(registry, name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Choose a role")
    role = registry._resolve(name.strip())
    if role is None:
        raise ValueError(f"Unknown role: {name.strip()[:80]}")
    return role.name


def normalise_rule(state, raw: Any) -> dict[str, Any]:
    """Validate one rule against this workspace's roster and policy."""
    if not isinstance(raw, dict):
        raise ValueError("Rule must be a JSON object")
    phrase = _rule_phrase(raw.get("phrase"))
    role = _resolve(state.config.roles, raw.get("role"))
    mode = _rule_mode(raw.get("mode", "contains"))
    weight = _number(raw.get("weight", 2.0), 0.5, 10.0, "Rule weight")
    priority = _integer(raw.get("priority", 5), 1, 9, "Rule priority")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("Rule enabled must be true or false")
    note = raw.get("note", "")
    if not isinstance(note, str) or len(note) > MAX_RULE_NOTE:
        raise ValueError(f"Rule note must be at most {MAX_RULE_NOTE} characters")
    raw_id = raw.get("id")
    rule_id = _rule_id(raw_id) if raw_id else propose_id(role, phrase, mode)
    return {"id": rule_id, "phrase": phrase, "role": role, "mode": mode, "weight": weight,
            "priority": priority, "enabled": enabled, "note": note.strip()}


def _duplicate(policy: Policy, rule: dict[str, Any]) -> Rule | None:
    for existing in policy.rules:
        if existing.id == rule["id"]:
            continue
        if existing.role == rule["role"] and existing.phrase.casefold() == rule["phrase"].casefold():
            return existing
    return None


def prepare_rule(state, payload: dict[str, Any]) -> dict[str, Any]:
    policy = load_policy(Path(state.workspace))
    rule = normalise_rule(state, payload.get("rule"))
    duplicate = _duplicate(policy, rule)
    if duplicate is not None:
        raise ValueError(f"A rule for this phrase and role already exists ({duplicate.id}).")
    existing = next((item for item in policy.rules if item.id == rule["id"]), None)
    if existing is None and len(policy.rules) >= MAX_RULES:
        raise ValueError(f"At most {MAX_RULES} rules can be stored; remove one first.")
    return _clean(state, {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "rule": rule,
        "id": rule["id"],
        "replaces": existing.id if existing else None,
        "confirmation": f"SAVE RULE {rule['id']}",
        "revision": policy.revision,
        "note": "Nothing has been saved yet. Confirm to write the rule.",
    })


def rule_action(state, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required")
    action = payload.get("action")
    if action == "prepare":
        return prepare_rule(state, payload)
    policy = load_policy(Path(state.workspace))
    if payload.get("revision") != policy.revision:
        raise ValueError("The routing policy changed since this page loaded; refresh and review again.")
    if action == "save":
        rule = normalise_rule(state, payload.get("rule"))
        duplicate = _duplicate(policy, rule)
        if duplicate is not None:
            raise ValueError(f"A rule for this phrase and role already exists ({duplicate.id}).")
        expected = f"SAVE RULE {rule['id']}"
        if payload.get("confirmation") != expected:
            raise ValueError(f"Type {expected} to confirm")
        existing = next((item for item in policy.rules if item.id == rule["id"]), None)
        if existing is None and len(policy.rules) >= MAX_RULES:
            raise ValueError(f"At most {MAX_RULES} rules can be stored; remove one first.")
        rule["created_at"] = existing.created_at if existing and existing.created_at else time.time()
        updated = list(policy.rules)
        if existing is None:
            updated.append(compile_rule(rule))
            saved = "created"
        else:
            index = updated.index(existing)
            updated[index] = compile_rule(rule)
            saved = "updated"
        written = _write_policy(state, enabled=policy.enabled, weights=policy.weights, rules=updated,
                                reason=f"save rule {rule['id']}")
        audit(state, _client(payload, state), "routerstudio.rule.save", every=0)
        return _clean(state, {"schema": SCHEMA, "observed_at": time.time(), "saved": saved, "rule": rule,
                              "policy": policy_view(state, written), "revision": written.revision})
    if action in {"remove", "enable", "disable"}:
        rule_id = _rule_id(payload.get("id"))
        existing = next((item for item in policy.rules if item.id == rule_id), None)
        if existing is None:
            raise ValueError("No such routing rule")
        verb = {"remove": "REMOVE", "enable": "ENABLE", "disable": "DISABLE"}[action]
        expected = f"{verb} RULE {rule_id}"
        if payload.get("confirmation") != expected:
            raise ValueError(f"Type {expected} to confirm")
        if action == "remove":
            updated = [item for item in policy.rules if item.id != rule_id]
        else:
            replacement = Rule(id=existing.id, phrase=existing.phrase, role=existing.role, weight=existing.weight,
                               priority=existing.priority, mode=existing.mode, enabled=action == "enable",
                               note=existing.note, created_at=existing.created_at)
            updated = [replacement if item.id == rule_id else item for item in policy.rules]
        reason = {"remove": f"remove rule {rule_id}", "enable": f"enable rule {rule_id}",
                  "disable": f"disable rule {rule_id}"}[action]
        written = _write_policy(state, enabled=policy.enabled, weights=policy.weights, rules=updated, reason=reason)
        audit(state, _client(payload, state), f"routerstudio.rule.{action}", every=0)
        return _clean(state, {"schema": SCHEMA, "observed_at": time.time(), "saved": action,
                              "policy": policy_view(state, written), "revision": written.revision})
    raise ValueError("Unknown rule action")


def save_weights(state, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required")
    policy = load_policy(Path(state.workspace))
    if payload.get("revision") != policy.revision:
        raise ValueError("The routing policy changed since this page loaded; refresh and review again.")
    if payload.get("confirmation") != "SAVE WEIGHTS":
        raise ValueError("Type SAVE WEIGHTS to confirm")
    weights = payload.get("weights")
    if not isinstance(weights, dict):
        raise ValueError("Weights must be an object with cost, speed and quality")
    parsed = {key: _weight_value(weights.get(key, policy.weights[key]), key) for key in WEIGHTS}
    if not any(parsed.values()):
        raise ValueError("At least one weight must be above zero")
    written = _write_policy(state, enabled=policy.enabled, weights=parsed, rules=list(policy.rules),
                            reason="save weights")
    audit(state, _client(payload, state), "routerstudio.weights", every=0)
    return _clean(state, {"schema": SCHEMA, "observed_at": time.time(), "weights": parsed,
                          "ranking": model_ranking(state), "policy": policy_view(state, written),
                          "revision": written.revision})


def set_enabled(state, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required")
    policy = load_policy(Path(state.workspace))
    if payload.get("revision") != policy.revision:
        raise ValueError("The routing policy changed since this page loaded; refresh and review again.")
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    expected = "ENABLE RULES" if enabled else "DISABLE RULES"
    if payload.get("confirmation") != expected:
        raise ValueError(f"Type {expected} to confirm")
    written = _write_policy(state, enabled=enabled, weights=policy.weights, rules=list(policy.rules),
                            reason="enable rules" if enabled else "disable rules")
    audit(state, _client(payload, state), f"routerstudio.rules.{'enable' if enabled else 'disable'}", every=0)
    return _clean(state, {"schema": SCHEMA, "observed_at": time.time(), "enabled": enabled,
                          "policy": policy_view(state, written), "revision": written.revision})


def _scored_rows(state, task: str, rules: list[Rule]) -> list[dict[str, Any]]:
    rows = []
    for role in state.config.roles.roles.values():
        score, hits = role.keyword_score(task)
        rows.append({"role": role.name, "score": float(score), "bonus": 0.0, "hits": list(hits),
                     "rules": [], "priority": 0})
    index = {row["role"]: row for row in rows}
    matched = []
    for rule in rules:
        entry = index.get(rule.role)
        if entry is None or not rule.enabled or not rule.matches(task):
            continue
        entry["bonus"] += rule.weight
        entry["rules"].append(rule.id)
        entry["hits"].append(f"rule:{rule.id}")
        entry["priority"] = max(entry["priority"], rule.priority)
        matched.append({"id": rule.id, "phrase": rule.phrase, "role": rule.role,
                        "weight": rule.weight, "priority": rule.priority})
    rows = [row for row in rows if row["score"] + row["bonus"] > 0]
    for row in rows:
        row["total"] = round(row["score"] + row["bonus"], 3)
    rows.sort(key=lambda row: (-row["total"], -row["priority"], row["role"]))
    return rows, matched


def simulate(state, payload: dict[str, Any]) -> dict[str, Any]:
    """One offline dry run. No provider is constructed, so no model is called."""
    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required")
    task = _text(payload.get("task"), MAX_TASK, "Task")
    policy = load_policy(Path(state.workspace))
    rules = list(policy.enabled_rules)
    draft_note = ""
    draft = payload.get("draft")
    if isinstance(draft, dict) and draft:
        prepared = normalise_rule(state, draft)
        rules = [rule for rule in rules if rule.id != prepared["id"]] + [compile_rule(prepared)]
        draft_note = " This preview includes one unsaved draft rule."
    settings = state.config.settings
    threshold = float(settings.router_min_score)
    started = time.perf_counter()
    rows, matched = _scored_rows(state, task, rules)
    elapsed = round((time.perf_counter() - started) * 1000, 2)
    decision = None
    deferred = None
    if not rows:
        deferred = {"stage": "classifier" if settings.router_llm_fallback else "default",
                    "reason": "No keyword or custom rule matched this task.",
                    "candidates": []}
    elif rows[0]["total"] < threshold:
        deferred = {"stage": "classifier" if settings.router_llm_fallback else "default",
                    "reason": f"Top score {rows[0]['total']:g} is below the {threshold:g} threshold.",
                    "candidates": [row["role"] for row in rows[:MAX_CANDIDATES]]}
    else:
        tied = [row["role"] for row in rows if row["total"] == rows[0]["total"]]
        if len(tied) > 1 and settings.router_llm_fallback:
            deferred = {"stage": "classifier",
                        "reason": f"{len(tied)} roles tie at {rows[0]['total']:g}; the classifier decides.",
                        "candidates": tied[:MAX_CANDIDATES]}
        else:
            decision = {
                "role": rows[0]["role"],
                "method": "keyword",
                "score": rows[0]["total"],
                "hits": rows[0]["hits"][:10],
                "note": ("A tie is broken by rule priority, then role name." if len(tied) > 1 else ""),
            }
    candidates = [{"role": row["role"], "score": row["total"], "base": round(row["score"], 2),
                   "bonus": round(row["bonus"], 2), "priority": row["priority"],
                   "hits": row["hits"][:8], "rules": row["rules"][:6]} for row in rows[:MAX_CANDIDATES]]
    return _clean(state, {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "task": task,
        "provider_calls": 0,
        "elapsed_ms": elapsed,
        "threshold": threshold,
        "policy_enabled": policy.enabled,
        "rules_considered": len(rules),
        "rules_matched": matched,
        "decision": decision,
        "deferred": deferred,
        "candidates": candidates,
        "classifier_agent": settings.router_agent or state.config.default_agent,
        "note": ("Offline dry run: no model was called and nothing ran. The classifier stage is predicted, not invoked. "
                 "A real run would ask the classifier only when the keyword stage defers." + draft_note),
    })


# ── lab ────────────────────────────────────────────────────────────────── #


def lab(state) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    error = ""
    try:
        data = _read_json(lab_path(Path(state.workspace)))
        if isinstance(data, dict) and isinstance(data.get("runs"), list):
            runs = [row for row in data["runs"] if isinstance(row, dict)][-MAX_LAB_RUNS:]
    except (OSError, ValueError) as exc:
        error = f"Lab history unavailable ({type(exc).__name__})."
    runs = list(reversed(runs))
    return _clean(state, {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "runs": runs,
        "last": runs[0] if runs else None,
        "max_cases": MAX_CASES,
        "error": error,
        "store": f"{STORE_DIR}/{LAB_FILE} (workspace runtime state, never committed)",
        "note": "Lab runs exercise the keyword stage and saved rules only. No provider is constructed.",
    })


def run_lab(state, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError(f"Provide 1-{MAX_CASES} cases")
    policy = load_policy(Path(state.workspace))
    prepared = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Each case must be an object with task and optional expected role")
        task = _text(case.get("task"), MAX_CASE_TASK, "Case task")
        expected_raw = case.get("expected")
        expected = _resolve(state.config.roles, expected_raw) if expected_raw else None
        prepared.append({"task": task, "expected": expected})
    started = time.perf_counter()
    results = []
    correct = 0
    with_expected = 0
    unresolved = 0
    for case in prepared:
        run = simulate(state, {"task": case["task"]})
        predicted = run["decision"]["role"] if run["decision"] else None
        top = run["candidates"][0]["role"] if run["candidates"] else None
        passed = None
        if case["expected"]:
            with_expected += 1
            passed = predicted == case["expected"]
            correct += int(passed)
        if predicted is None:
            unresolved += 1
        results.append({"task": case["task"], "expected": case["expected"], "predicted": predicted,
                        "top": top, "pass": passed, "stage": run["deferred"]["stage"] if run["deferred"] else "keyword",
                        "score": None if not run["candidates"] else run["candidates"][0]["score"]})
    elapsed = round((time.perf_counter() - started) * 1000, 2)
    run_id = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"-{hashlib.sha256(str(time.time()).encode()).hexdigest()[:6]}"
    run = {
        "id": run_id,
        "at": time.time(),
        "total": len(results),
        "with_expected": with_expected,
        "correct": correct,
        "accuracy": None if not with_expected else round(correct / with_expected, 3),
        "unresolved": unresolved,
        "provider_calls": 0,
        "elapsed_ms": elapsed,
        "policy_revision": policy.revision,
        "cases": results,
    }
    try:
        existing = _read_json(lab_path(Path(state.workspace)))
        runs = existing.get("runs") if isinstance(existing, dict) and isinstance(existing.get("runs"), list) else []
        runs = [row for row in runs if isinstance(row, dict)][-(MAX_LAB_RUNS - 1):] + [run]
        _atomic_json(lab_path(Path(state.workspace)), {"schema": SCHEMA, "runs": runs})
    except (OSError, ValueError) as exc:
        raise ValueError(f"The lab cannot record this run ({type(exc).__name__})") from exc
    audit(state, _client(payload, state), "routerstudio.lab", every=0)
    return _clean(state, {"schema": SCHEMA, "observed_at": time.time(), "run": run,
                          "note": "Keyword stage and saved rules only; no model was called.",
                          "limits": "Expected roles measure the keyword stage, not the classifier."})


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
