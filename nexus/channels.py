"""Alerts, channels, voice preferences and the compact layout.

Three different things live here, and the file keeps them apart on purpose:

* **alerts** — real notices. A dashboard run that finishes, fails or stops to
  ask for approval appends one row through :func:`record`. The same rows the
  Jarvis daemon writes are read back through its own :class:`Inbox`, so the page
  shows one list, not a second invented one.
* **delivery** — how those notices are allowed to reach you (off / local /
  native), quiet mode and do-not-disturb. The authoritative engine is the
  Jarvis delivery policy; this module reads and writes it through
  :meth:`Inbox.configure` instead of keeping a copy.
* **preferences** — voice and mobile-lite settings. These are display choices,
  not actions: they are saved with a revision (so a stale tab cannot overwrite a
  newer choice) but without a typed token.

Nothing here sends a message to a third party, records audio or starts a bot.
Telegram polling is delegated to the existing, already-token-gated
``gui_desktop`` operation so there is exactly one code path for it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any

SCHEMA = 1

POLICY_DEFAULTS: dict[str, Any] = {
    "schema": SCHEMA,
    "voice": {"enabled": False, "voice": "", "rate": 1.0, "pitch": 1.0, "read": "digest"},
    "mobile": {"lite": False, "cards": True, "font_scale": 1.0},
}

SAVE_TOKEN = "SAVE ALERT POLICY"
PULL_TOKEN = "PULL NOTICES"
PUSH_TOKEN = "SEND OS ALERT"

TRANSPORTS = ("off", "local", "native")
READ_MODES = ("digest", "latest", "notice")
MAX_NOTICES = 500
MAX_TITLE = 200
MAX_DETAIL = 300
MAX_INBOX = 200

_NOTICE_KINDS = {"run", "approval", "reminder", "monitor", "brief", "assistant"}


# ── paths and preferences ──────────────────────────────────────────────── #


def store_dir(workspace: Path | str) -> Path:
    return Path(workspace) / ".nexus" / "channels"


def policy_path(workspace: Path | str) -> Path:
    return store_dir(workspace) / "policy.json"


def policy_bytes(policy: dict[str, Any]) -> bytes:
    return (json.dumps(policy, indent=2, sort_keys=True) + "\n").encode()


def revision_of(workspace: Path | str) -> str:
    path = policy_path(workspace)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def _validated(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict):
        raise ValueError("Preferences must be an object")
    unknown = set(policy) - set(POLICY_DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown preference key(s): {', '.join(sorted(unknown))[:120]}")
    clean: dict[str, Any] = {"schema": SCHEMA}
    voice = policy.get("voice", {})
    mobile = policy.get("mobile", {})
    for name, part in (("voice", voice), ("mobile", mobile)):
        if not isinstance(part, dict):
            raise ValueError(f"{name} must be an object")
        unknown = set(part) - set(POLICY_DEFAULTS[name])
        if unknown:
            raise ValueError(f"Unknown {name} key(s): {', '.join(sorted(unknown))[:120]}")
    v = {**POLICY_DEFAULTS["voice"], **voice}
    if type(v["enabled"]) is not bool:
        raise ValueError("voice.enabled must be true or false")
    if not isinstance(v["voice"], str) or len(v["voice"]) > 120 or "\n" in v["voice"]:
        raise ValueError("voice.voice must be a single voice name under 120 characters")
    for key in ("rate", "pitch"):
        value = v[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"voice.{key} must be a number")
        low, high = (0.5, 2.0) if key == "rate" else (0.0, 2.0)
        if not low <= value <= high:
            raise ValueError(f"voice.{key} must be between {low} and {high}")
    if v["read"] not in READ_MODES:
        raise ValueError("voice.read must be digest, latest or notice")
    m = {**POLICY_DEFAULTS["mobile"], **mobile}
    for key in ("lite", "cards"):
        if type(m[key]) is not bool:
            raise ValueError(f"mobile.{key} must be true or false")
    scale = m["font_scale"]
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale):
        raise ValueError("mobile.font_scale must be a number")
    if not 0.85 <= scale <= 1.3:
        raise ValueError("mobile.font_scale must be between 0.85 and 1.3")
    clean["voice"] = v
    clean["mobile"] = m
    return clean


def load_policy(workspace: Path | str) -> dict[str, Any]:
    path = policy_path(workspace)
    if not path.exists():
        return json.loads(json.dumps(POLICY_DEFAULTS))
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64000:
        raise ValueError("The preferences file is not a plain bounded file")
    try:
        return _validated(json.loads(path.read_text(encoding="utf-8")))
    except UnicodeDecodeError:
        raise ValueError("The preferences file is not readable UTF-8") from None
    except json.JSONDecodeError:
        raise ValueError("The preferences file is not valid JSON; fix or delete it") from None


def policy_view(workspace: Path | str) -> dict[str, Any]:
    try:
        policy, error = load_policy(workspace), ""
    except ValueError as exc:
        policy, error = json.loads(json.dumps(POLICY_DEFAULTS)), str(exc)
    return {"schema": SCHEMA, "policy": policy, "revision": revision_of(workspace),
            "file": str(policy_path(workspace).relative_to(Path(workspace).resolve()))
            if policy_path(workspace).is_relative_to(Path(workspace).resolve()) else str(policy_path(workspace)),
            "file_exists": policy_path(workspace).exists(), "error": error,
            "note": "Preferences only: they change this page, never what an agent may do."}


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def save_policy(state, payload: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(state.workspace)
    if str(payload.get("revision") or "") != revision_of(workspace):
        raise ValueError("These preferences changed since you read them; reload before saving")
    unknown = set(payload) - {"revision", "voice", "mobile"}
    if unknown:
        raise ValueError(f"Unknown preference key(s): {', '.join(sorted(unknown))[:120]}")
    patch = {}
    for part in ("voice", "mobile"):
        if part in payload and not isinstance(payload[part], dict):
            raise ValueError(f"{part} preferences must be an object")
        patch[part] = payload.get(part) or {}
    current = load_policy(workspace)
    merged = {"schema": SCHEMA,
              "voice": {**current["voice"], **patch["voice"]},
              "mobile": {**current["mobile"], **patch["mobile"]}}
    clean = _validated(merged)
    _atomic_bytes(policy_path(workspace), policy_bytes(clean))
    _audit(state, "channels.preferences", "user", {"voice": clean["voice"]["enabled"],
                                                   "lite": clean["mobile"]["lite"]})
    return {**policy_view(workspace), "saved": True}


# ── notices ────────────────────────────────────────────────────────────── #


def _studio(state):
    return state.workbench.studio


def _redact(state, text: str) -> str:
    try:
        return _studio(state).redact_text(text)[:MAX_TITLE]
    except Exception:  # noqa: BLE001 - redaction is best effort, never fatal
        return text[:MAX_TITLE]


def record(state, kind: str, *, state_name: str, title: str, detail: str = "",
           task_id: str = "") -> dict[str, Any] | None:
    """Append one notice. Never raises: a failed alert must not fail a run."""
    if kind not in _NOTICE_KINDS:
        return None
    try:
        studio = _studio(state)
        rows = studio.items("jarvis-notice")
        while len(rows) >= MAX_NOTICES:
            oldest = min(rows, key=lambda item: float(item.get("created") or 0))
            with studio.wb.lock, studio.wb.connect() as conn:
                conn.execute("DELETE FROM studio WHERE id=?", (oldest["id"],))
            rows = [row for row in rows if row["id"] != oldest["id"]]
        body = {
            "kind": kind,
            "state": str(state_name)[:40],
            "title": _redact(state, str(title))[:MAX_TITLE],
            "detail": _redact(state, str(detail))[:MAX_DETAIL],
            "task_id": str(task_id)[:40],
            "created": time.time(),
            "read": False,
            "source": "dashboard",
        }
        return studio.put("jarvis-notice", body)
    except Exception:  # noqa: BLE001 - see docstring
        return None


def _clip(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or "")[:40],
        "kind": str(row.get("kind") or row.get("state") or "notice")[:40],
        "state": str(row.get("state") or "")[:40],
        "title": str(row.get("title") or row.get("task") or "Notice")[:MAX_TITLE],
        "detail": str(row.get("detail") or "")[:MAX_DETAIL],
        "task_id": str(row.get("task_id") or row.get("job_id") or "")[:40],
        "created": float(row.get("created") or row.get("updated_at") or 0),
        "read": bool(row.get("read")),
        "source": str(row.get("source") or "jarvis")[:40],
    }


def inbox(state, limit: int = 60) -> dict[str, Any]:
    from .jarvis.inbox import Inbox

    try:
        rows = Inbox(_studio(state)).items()
        error = ""
    except (ValueError, OSError, KeyError) as exc:
        rows, error = [], f"{type(exc).__name__}: the local notice store is unavailable"
    rows = rows[:max(1, min(int(limit or 60), MAX_INBOX))]
    items = [_clip(row) for row in rows]
    try:
        waiting = Inbox(_studio(state)).pending()
        pending = {"jobs": len(waiting.get("jobs") or []), "actions": len(waiting.get("actions") or [])}
    except (ValueError, OSError, KeyError):
        pending = {"jobs": 0, "actions": 0}
    return {
        "schema": SCHEMA, "observed_at": time.time(), "items": items, "error": error,
        "pending": pending,
        "digest": {"total": len(items), "unread": sum(1 for item in items if not item["read"]),
                   "states": _states(items)},
        "note": "Local notices only. Nothing here is pushed anywhere until delivery allows it.",
    }


def _states(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = item["state"] or item["kind"]
        counts[key] = counts.get(key, 0) + 1
    return counts


def acknowledge(state, payload: dict[str, Any]) -> dict[str, Any]:
    from .jarvis.inbox import Inbox

    ids = payload.get("ids")
    if ids is None:
        ids = []
    if not isinstance(ids, list) or len(ids) > MAX_INBOX:
        raise ValueError("Send up to 200 notice ids")
    wanted = [str(value)[:40] for value in ids if isinstance(value, str)]
    studio = _studio(state)
    box = Inbox(studio)
    known = {row["id"] for row in box.items()}
    missing = [value for value in wanted if value not in known]
    # Inbox.acknowledge takes the queue lock itself; taking it here would deadlock.
    for ident in wanted:
        if ident in known:
            box.acknowledge(ident)
    return {"acknowledged": len([value for value in wanted if value in known]),
            "missing": missing[:10], "digest": inbox(state)["digest"]}


# ── delivery ───────────────────────────────────────────────────────────── #


def _delivery(state):
    from .jarvis.inbox import Inbox

    return Inbox(_studio(state))


def _delivery_policy(state) -> dict[str, Any]:
    try:
        raw = _delivery(state).delivery_policy()
    except (ValueError, OSError, KeyError) as exc:
        return {"transport": "off", "quiet": False, "dnd_until": 0, "error": type(exc).__name__}
    return {"transport": raw.get("transport", "local"),
            "quiet": bool(raw.get("quiet")), "dnd_until": float(raw.get("dnd_until") or 0),
            "last_push_at": raw.get("last_push_attempt"), "last_push_result": raw.get("last_push_result"),
            "push_digest": bool(raw.get("push_digest"))}


def delivery_view(state) -> dict[str, Any]:
    from .jarvis.notifications import native_available

    policy = _delivery_policy(state)
    now = time.time()
    remaining = max(0, int(round(policy["dnd_until"] - now))) if policy["dnd_until"] else 0
    return {
        "schema": SCHEMA, "observed_at": now, "policy": {**policy, "dnd_remaining_s": remaining},
        "revision": _delivery_revision(policy), "tokens": {"save": SAVE_TOKEN, "pull": PULL_TOKEN,
                                                           "push": PUSH_TOKEN},
        "native": native_available(),
        "quiet_now": bool(policy["quiet"] or remaining > 0),
        "note": "Quiet/DND suppress delivery. Notices stay in the inbox either way.",
    }


def _delivery_revision(policy: dict[str, Any]) -> str:
    stable = {"transport": policy.get("transport"), "quiet": bool(policy.get("quiet")),
              "dnd_until": int(float(policy.get("dnd_until") or 0))}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:12]


def save_delivery(state, payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("confirmation") or "").strip() != SAVE_TOKEN:
        raise ValueError(f"Type {SAVE_TOKEN} to change how alerts are delivered")
    current = _delivery_policy(state)
    if str(payload.get("revision") or "") != _delivery_revision(current):
        raise ValueError("Delivery changed since you read it; reload before saving")
    transport = payload.get("transport", current["transport"])
    if transport not in TRANSPORTS:
        raise ValueError("Transport must be off, local or native")
    quiet = payload.get("quiet", current["quiet"])
    if type(quiet) is not bool:
        raise ValueError("quiet must be true or false")
    minutes = payload.get("dnd_minutes", None)
    if minutes is not None and (type(minutes) is not int or not 0 <= minutes <= 1440):
        raise ValueError("DND must be a whole number of minutes between 0 and 1440")
    box = _delivery(state)
    box.configure(transport=transport, quiet=quiet)
    if minutes is not None:
        box.configure(minutes=minutes)
    _audit(state, "channels.delivery", "user", {"transport": transport, "quiet": quiet,
                                                "dnd_minutes": minutes}, risk="medium")
    return {**delivery_view(state), "saved": True}


def pull(state, payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("confirmation") or "").strip() != PULL_TOKEN:
        raise ValueError(f"Type {PULL_TOKEN} to pull notices into this page")
    result = _delivery(state).notify()
    _audit(state, "channels.pull", "user", {"notices": len(result.get("notices") or []),
                                            "suppressed": bool(result.get("suppressed"))})
    return {**result, "count": len(result.get("notices") or []), "delivery": delivery_view(state)}


def push(state, payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("confirmation") or "").strip() != PUSH_TOKEN:
        raise ValueError(f"Type {PUSH_TOKEN} to ask the desktop for one alert")
    policy = _delivery_policy(state)
    if policy["transport"] != "native":
        raise ValueError("Choose the native transport before asking the desktop for an alert")
    if state.demo:
        raise ValueError("Native delivery requires live mode")
    result = _delivery(state).push()
    _audit(state, "channels.push", "user", {"submitted": bool(result.get("submitted"))}, risk="medium")
    return {"result": result, "delivery": delivery_view(state)}


# ── Telegram ───────────────────────────────────────────────────────────── #


def telegram_view(state) -> dict[str, Any]:
    from .gui_desktop import dispatch

    snapshot = dispatch(state)
    runtime = snapshot.get("telegram_runtime") or {}
    policy = (snapshot.get("policies") or {}).get("telegram") or {}
    settings = policy.get("settings") or {}
    credentials = snapshot.get("credentials") or {}
    token_present = bool(credentials.get("telegram_token"))
    pin_present = bool(credentials.get("telegram_pin"))
    checks = [
        {"key": "policy", "ok": bool(settings.get("enabled")),
         "label": "Policy enabled", "hint": "telegram.yaml must set enabled: true"},
        {"key": "whitelist", "ok": bool(settings.get("user_ids")),
         "label": "Numeric user whitelist", "hint": "Up to ten exact private-chat user ids"},
        {"key": "token", "ok": token_present,
         "label": "TELEGRAM_BOT_TOKEN in the environment", "hint": "Set it where the server runs"},
        {"key": "pin", "ok": pin_present,
         "label": "TELEGRAM_APPROVAL_PIN in the environment", "hint": "8–12 digits, never shown"},
    ]
    return {
        "schema": SCHEMA, "observed_at": time.time(), "demo": bool(snapshot.get("demo")),
        "runtime": {key: runtime.get(key) for key in ("phase", "alive", "started_at", "last_poll_at", "error", "scope", "notice")},
        "policy": {"enabled": bool(settings.get("enabled")), "error": bool(policy.get("error")),
                   "whitelist": len(settings.get("user_ids") or []),
                   "poll_seconds": settings.get("poll_seconds"),
                   "revision": policy.get("revision")},
        "credentials": {"token_present": token_present, "pin_present": pin_present},
        "checks": checks, "ready": all(check["ok"] for check in checks),
        "tokens": {"start": "START TELEGRAM POLLING", "stop": "STOP TELEGRAM POLLING"},
        "note": "Presence of a credential is not validity, and a running poller is not an "
                "account check. Stop is a request: in-flight network work can finish.",
    }


def telegram_action(state, payload: dict[str, Any]) -> dict[str, Any]:
    from .gui_desktop import dispatch

    operation = payload.get("operation")
    if operation not in {"telegram-start", "telegram-stop"}:
        raise ValueError("Choose telegram-start or telegram-stop")
    expected = "START TELEGRAM POLLING" if operation == "telegram-start" else "STOP TELEGRAM POLLING"
    if str(payload.get("confirmation") or "").strip() != expected:
        raise ValueError(f"Type {expected} to change the poller")
    # Anything past the type check is delegated to the existing desktop action,
    # which owns the policy revision check and refuses in demo mode. Its errors
    # are intentionally generic: they must never echo private config.
    dispatch(state, payload)
    return {**telegram_view(state), "operation": operation}


# ── audit and overview ─────────────────────────────────────────────────── #


def _audit(state, action: str, actor: str, detail: dict, *, risk: str = "low") -> None:
    from . import securitycenter

    try:
        securitycenter.audit_action(state, action, actor, detail, risk=risk, target="channels")
    except (OSError, ValueError):
        pass


def audit_read(state, action: str, client: str, every: float = 120.0) -> None:
    from . import securitycenter

    try:
        securitycenter.audit_read(state, f"channels.{action}", client or "unknown", every=every)
    except (OSError, ValueError):
        pass


def overview(state) -> dict[str, Any]:
    box = inbox(state, 20)
    return {
        "schema": SCHEMA, "observed_at": time.time(), "demo": bool(getattr(state, "demo", False)),
        "inbox": box, "delivery": delivery_view(state), "telegram": telegram_view(state),
        "preferences": policy_view(state.workspace),
        "tokens": {"save_delivery": SAVE_TOKEN, "pull": PULL_TOKEN, "push": PUSH_TOKEN},
        "limits": {"notices": MAX_NOTICES, "inbox": MAX_INBOX,
                   "dnd_minutes": [0, 1440], "rate": [0.5, 2.0]},
        "notes": ["local_only", "quiet_keeps_inbox", "voice_is_browser", "telegram_is_opt_in"],
    }
