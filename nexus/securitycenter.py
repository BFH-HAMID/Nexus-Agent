"""Security Center: one tamper-evident trail, real sessions and step-up checks.

The dashboard already had a good enforcement layer - the approval gate, the
operator-owned ``permissions.yaml``, the Emergency Stop latch, redaction and
Studio's reviewed backup - but it had no single place that answers the questions
an operator asks during an incident:

* *what happened?*  The audit file was append-only but not tamper-evident.
* *who is signed in?*  Browser sessions were valid HMAC cookies with no list and
  no way to revoke one.
* *is this really me?*  The serve token was the only factor.
* *which secrets exist?*  Keys lived in five places and the page could not say
  which without printing them.
* *can I change the policy safely?*  ``permissions.yaml`` had no editor at all.

This module answers those without inventing a second authority:

* **Chain, not copy.** ``append_event`` writes the *same* file the permission
  gate and the dashboard API already share; it only adds ``seq``, ``prev`` and
  ``hash``. ``verify`` recomputes the chain and names the first broken line.
* **Revocation is a list, not a guess.** A session is one random nonce; the list
  stores its hash, never the cookie. Revoked means the cookie stops working.
* **Step-up, not a second login.** When TOTP is enrolled, the destructive
  security actions on this page require a fresh six-digit code. The serve token
  keeps working exactly as before, so nothing that already existed is gated.
* **Read the store, never echo it.** The vault reports a name, a source and a
  fingerprint. Values enter through a write-only field and never come back.
* **Edit the real file.** The policy editor patches ``permissions.yaml`` in
  place, validates it with the same loader the gate uses, and restores the
  previous bytes if the result would not load.

What this module deliberately does not do: it does not run a tool, grant a
permission, bypass the approval gate, or claim to be an OS sandbox. Policy is
policy; the gate still asks.

State lives under ``<Nexus home>/security/<workspace hash>/`` with mode 0600.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from filelock import FileLock

SCHEMA = 1
MAX_EVENTS = 500
MAX_SESSIONS = 50
AUDIT_LIMIT_BYTES = 5_000_000
MAX_SCAN_FINDINGS = 40
MAX_TRANSCRIPT_LINES = 4_000

TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_DRIFT = 1
RECOVERY_COUNT = 8
PENDING_TTL = 600.0
FAIL_WINDOW = 300.0
FAIL_LIMIT = 5

MODES = ("READ_ONLY", "ASK_EVERY_TIME", "AUTO_ALLOW")
RISKY_TOOLS = ("run_command", "git", "gh", "write_file", "edit_file", "delete_file",
               "browser_action", "office_file", "vscode_open")
COMPUTER_KEYS = ("browser", "editor", "office", "shell_backend", "image")

SESSION_TOKEN = "REVOKE SESSION"
OTHERS_TOKEN = "REVOKE OTHER SESSIONS"
ALL_TOKEN = "REVOKE ALL SESSIONS"
VAULT_SAVE = "SAVE SECRET"
VAULT_DELETE = "DELETE SECRET"
ENROL_TOKEN = "ENABLE 2FA"
DISABLE_TOKEN = "DISABLE 2FA"
POLICY_TOKEN = "SAVE CAPABILITY POLICY"
SNAPSHOT_TOKEN = "TAKE SECURITY SNAPSHOT"
RESTORE_TOKEN = "RESTORE SECURITY SNAPSHOT"
SNAPSHOT_SCHEMA = 1
MAX_SNAPSHOTS = 20
SNAPSHOT_NAME = re.compile(r"^[0-9]{8}T[0-9]{6}Z(-[0-9]{1,2})?$")

_audit_lock = threading.Lock()
_audit_at: dict[str, float] = {}
_denied_paths: dict[str, float] = {}
_denied = None
_cache_lock = threading.Lock()
_session_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}
_pending: dict[str, tuple[str, float]] = {}
_failures: dict[str, deque[float]] = {}

# Untrusted text is data. These patterns only *point at* text a human should
# read; they are not a verdict and never change a permission by themselves.
INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("override", re.compile(
        r"\b(?:ignore|disregard|forget)\b[^\n]{0,40}\b(?:previous|prior|above|earlier)\b"
        r"[^\n]{0,30}\b(?:instructions?|prompts?|rules?)\b", re.I)),
    ("roleplay", re.compile(
        r"\b(?:you are now|from now on you|act as (?:a|an|the)|pretend (?:to be|you are))\b", re.I)),
    ("system", re.compile(
        r"\b(?:system prompt|developer message|new system instruction|begin system)\b", re.I)),
    ("exfiltrate", re.compile(
        r"\b(?:curl|wget)\b[^\n]{0,80}\|\s*(?:sh|bash)\b|"
        r"\bbase64\s+(?:-d|--decode)\b[^\n]{0,40}\|\s*(?:sh|bash)\b", re.I)),
    ("secrets", re.compile(
        r"\b(?:send|upload|post|email|paste)\b[^\n]{0,40}"
        r"\b(?:api[- _]?key|token|password|credential|secret)s?\b", re.I)),
    ("silence", re.compile(
        r"\b(?:do not tell|don't tell|without (?:telling|informing))\b[^\n]{0,30}"
        r"\b(?:user|operator|human|owner)\b", re.I)),
)


# ── paths ──────────────────────────────────────────────────────────────── #

def _home() -> Path:
    from .config import nexus_home

    return nexus_home().resolve()


def _workspace_key(workspace: Path | str) -> str:
    return hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()


def audit_root(workspace: Path | str) -> Path:
    """Same directory the permission gate and the API audit already use."""
    home = _home()
    root = (home / "permissions" / _workspace_key(workspace)).resolve()
    if not root.is_relative_to(home):
        raise ValueError("Audit must stay in the trusted Nexus state directory")
    return root


def audit_file(workspace: Path | str) -> Path:
    root = audit_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    return root / "audit.jsonl"


def security_root(workspace: Path | str) -> Path:
    home = _home()
    root = (home / "security" / _workspace_key(workspace)[:16]).resolve()
    if not root.is_relative_to(home):
        raise ValueError("Security state must stay in the trusted Nexus state directory")
    return root


def sessions_path(workspace: Path | str) -> Path:
    return security_root(workspace) / "sessions.json"


def totp_path(workspace: Path | str) -> Path:
    return security_root(workspace) / "totp.json"


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
        raise ValueError("Security state must be a bounded regular file")
    return json.loads(path.read_text(encoding="utf-8"))


# ── the hash chain ─────────────────────────────────────────────────────── #

def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _hash_of(record: dict[str, Any]) -> str:
    body = {key: value for key, value in record.items() if key != "hash"}
    return hashlib.sha256((str(record.get("prev", "")) + "\n").encode() + _canonical(body)).hexdigest()


def _tail_record(path: Path, block: int = 131_072) -> dict[str, Any] | None:
    """The last complete line, or None when the file is absent/short/corrupt."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if not size:
        return None
    with path.open("rb") as handle:
        handle.seek(max(0, size - block))
        chunk = handle.read(block)
    for line in reversed(chunk.splitlines()):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return row if isinstance(row, dict) else None
    return None


def append_event(workspace: Path | str, event: dict[str, Any]) -> dict[str, Any]:
    """Redact, chain and append one line to the shared audit trail.

    Raises ``ValueError``/``OSError`` when the record cannot be written; callers
    treat that as a hard failure instead of silently losing the trail.
    """
    from .studio import redact

    if not isinstance(event, dict):
        raise ValueError("Audit event must be an object")
    path = audit_file(workspace)
    lock = path.with_suffix(".lock")
    if path.is_symlink() or lock.is_symlink():
        raise ValueError("Audit symlinks are forbidden")
    record = redact(dict(event))
    try:
        _canonical(record)
    except (TypeError, ValueError) as exc:
        raise ValueError("Audit detail must be finite JSON") from exc
    with FileLock(str(lock), timeout=5):
        if path.exists() and path.stat().st_size > AUDIT_LIMIT_BYTES:
            raise ValueError("Audit trail full; archive it manually before further use")
        previous = _tail_record(path)
        if previous and previous.get("hash"):
            record["prev"] = str(previous["hash"])
            record["seq"] = int(previous.get("seq", 0)) + 1
        else:
            record["prev"] = ""
            record["seq"] = 0
        record["hash"] = _hash_of(record)
        line = json.dumps(record, ensure_ascii=False, allow_nan=False)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return record


def verify_text(text: str, *, size: int | None = None) -> dict[str, Any]:
    """Recompute a chain from its exact text — the live file or a snapshot copy."""
    result: dict[str, Any] = {"schema": SCHEMA, "ok": True, "checked": 0, "legacy": 0,
                              "unchained_after": 0, "first_bad": None, "head": "", "seq": -1,
                              "size": len(text.encode("utf-8")) if size is None else size,
                              "partial": False, "at": time.time()}
    expected_prev = ""
    expected_seq = 0
    for number, raw in enumerate(text.splitlines(keepends=True), start=1):
        if not raw.strip():
            continue
        if not raw.endswith("\n"):
            result["partial"] = True
            if result["first_bad"] is None:
                result["first_bad"] = {"line": number, "reason": "truncated_line"}
            continue
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            result["legacy"] += 1
            if result["checked"]:
                result["unchained_after"] += 1
            if result["first_bad"] is None:
                result["first_bad"] = {"line": number, "reason": "unreadable"}
            continue
        if not isinstance(record, dict):
            result["unchained_after"] += 1
            if result["first_bad"] is None:
                result["first_bad"] = {"line": number, "reason": "not_an_object"}
            continue
        if "hash" not in record:
            # Lines written before the chain existed, or by a hand edit.
            if result["checked"]:
                result["unchained_after"] += 1
                if result["first_bad"] is None:
                    result["first_bad"] = {"line": number, "reason": "unchained_after_chain"}
            else:
                result["legacy"] += 1
            continue
        result["checked"] += 1
        if record.get("prev") != expected_prev and result["first_bad"] is None:
            result["first_bad"] = {"line": number, "reason": "prev_mismatch"}
        if record.get("seq") != expected_seq and result["first_bad"] is None:
            result["first_bad"] = {"line": number, "reason": "sequence_gap"}
        try:
            actual = _hash_of(record)
        except (TypeError, ValueError):
            actual = ""
        if actual != record.get("hash") and result["first_bad"] is None:
            result["first_bad"] = {"line": number, "reason": "hash_mismatch"}
        expected_prev = str(record.get("hash"))
        expected_seq += 1
    result["head"] = expected_prev
    result["seq"] = expected_seq - 1
    if result["first_bad"] or result["partial"]:
        result["ok"] = False
    return result


def verify_chain(workspace: Path | str) -> dict[str, Any]:
    """Recompute every chained line. Reports the first line that does not fit."""
    try:
        path = audit_file(workspace)
    except ValueError as exc:
        return {**verify_text(""), "ok": False, "first_bad": {"line": 0, "reason": "unsafe_path"},
                "note": str(exc)}
    if not path.exists():
        return verify_text("")
    if path.is_symlink() or path.stat().st_size > AUDIT_LIMIT_BYTES:
        return {**verify_text(""), "ok": False, "first_bad": {"line": 0, "reason": "unsafe_file"}}
    try:
        size = path.stat().st_size
        data = path.read_bytes()
    except OSError:
        return {**verify_text(""), "ok": False, "first_bad": {"line": 0, "reason": "unreadable_file"}}
    return verify_text(data.decode("utf-8", "replace"), size=size)


def read_events(workspace: Path | str, limit: int = 100, action: str = "") -> list[dict[str, Any]]:
    """Newest first, bounded. Never creates the file."""
    try:
        path = audit_file(workspace)
    except ValueError:
        return []
    if not path.exists() or path.is_symlink() or path.stat().st_size > AUDIT_LIMIT_BYTES:
        return []
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 512_000))
        lines = handle.read(512_000).decode("utf-8", "replace").splitlines()
    rows: list[dict[str, Any]] = []
    wanted = action.strip()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if wanted and not str(row.get("action", "")).startswith(wanted):
            continue
        rows.append({
            "seq": row.get("seq"), "at": row.get("at"), "actor": row.get("actor", ""),
            "action": str(row.get("action", ""))[:120], "target": str(row.get("target", ""))[:300],
            "result": str(row.get("result", ""))[:120], "risk": row.get("risk", "low"),
            "detail": row.get("detail") if isinstance(row.get("detail"), dict) else {},
            "hash": str(row.get("hash", ""))[:64], "prev": str(row.get("prev", ""))[:64],
        })
        if len(rows) >= max(1, min(int(limit), MAX_EVENTS)):
            break
    return rows


def audit_read(state, action: str, client: str, *, every: float = 300.0) -> bool:
    """Sample page reads into the same trail (never blocks the answer)."""
    now = time.time()
    key = f"{client}|{action}"
    with _audit_lock:
        if every > 0 and now - _audit_at.get(key, 0.0) < every:
            return False
        _audit_at[key] = now
    try:
        append_event(state.workspace, {"at": round(now, 3), "actor": "user", "action": action,
                                       "target": "dashboard", "result": "ok", "risk": "low",
                                       "detail": {"client": client}})
    except (OSError, ValueError):
        return False
    return True


def audit_action(state, action: str, client: str, data: dict[str, Any], *, risk: str = "high",
                 target: str = "dashboard") -> dict[str, Any]:
    """A mutation that cannot record itself is reported, not hidden."""
    detail = {"client": client}
    for key in ("name", "id", "scope", "enrolled", "changed", "fingerprint", "revoked"):
        value = data.get(key)
        if isinstance(value, (str, int, float, bool)):
            detail[key] = value
        elif isinstance(value, list) and len(value) <= 20 and all(isinstance(v, str) for v in value):
            detail[key] = value[:20]
    return append_event(state.workspace, {"at": round(time.time(), 3), "actor": "user",
                                          "action": action, "target": target, "result": "ok",
                                          "risk": risk, "detail": detail})


def note_denied(state, path: str, client: str, *, presented: bool = False) -> None:
    """A refused request is evidence — but a signed-out tab is not an attack.

    A request that *presented* a token or cookie and was refused is recorded
    up to the per-workspace/client rate limit: wrong credentials are worth
    counting. A request with no credential at all is recorded once per
    path per minute per client, so polling cannot flood the trail.
    """
    global _denied
    try:
        if _denied is None:
            from .webapi import Limiter

            _denied = Limiter(limit=30, window=60.0)
        # One workspace's paired-device reconnects must not exhaust another
        # workspace's audit budget (or signed-out-path deduplication).
        caller = hashlib.sha256((str(Path(state.workspace).resolve()) + '\0' + str(client)).encode()).hexdigest()
        if not _denied.allow(caller):
            return
        key = f"{caller}|{path}"
        now = time.time()
        with _audit_lock:
            if not presented and now - _denied_paths.get(key, 0.0) < 60.0:
                return
            _denied_paths[key] = now
        append_event(state.workspace, {"at": round(now, 3), "actor": "anonymous",
                                       "action": "security.auth.denied", "target": str(path)[:300],
                                       "result": "denied", "risk": "medium",
                                       "detail": {"client": client, "credential": bool(presented)}})
    except (OSError, ValueError):
        return


# ── sessions ───────────────────────────────────────────────────────────── #

def session_id(nonce: str) -> str:
    return hashlib.sha256(str(nonce).encode()).hexdigest()[:12]


def _session_data(workspace: Path | str) -> dict[str, Any]:
    path = sessions_path(workspace)
    try:
        stat = path.stat()
    except OSError:
        return {"version": 1, "sessions": []}
    with _cache_lock:
        cached = _session_cache.get(str(path))
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
    if path.is_symlink() or not path.is_file() or stat.st_size > 200_000:
        raise ValueError("The session store must be a bounded regular file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("The session store is unreadable") from exc
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        raise ValueError("The session store has an unknown shape")
    rows = [row for row in data["sessions"] if isinstance(row, dict) and isinstance(row.get("id"), str)]
    clean = {"version": 1, "sessions": rows[-MAX_SESSIONS:]}
    with _cache_lock:
        _session_cache[str(path)] = (stat.st_mtime_ns, stat.st_size, clean)
    return clean


def _write_sessions(workspace: Path | str, data: dict[str, Any]) -> None:
    path = sessions_path(workspace)
    now = time.time()
    rows = []
    for row in data.get("sessions", []):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        try:
            expires = float(row.get("expires") or 0)
        except (TypeError, ValueError):
            expires = 0.0
        if expires and expires < now - 86_400:
            continue
        if row.get("revoked") and float(row.get("revoked_at") or 0) < now - 30 * 86_400:
            continue
        rows.append(row)
    payload = {"version": 1, "sessions": rows[-MAX_SESSIONS:]}
    _atomic_bytes(path, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    with _cache_lock:
        _session_cache.pop(str(path), None)


def record_session(workspace: Path | str, nonce: str, host: str, remember: bool,
                   expires: float, ua: str = "") -> bool:
    """Remember one issued browser session. Returns True only for a new one."""
    ident = session_id(nonce)
    data = _session_data(workspace)
    now = time.time()
    created = True
    clean_ua = "".join(ch for ch in str(ua)[:160] if ch.isprintable())
    for row in data["sessions"]:
        if row.get("id") == ident:
            row.update(last=round(now, 3), expires=round(float(expires), 3), remember=bool(remember))
            if clean_ua:
                row["ua"] = clean_ua
            created = False
            break
    else:
        data["sessions"].append({"id": ident, "host": str(host)[:120], "remember": bool(remember),
                                 "created": round(now, 3), "last": round(now, 3),
                                 "expires": round(float(expires), 3), "revoked": False,
                                 "revoked_at": 0, "ua": clean_ua})
    _write_sessions(workspace, data)
    if created:
        try:
            append_event(workspace, {"at": round(now, 3), "actor": "user", "action": "security.session.issue",
                                     "target": ident, "result": "ok", "risk": "low",
                                     "detail": {"host": str(host)[:120], "remember": bool(remember)}})
        except (OSError, ValueError):
            pass
    return created


def is_revoked(workspace: Path | str, nonce: str) -> bool:
    ident = session_id(nonce)
    for row in _session_data(workspace)["sessions"]:
        if row.get("id") == ident:
            return bool(row.get("revoked"))
    return False


def cookie_nonce(cookie_header: str) -> str:
    """The 32-char nonce of the signed GUI cookie, or "" when there is none."""
    from http.cookies import CookieError, SimpleCookie

    try:
        value = SimpleCookie(cookie_header or "")["nexus_gui_session"].value
        parts = value.split(".")
        return parts[1] if len(parts) == 3 and len(parts[1]) == 32 else ""
    except (KeyError, ValueError, CookieError):
        return ""


def revoke_current(workspace: Path | str, cookie_header: str) -> bool:
    """Logging out revokes the nonce as well as clearing the cookie."""
    nonce = cookie_nonce(cookie_header)
    if not nonce:
        return False
    data = _session_data(workspace)
    changed = False
    for row in data["sessions"]:
        if row.get("id") == session_id(nonce) and not row.get("revoked"):
            row["revoked"] = True
            row["revoked_at"] = round(time.time(), 3)
            changed = True
    if changed:
        _write_sessions(workspace, data)
        try:
            append_event(workspace, {"at": round(time.time(), 3), "actor": "user",
                                     "action": "security.session.logout",
                                     "target": session_id(nonce), "result": "ok", "risk": "low",
                                     "detail": {}})
        except (OSError, ValueError):
            pass
    return changed


def sessions_view(workspace: Path | str, current_nonce: str = "") -> dict[str, Any]:
    error = ""
    try:
        rows = _session_data(workspace)["sessions"]
    except ValueError as exc:
        rows, error = [], str(exc)
    current = session_id(current_nonce) if current_nonce else ""
    now = time.time()
    items = []
    for row in sorted(rows, key=lambda r: float(r.get("created") or 0), reverse=True):
        items.append({
            "id": row.get("id"), "current": row.get("id") == current,
            "host": str(row.get("host", ""))[:120], "remember": bool(row.get("remember")),
            "created": row.get("created"), "last": row.get("last"),
            "expires": row.get("expires"), "revoked": bool(row.get("revoked")),
            "revoked_at": row.get("revoked_at") or 0,
            "active": (not row.get("revoked")) and float(row.get("expires") or 0) > now,
            "ua": str(row.get("ua", ""))[:160],
        })
    return {"schema": SCHEMA, "observed_at": time.time(), "sessions": items, "current": current,
            "error": error, "limits": {"sessions": MAX_SESSIONS},
            "tokens": {"one": SESSION_TOKEN, "others": OTHERS_TOKEN, "all": ALL_TOKEN}}


def revoke_sessions(state, payload: dict[str, Any], current_nonce: str = "") -> dict[str, Any]:
    if getattr(state, "demo", False):
        raise ValueError("Demo mode cannot change browser sessions")
    scope = str(payload.get("scope") or "one")
    target = str(payload.get("id") or "").strip()
    if scope not in {"one", "others", "all"}:
        raise ValueError("scope must be one, others or all")
    if scope == "one" and not re.fullmatch(r"[0-9a-f]{12}", target):
        raise ValueError("A listed session id is required")
    expected = {"one": f"{SESSION_TOKEN} {target}", "others": OTHERS_TOKEN, "all": ALL_TOKEN}[scope]
    if payload.get("confirmation") != expected:
        raise ValueError(f"Type {expected} exactly to confirm")
    require_code(state, payload)
    data = _session_data(state.workspace)
    current = session_id(current_nonce) if current_nonce else ""
    changed = []
    for row in data["sessions"]:
        hit = (scope == "all") or (scope == "one" and row.get("id") == target) or \
              (scope == "others" and row.get("id") != current)
        if hit and not row.get("revoked"):
            row["revoked"] = True
            row["revoked_at"] = round(time.time(), 3)
            changed.append(row.get("id"))
    if not changed:
        raise ValueError("Nothing to revoke: those sessions are already signed out")
    _write_sessions(state.workspace, data)
    audit_action(state, f"security.session.revoke.{scope}", "user",
                 {"scope": scope, "id": target, "revoked": changed}, target=target or scope)
    return sessions_view(state.workspace, current_nonce)


# ── two-factor ─────────────────────────────────────────────────────────── #

def _b32_secret(raw: bytes | None = None) -> str:
    return base64.b32encode(raw or os.urandom(20)).decode("ascii").rstrip("=")


def _b32_bytes(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded, casefold=True)


def totp_code(secret: str, counter: int) -> str:
    digest = hmac.new(_b32_bytes(secret), struct.pack(">Q", int(counter)), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 10 ** TOTP_DIGITS:0{TOTP_DIGITS}d}"


def _clean_code(value: Any) -> str:
    return re.sub(r"[\s-]", "", str(value or "")).upper()[:12]


def _totp_data(workspace: Path | str) -> dict[str, Any] | None:
    data = _read_json(totp_path(workspace), 64_000)
    if data is None:
        return None
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("secret"), str) or len(data["secret"]) < 16
            or not isinstance(data.get("recovery"), list)):
        raise ValueError("The two-factor store has an unknown shape")
    return data


def totp_status(workspace: Path | str) -> dict[str, Any]:
    error = ""
    try:
        data = _totp_data(workspace)
    except ValueError as exc:
        data, error = None, str(exc)
    enrolled = data is not None
    return {"schema": SCHEMA, "enrolled": enrolled, "created": (data or {}).get("created"),
            "recovery_left": len((data or {}).get("recovery") or []),
            "last_used": (data or {}).get("last_step"), "error": error,
            "digits": TOTP_DIGITS, "period": TOTP_STEP, "algorithm": "SHA1",
            "limits": {"recovery": RECOVERY_COUNT}, "tokens": {"enable": ENROL_TOKEN, "disable": DISABLE_TOKEN}}


def _check_failures(workspace: Path | str) -> None:
    key = str(security_root(workspace))
    now = time.time()
    bucket = _failures.get(key)
    if not bucket:
        return
    while bucket and now - bucket[0] > FAIL_WINDOW:
        bucket.popleft()
    if len(bucket) >= FAIL_LIMIT:
        raise ValueError("Too many two-factor attempts; wait five minutes")


def _record_failure(workspace: Path | str) -> None:
    key = str(security_root(workspace))
    bucket = _failures.setdefault(key, deque())
    bucket.append(time.time())
    while len(bucket) > 20:
        bucket.popleft()


def verify_code(workspace: Path | str, code: Any) -> bool:
    """Accept a TOTP code (once per step) or a single-use recovery code."""
    clean = _clean_code(code)
    if not clean:
        return False
    try:
        data = _totp_data(workspace)
    except ValueError:
        return False
    if not data:
        return False
    now = int(time.time()) // TOTP_STEP
    last = int(data.get("last_step") or 0)
    if clean.isdigit() and len(clean) == TOTP_DIGITS:
        for step in range(now - TOTP_DRIFT, now + TOTP_DRIFT + 1):
            if step <= last:
                continue
            if hmac.compare_digest(totp_code(data["secret"], step), clean):
                data["last_step"] = step
                data["last_used_at"] = round(time.time(), 3)
                _atomic_bytes(totp_path(workspace),
                              json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                return True
    digest = hashlib.sha256(clean.encode()).hexdigest()
    recovery = [str(item) for item in data.get("recovery") or []]
    for index, stored in enumerate(recovery):
        if hmac.compare_digest(stored, digest):
            recovery.pop(index)
            data["recovery"] = recovery
            _atomic_bytes(totp_path(workspace),
                          json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            _record_recovery_use(workspace)
            return True
    return False


def _record_recovery_use(workspace: Path | str) -> None:
    try:
        append_event(workspace, {"at": round(time.time(), 3), "actor": "user",
                                 "action": "security.totp.recovery", "target": "two-factor",
                                 "result": "ok", "risk": "high", "detail": {}})
    except (OSError, ValueError):
        pass


def require_code(state, payload: dict[str, Any]) -> None:
    """Step-up gate: no-op until the operator enrols, strict after that."""
    try:
        status = totp_status(state.workspace)
    except ValueError as exc:
        raise ValueError(f"Two-factor state is unreadable: {exc}") from exc
    if not status["enrolled"]:
        return
    _check_failures(state.workspace)
    if verify_code(state.workspace, payload.get("totp")):
        return
    _record_failure(state.workspace)
    raise ValueError("A current two-factor code is required for this action")


def begin_enrolment(state, payload: dict[str, Any]) -> dict[str, Any]:
    status = totp_status(state.workspace)
    if status["enrolled"]:
        raise ValueError("Two-factor is already enrolled; disable it first to re-enrol")
    secret = _b32_secret()
    _pending[str(security_root(state.workspace))] = (secret, time.time() + PENDING_TTL)
    account = "operator"
    uri = (f"otpauth://totp/Nexus%20Dashboard:{account}?secret={secret}"
           f"&issuer=Nexus&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP}")
    return {"schema": SCHEMA, "secret": secret, "uri": uri, "account": account,
            "issuer": "Nexus", "digits": TOTP_DIGITS, "period": TOTP_STEP,
            "algorithm": "SHA1", "expires": round(time.time() + PENDING_TTL, 3),
            "note": "The secret is shown once. It is stored only after a code confirms it."}


def confirm_enrolment(state, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("confirmation") != ENROL_TOKEN:
        raise ValueError(f"Type {ENROL_TOKEN} exactly to confirm")
    key = str(security_root(state.workspace))
    pending = _pending.get(key)
    if not pending or pending[1] < time.time():
        raise ValueError("No enrolment is pending; start again")
    secret, _ = pending
    counter = int(time.time()) // TOTP_STEP
    clean = _clean_code(payload.get("code"))
    if not clean.isdigit() or len(clean) != TOTP_DIGITS:
        raise ValueError("Enter the six-digit code from the authenticator app")
    if not any(hmac.compare_digest(totp_code(secret, counter + drift), clean)
               for drift in (-1, 0, 1)):
        _record_failure(state.workspace)
        raise ValueError("That code does not match; check the device clock")
    recovery = [base64.b32encode(os.urandom(5)).decode("ascii").rstrip("=")[:8] for _ in range(RECOVERY_COUNT)]
    data = {"version": 1, "secret": secret, "created": round(time.time(), 3),
            "last_step": counter, "recovery": [hashlib.sha256(code.encode()).hexdigest() for code in recovery]}
    _atomic_bytes(totp_path(state.workspace), json.dumps(data, ensure_ascii=False).encode("utf-8"))
    _pending.pop(key, None)
    audit_action(state, "security.totp.enable", "user", {"enrolled": True}, target="two-factor")
    return {**totp_status(state.workspace), "recovery_codes": recovery,
            "note": "Store the recovery codes offline. Each works once."}


def disable_two_factor(state, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("confirmation") != DISABLE_TOKEN:
        raise ValueError(f"Type {DISABLE_TOKEN} exactly to confirm")
    status = totp_status(state.workspace)
    if not status["enrolled"]:
        raise ValueError("Two-factor is not enrolled")
    _check_failures(state.workspace)
    if not verify_code(state.workspace, payload.get("code") or payload.get("totp")):
        _record_failure(state.workspace)
        raise ValueError("A current code or recovery code is required to disable two-factor")
    path = totp_path(state.workspace)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    audit_action(state, "security.totp.disable", "user", {"enrolled": False}, target="two-factor")
    return totp_status(state.workspace)


# ── the vault view ─────────────────────────────────────────────────────── #

def _fingerprint(value: str) -> str:
    return hashlib.sha256(("nexus-vault:" + str(value)).encode()).hexdigest()[:10]


def _display_path(path: Path) -> str:
    text = str(path)
    home = str(Path.home())
    return text.replace(home, "~", 1) if home and text.startswith(home) else text


def _weather_key(state) -> bool:
    try:
        from .hud.world.weatherapi import credential

        return bool(getattr(state, "weatherapi_key", "") or credential(state.workspace))
    except Exception:  # noqa: BLE001 - an unavailable World store is reported, not faked
        return False


def _items(state) -> list[dict[str, Any]]:
    from . import keys

    rows: list[dict[str, Any]] = []
    stored = keys.load_store()
    for family, spec in keys.PROVIDERS.items():
        env_value, env_name = keys.key_from_env(family)
        value = env_value or stored.get(family, "")
        source = env_name if env_value else ("key store" if stored.get(family) else "")
        rows.append({"name": family, "label": spec.label, "group": "model",
                     "set": bool(value), "source": source, "fingerprint": _fingerprint(value) if value else "",
                     "editable": True, "env": list(spec.env)})
    for family, names in keys.EXTRA_ENV.items():
        value, env_name = keys.key_from_env(family)
        rows.append({"name": family, "label": family.title(), "group": "model",
                     "set": bool(value), "source": env_name if value else "",
                     "fingerprint": _fingerprint(value) if value else "",
                     "editable": False, "env": list(names)})
    others = (
        ("github", "GitHub CLI / token", ("GITHUB_TOKEN", "GH_TOKEN"), "cli",
         "Set with `gh auth login` on the server, or export GITHUB_TOKEN."),
        ("telegram", "Telegram bot", ("TELEGRAM_BOT_TOKEN",), "env", "Telegram policy lives in telegram.yaml."),
        ("telegram_pin", "Telegram approval PIN", ("TELEGRAM_APPROVAL_PIN",), "env", ""),
        ("weather", "WeatherAPI key", ("WEATHERAPI_KEY",), "world",
         "Set it in World settings, where the native OS vault can hold it."),
        ("hud", "HUD token", ("NEXUS_HUD_TOKEN",), "env", ""),
    )
    for name, label, names, kind, hint in others:
        value = ""
        env_name = ""
        for candidate in names:
            found = os.environ.get(candidate, "").strip()
            if found:
                value, env_name = found, candidate
                break
        known = bool(value)
        if name == "weather" and not value and _weather_key(state):
            value, env_name = "configured", "session or native vault"
        rows.append({"name": name, "label": label, "group": "integration", "set": bool(value),
                     "source": (env_name if env_name.startswith(("session", "env:")) else "env:" + env_name) if value else "",
                     "fingerprint": _fingerprint(value) if known else "",
                     "editable": False, "env": list(names), "hint": hint, "kind": kind})
    return rows


def vault_view(state) -> dict[str, Any]:
    from . import keys

    path = keys.store_path()
    try:
        private = keys.store_is_private()
    except OSError:
        private = False
    exists = path.exists()
    try:
        mode = f"{path.stat().st_mode & 0o777:04o}" if exists else "not created"
    except OSError:
        mode = "unknown"
    items = _items(state)
    return {"schema": SCHEMA, "observed_at": time.time(), "items": items,
            "configured": sum(1 for item in items if item["set"]),
            "store": {"path": _display_path(path), "exists": exists, "private": private,
                      "source": "Environment variables are read first; the key store is second.",
                      "mode": mode},
            "never": ["Values are never returned by this API.", "Only a name, a source and a fingerprint are listed.",
                      "The browser sends a new value once; the server does not keep it in the response."],
            "tokens": {"save": VAULT_SAVE, "delete": VAULT_DELETE}}


def set_secret(state, payload: dict[str, Any]) -> dict[str, Any]:
    from . import keys

    if getattr(state, "demo", False):
        raise ValueError("Demo mode cannot change secrets")
    family = keys.canonical(str(payload.get("name") or ""))
    if family not in keys.PROVIDERS:
        raise ValueError("Only the listed model providers can be written here; other secrets stay in the environment")
    expected = f"{VAULT_SAVE} {family}"
    if payload.get("confirmation") != expected:
        raise ValueError(f"Type {expected} exactly to confirm")
    require_code(state, payload)
    value = keys.normalize_key(str(payload.get("value") or ""))
    if not 8 <= len(value) <= 512 or any(c.isspace() or not c.isprintable() for c in value):
        raise ValueError("A key is 8-512 printable characters with no whitespace")
    keys.save_key(family, value)
    warning = keys.looks_like_key(family, value)
    audit_action(state, "security.vault.set", "user",
                 {"name": family, "fingerprint": _fingerprint(value)}, target=family)
    return {**vault_view(state), "saved": family, "warning": warning}


def delete_secret(state, payload: dict[str, Any]) -> dict[str, Any]:
    from . import keys

    if getattr(state, "demo", False):
        raise ValueError("Demo mode cannot change secrets")
    family = keys.canonical(str(payload.get("name") or ""))
    if family not in keys.PROVIDERS:
        raise ValueError("Only the listed model providers can be removed here")
    expected = f"{VAULT_DELETE} {family}"
    if payload.get("confirmation") != expected:
        raise ValueError(f"Type {expected} exactly to confirm")
    require_code(state, payload)
    stored = keys.load_store().get(family, "")
    if not stored:
        raise ValueError("That key is not in the key store; an environment variable is still in use")
    keys.delete_key(family)
    audit_action(state, "security.vault.delete", "user",
                 {"name": family, "fingerprint": _fingerprint(stored)}, target=family)
    return {**vault_view(state), "deleted": family}


# ── capability policy ──────────────────────────────────────────────────── #

def policy_path(workspace: Path | str) -> Path:
    return Path(workspace) / "permissions.yaml"


def _policy_text(workspace: Path | str) -> str:
    path = policy_path(workspace)
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size > 64_000)):
        raise ValueError("permissions.yaml must be a bounded regular operator-owned file")
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except (OSError, UnicodeError) as exc:
        raise ValueError("permissions.yaml is unreadable") from exc


def _policy_revision(workspace: Path | str) -> str:
    return hashlib.sha256(_policy_text(workspace).encode("utf-8")).hexdigest()


def _file_mode(path: Path) -> str:
    try:
        return f"{path.stat().st_mode & 0o777:04o}"
    except OSError:
        return "missing"


def policy_view(state) -> dict[str, Any]:
    from .computer import settings as computer_settings
    from .permissions import FORCED_REVIEW, Permissions

    workspace = Path(state.workspace)
    path = policy_path(workspace)
    error = ""
    try:
        text = _policy_text(workspace)
        loaded = Permissions(workspace).load()
    except ValueError as exc:
        text, error = "", str(exc)
        loaded = {"default": "ASK_EVERY_TIME", "tools": {},
                  "computer": computer_settings({})}
    tools = {str(key): str(value) for key, value in loaded["tools"].items()}
    effective = {tool: tools.get(tool, loaded["default"]) for tool in RISKY_TOOLS}
    try:
        from .computer_control import EmergencyStop

        latch = bool(EmergencyStop().stopped())
    except Exception:  # noqa: BLE001 - a missing latch is reported as unknown, never as safe
        latch = None
    return {
        "schema": SCHEMA, "observed_at": time.time(), "error": error,
        "file": {"path": "permissions.yaml", "exists": path.exists(), "mode": _file_mode(path),
                 "revision": hashlib.sha256(text.encode("utf-8")).hexdigest() if text or path.exists() else "",
                 "size": len(text.encode("utf-8"))},
        "default": loaded["default"], "tools": tools, "effective": effective,
        "risky_mutating": list(RISKY_TOOLS), "modes": list(MODES),
        "forced": sorted(FORCED_REVIEW), "computer": loaded["computer"], "latch": latch,
        "demo": bool(getattr(state, "demo", False)), "token": POLICY_TOKEN,
        "notes": ["policy_not_sandbox", "forced_review", "operator_owned", "restart_not_required"],
    }


def _render_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not text or re.search(r"^[\s:#&*!|>'\"%@`-]|[:\s]#|^$", text):
        return json.dumps(text, ensure_ascii=False)
    return text


def _replace_value(line: str, value: Any) -> str:
    """Keep indentation, key and a trailing ``# comment`` from the original line."""
    marker = ""
    body = line.rstrip("\n")
    if " #" in body:
        body, marker = body.split(" #", 1)
        marker = " #" + marker
    key, _, _ = body.partition(":")
    return f"{key}: {_render_scalar(value)}{marker}"


def _block_bounds(lines: list[str], section: str) -> tuple[int, int]:
    start = -1
    for index, line in enumerate(lines):
        if re.match(rf"^{re.escape(section)}\s*:\s*(#.*)?$", line):
            start = index
            break
    if start < 0:
        return -1, -1
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t", "#")):
            end = index
            break
    return start, end


def _patch_policy(text: str, updates: dict[str, Any]) -> str:
    """Line-surgical edit: unrelated lines (and their comments) stay byte-identical."""
    for section in ("tools", "computer"):
        if re.search(rf"^{section}[ \t]*:[ \t]*\S", text, re.M) and updates.get(section):
            raise ValueError("This permissions file uses a layout this page cannot edit safely; edit it by hand")
    lines = text.splitlines()
    if updates.get("default"):
        for index, line in enumerate(lines):
            if re.match(r"^default\s*:", line):
                lines[index] = _replace_value(line, updates["default"])
                break
        else:
            lines.extend(["", f"default: {updates['default']}"])
    for tool, mode in (updates.get("tools") or {}).items():
        start, end = _block_bounds(lines, "tools")
        if start < 0:
            if mode:
                lines.extend(["", "tools:", f"  {tool}: {mode}"])
            continue
        target = None
        for index in range(start + 1, end):
            if re.match(rf"^\s{{2,4}}{re.escape(tool)}\s*:", lines[index]) and not lines[index].strip().startswith("#"):
                target = index
                break
        if mode and target is not None:
            lines[target] = _replace_value(lines[target], mode)
        elif mode:
            insert = start + 1
            for index in range(start + 1, end):
                if lines[index].strip() and lines[index][:1] in {" ", "\t"}:
                    insert = index + 1
            lines.insert(insert, f"  {tool}: {mode}")
        elif target is not None:
            lines.pop(target)
    for key, value in (updates.get("computer") or {}).items():
        start, end = _block_bounds(lines, "computer")
        if start < 0:
            lines.extend(["", "computer:", f"  {key}: {_render_scalar(value)}"])
            continue
        target = None
        for index in range(start + 1, end):
            if re.match(rf"^\s{{2,4}}{re.escape(key)}\s*:", lines[index]) and not lines[index].strip().startswith("#"):
                target = index
                break
        if target is not None:
            lines[target] = _replace_value(lines[target], value)
        else:
            insert = start + 1
            for index in range(start + 1, end):
                if lines[index].strip() and lines[index][:1] in {" ", "\t"}:
                    insert = index + 1
            lines.insert(insert, f"  {key}: {_render_scalar(value)}")
    return "\n".join(lines).rstrip("\n") + "\n"


def _validate_policy_bytes(workspace: Path | str, text: str) -> dict[str, Any]:
    import tempfile

    from .permissions import Permissions

    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "permissions.yaml").write_text(text, encoding="utf-8")
        return Permissions(directory).load()


def save_policy(state, payload: dict[str, Any]) -> dict[str, Any]:
    from .computer import settings as computer_settings
    from .permissions import Permissions

    if getattr(state, "demo", False):
        raise ValueError("Demo mode cannot change the capability policy")
    if payload.get("confirmation") != POLICY_TOKEN:
        raise ValueError(f"Type {POLICY_TOKEN} exactly to confirm")
    require_code(state, payload)
    workspace = Path(state.workspace)
    text = _policy_text(workspace)
    if payload.get("revision") != hashlib.sha256(text.encode("utf-8")).hexdigest():
        raise ValueError("permissions.yaml changed on disk; reload before saving")
    default = str(payload.get("default") or "").strip()
    if default not in MODES:
        raise ValueError("Choose READ_ONLY, ASK_EVERY_TIME or AUTO_ALLOW")
    raw_tools = payload.get("tools") or {}
    if not isinstance(raw_tools, dict) or len(raw_tools) > len(RISKY_TOOLS):
        raise ValueError("Unknown tool overrides")
    tools: dict[str, str] = {}
    for name, mode in raw_tools.items():
        if name not in RISKY_TOOLS or mode not in MODES:
            raise ValueError(f"Unknown tool/mode: {name}")
        tools[name] = mode
    raw_computer = payload.get("computer") or {}
    if not isinstance(raw_computer, dict) or set(raw_computer) - set(COMPUTER_KEYS):
        raise ValueError("Unknown computer fields")
    current = Permissions(workspace).load()
    merged = {**current["computer"], **raw_computer}
    try:
        merged = computer_settings(merged)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    computer = {key: merged[key] for key in COMPUTER_KEYS if key in raw_computer}
    patched = _patch_policy(text, {"default": default, "tools": tools, "computer": computer})
    try:
        loaded = _validate_policy_bytes(workspace, patched)
    except Exception as exc:  # yaml errors and schema errors both refuse the write
        raise ValueError(f"The edited policy would not load: {exc}") from exc
    if loaded["default"] != default or any(loaded["tools"].get(k) != v for k, v in tools.items()):
        raise ValueError("This permissions file uses a layout this page cannot edit safely; edit it by hand")
    path = policy_path(workspace)
    previous = path.read_bytes() if path.exists() else None
    _atomic_bytes(path, patched.encode("utf-8"), mode=0o600)
    changed = {"default": default != current["default"],
               "tools": sorted(k for k, v in tools.items() if current["tools"].get(k) != v),
               "computer": sorted(k for k in COMPUTER_KEYS if k in raw_computer and current["computer"].get(k) != merged.get(k))}
    try:
        audit_action(state, "security.policy.save", "user",
                     {"changed": [key for key, value in changed.items() if value], "default": default},
                     target="permissions.yaml")
    except (OSError, ValueError):
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_bytes(path, previous, mode=0o600)
        raise
    return {**policy_view(state), "changed": changed}


# ── injection watch ────────────────────────────────────────────────────── #

def _clean_snippet(line: str, start: int, end: int) -> str:
    from .studio import redact

    left = max(0, start - 40)
    right = min(len(line), end + 60)
    text = " ".join(line[left:right].split())
    return redact(text)[:220]


def scan_transcript(text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for number, line in enumerate((text or "").splitlines(), start=1):
        if number > MAX_TRANSCRIPT_LINES:
            break
        if not line.strip():
            continue
        for label, pattern in INJECTION_RULES:
            match = pattern.search(line)
            if not match:
                continue
            findings.append({"rule": label, "line": number,
                             "snippet": _clean_snippet(line, match.start(), match.end())})
            break
        if len(findings) >= MAX_SCAN_FINDINGS:
            break
    return findings


def scan_injection(state, payload: dict[str, Any]) -> dict[str, Any]:
    from .sessions import SessionStore

    store = SessionStore()
    workspace = str(Path(state.workspace).resolve())
    ident = str(payload.get("session_id") or "").strip()
    if ident and not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", ident):
        raise ValueError("Unknown session id shape")
    row = store.transcript(ident, workspace=workspace) if ident else store.latest(workspace)
    recent = store.recent_for(workspace, limit=20)
    if not row:
        return {"schema": SCHEMA, "observed_at": time.time(), "session": None,
                "findings": [], "recent": recent,
                "note": "No stored transcript for this workspace yet; nothing was scanned."}
    body = str(row.get("transcript") or "")
    findings = scan_transcript(body)
    audit_action(state, "security.injection.scan", "user",
                 {"name": str(row.get("id", ""))[:40], "fingerprint": hashlib.sha256(body.encode()).hexdigest()[:10]},
                 risk="low", target="transcript")
    return {"schema": SCHEMA, "observed_at": time.time(),
            "session": {"id": row.get("id"), "created": row.get("created"),
                        "role": row.get("role", ""), "task": str(row.get("task", ""))[:200],
                        "lines": len(body.splitlines()), "chars": len(body)},
            "findings": findings, "recent": recent,
            "note": ("Matches point at text worth reading. The transcript mixes what you typed with "
                     "what tools fetched, so a hit is a prompt to review, not a verdict.")}


# ── snapshots: a backup this page can read back ───────────────────────── #


def snapshots_dir(workspace: Path | str) -> Path:
    root = security_root(workspace) / "snapshots"
    if root.is_symlink():
        raise ValueError("snapshots/ is a symlink; refusing to use it")
    return root


def _snapshot_path(workspace: Path | str, name: str) -> Path:
    """Only names this module minted are accepted; nothing else becomes a path."""
    if not SNAPSHOT_NAME.fullmatch(str(name or "")):
        raise ValueError("Unknown snapshot name")
    return snapshots_dir(workspace) / f"{name}.json"


def _snapshot_rows(workspace: Path | str) -> list[dict[str, Any]]:
    root = snapshots_dir(workspace)
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for entry in sorted(root.glob("*.json"), reverse=True):
        try:
            blob = _read_json(entry, limit=AUDIT_LIMIT_BYTES + 1_000_000)
        except (OSError, ValueError):
            continue
        if not isinstance(blob, dict) or blob.get("kind") != "nexus-security-snapshot":
            continue
        audit = blob.get("audit") if isinstance(blob.get("audit"), dict) else {}
        policy = blob.get("policy") if isinstance(blob.get("policy"), dict) else {}
        rows.append({"name": entry.stem, "at": blob.get("created"), "bytes": entry.stat().st_size,
                     "checked": audit.get("checked", 0), "head": str(audit.get("head") or ""),
                     "policy_revision": policy.get("revision", "")})
    return rows


def snapshots_view(workspace: Path | str) -> dict[str, Any]:
    rows = _snapshot_rows(workspace)
    return {"schema": SCHEMA, "observed_at": time.time(), "snapshots": rows,
            "dir": _display_path(snapshots_dir(workspace)), "limit": MAX_SNAPSHOTS,
            "tokens": {"take": SNAPSHOT_TOKEN, "restore": RESTORE_TOKEN}}


def take_snapshot(state, payload: dict[str, Any]) -> dict[str, Any]:
    """Write the trail and the policy to one file, then prove the copy verifies."""
    if getattr(state, "demo", False):
        raise ValueError("Demo mode cannot take a snapshot")
    if payload.get("confirmation") != SNAPSHOT_TOKEN:
        raise ValueError(f"Type {SNAPSHOT_TOKEN} exactly to confirm")
    require_code(state, payload)
    workspace = Path(state.workspace)
    path = audit_file(workspace)
    if path.exists() and (path.is_symlink() or path.stat().st_size > AUDIT_LIMIT_BYTES):
        raise ValueError("The audit trail cannot be snapshotted: unsafe file or larger than 5 MB")
    raw = path.read_bytes() if path.exists() else b""
    text = raw.decode("utf-8", "replace")
    chain = verify_text(text, size=len(raw))
    if not chain["ok"]:
        raise ValueError("The live chain does not verify; repair it before taking a snapshot")
    policy_text = _policy_text(workspace)
    root = snapshots_dir(workspace)
    root.mkdir(parents=True, exist_ok=True)
    base = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name, suffix = base, 0
    while (root / f"{name}.json").exists():
        suffix += 1
        if suffix > 99:
            raise ValueError("Too many snapshots in this second; wait a moment")
        name = f"{base}-{suffix}"
    blob = {"kind": "nexus-security-snapshot", "schema": SNAPSHOT_SCHEMA, "created": time.time(),
            "workspace": workspace.name,
            "audit": {"text": text, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                      "checked": chain["checked"], "head": chain["head"], "seq": chain["seq"]},
            "policy": {"text": policy_text, "sha256": hashlib.sha256(policy_text.encode()).hexdigest(),
                       "revision": _policy_revision(workspace)},
            "excludes": ["credential values", "session cookies", "two-factor secret", "OS keys"],
            "restore": "The audit trail can be restored from here; the policy is reported, never overwritten."}
    _atomic_bytes(root / f"{name}.json", json.dumps(blob, ensure_ascii=False, allow_nan=False).encode())
    for stale in sorted(root.glob("*.json"), reverse=True)[MAX_SNAPSHOTS:]:
        try:
            stale.unlink()
        except OSError:
            pass
    audit_action(state, "security.snapshot.take", "user",
                 {"name": name, "scope": f"{chain['checked']} line(s)", "fingerprint": str(chain["head"])[:10]},
                 risk="medium", target="audit")
    return {**snapshots_view(workspace), "taken": name, "chain": chain}


def snapshot_detail(workspace: Path | str, name: str) -> dict[str, Any]:
    path = _snapshot_path(workspace, name)
    if not path.exists() or path.is_symlink():
        raise ValueError("No snapshot with that name")
    blob = _read_json(path, limit=AUDIT_LIMIT_BYTES + 1_000_000)
    if not isinstance(blob, dict) or blob.get("kind") != "nexus-security-snapshot":
        raise ValueError("That file is not a security snapshot")
    audit = blob.get("audit") if isinstance(blob.get("audit"), dict) else {}
    text = str(audit.get("text") or "")
    raw = text.encode()
    chain = verify_text(text, size=len(raw))
    digest_ok = hashlib.sha256(raw).hexdigest() == str(audit.get("sha256") or "")
    policy = blob.get("policy") if isinstance(blob.get("policy"), dict) else {}
    policy_text = str(policy.get("text") or "")
    policy_ok = hashlib.sha256(policy_text.encode()).hexdigest() == str(policy.get("sha256") or "")
    return {"name": name, "blob": blob, "cache": {"chain": chain, "digest_ok": digest_ok,
                                                  "policy_ok": policy_ok,
                                                  "ok": chain["ok"] and digest_ok and policy_ok}}


def restore_snapshot(state, payload: dict[str, Any]) -> dict[str, Any]:
    """Replace the live trail with a verified snapshot, keeping what it replaced."""
    if getattr(state, "demo", False):
        raise ValueError("Demo mode cannot restore a snapshot")
    name = str(payload.get("name") or "")
    if payload.get("confirmation") != f"{RESTORE_TOKEN} {name}":
        raise ValueError(f"Type {RESTORE_TOKEN} {name} exactly to confirm")
    require_code(state, payload)
    workspace = Path(state.workspace)
    detail = snapshot_detail(workspace, name)
    check = detail["cache"]
    if not check["ok"]:
        raise ValueError("This snapshot does not verify; refusing to restore it")
    path = audit_file(workspace)
    if path.exists():
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        keep = path.with_name(f"audit.jsonl.replaced-{stamp}")
        _atomic_bytes(keep, path.read_bytes())
    restored = str(detail["blob"]["audit"]["text"]).encode()
    _atomic_bytes(path, restored)
    before = verify_chain(workspace)
    audit_action(state, "security.snapshot.restore", "user",
                 {"name": name, "scope": f"{before['checked']} line(s)", "fingerprint": str(before["head"])[:10]},
                 risk="high", target="audit")
    # Re-read so the answer includes the restore event it just wrote.
    verify = verify_chain(workspace)
    return {**snapshots_view(workspace), "restored": name, "chain": verify,
            "policy": {"snapshot": detail["blob"].get("policy", {}).get("revision", ""),
                       "live": _policy_revision(workspace)}}


# ── overview and anomalies ─────────────────────────────────────────────── #

def _recent_by_action(events: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    return [row for row in events if str(row.get("action", "")).startswith(prefix)]


def anomalies(state, events: list[dict[str, Any]], chain: dict[str, Any],
              sessions: dict[str, Any], two_factor: dict[str, Any], policy: dict[str, Any]) -> list[dict[str, Any]]:
    now = time.time()
    rows: list[dict[str, Any]] = []

    if chain.get("first_bad") or not chain.get("ok"):
        bad = chain.get("first_bad") or {}
        rows.append({"id": "chain", "level": "critical", "params": {"line": bad.get("line", 0),
                                                                  "reason": str(bad.get("reason", "unknown"))},
                     "evidence": [str(bad.get("reason", ""))]})
    denied = [row for row in events if row.get("action") == "security.auth.denied"
              and isinstance(row.get("at"), (int, float)) and now - row["at"] <= 600]
    if len(denied) >= 5:
        rows.append({"id": "authDenied", "level": "warn", "params": {"count": len(denied), "minutes": 10},
                     "evidence": sorted({str(row.get("detail", {}).get("client", "")) for row in denied})[:5]})
    denied_tools = [row for row in events if row.get("action") == "permission.denied"
                    or (row.get("action", "").startswith("permission.") and row.get("result") == "denied")]
    if len(denied_tools) >= 3:
        rows.append({"id": "permissionDenied", "level": "warn", "params": {"count": len(denied_tools)},
                     "evidence": sorted({str(row.get("target", "")) for row in denied_tools})[:5]})
    active = [row for row in sessions.get("sessions", []) if row.get("active")]
    if active and not two_factor.get("enrolled"):
        rows.append({"id": "noTwoFactor", "level": "info", "params": {"sessions": len(active)},
                     "evidence": ["two-factor is not enrolled"]})
    revoked = [row for row in sessions.get("sessions", []) if row.get("revoked")
               and now - float(row.get("revoked_at") or 0) <= 86_400]
    if revoked:
        rows.append({"id": "sessionRevoked", "level": "info", "params": {"count": len(revoked)},
                     "evidence": [str(row.get("id")) for row in revoked][:5]})
    if policy.get("default") == "AUTO_ALLOW":
        rows.append({"id": "autoAllow", "level": "info", "params": {}, "evidence": ["permissions.yaml default"]})
    if policy.get("latch"):
        rows.append({"id": "latch", "level": "warn", "params": {}, "evidence": ["Emergency Stop"]})
    return rows


def overview(state, current_nonce: str = "") -> dict[str, Any]:
    from . import keys

    workspace = Path(state.workspace)
    events = read_events(workspace, limit=MAX_EVENTS)
    chain = verify_chain(workspace)
    sessions = sessions_view(workspace, current_nonce)
    two_factor = totp_status(workspace)
    policy = policy_view(state)
    vault = vault_view(state)
    denied = [row for row in events if row.get("action") == "security.auth.denied"]
    try:
        store_private = keys.store_is_private()
    except OSError:
        store_private = False
    backup_rows = [row for row in events if "backup" in str(row.get("action", ""))][:5]
    return {
        "schema": SCHEMA, "observed_at": time.time(), "demo": bool(getattr(state, "demo", False)),
        "audit": {"events": len(events), "counted": chain.get("checked", 0), "legacy": chain.get("legacy", 0),
                  "chain": "valid" if chain.get("ok") else "broken", "head": chain.get("head", ""),
                  "seq": chain.get("seq", -1), "size": chain.get("size", 0),
                  "verified_at": chain.get("at"), "first_bad": chain.get("first_bad"),
                  "denied": len(denied), "file": "permissions/<workspace>/audit.jsonl"},
        "sessions": {"active": sum(1 for row in sessions["sessions"] if row["active"]),
                     "remembered": sum(1 for row in sessions["sessions"] if row["remember"] and row["active"]),
                     "revoked_24h": sum(1 for row in sessions["sessions"] if row["revoked"]),
                     "error": sessions.get("error", "")},
        "two_factor": {"enrolled": two_factor["enrolled"], "recovery_left": two_factor["recovery_left"],
                       "error": two_factor.get("error", "")},
        "vault": {"configured": vault["configured"], "total": len(vault["items"]),
                  "store_private": store_private},
        "policy": {"default": policy["default"], "explicit": len(policy["tools"]),
                   "computer": policy["computer"], "latch": policy["latch"], "error": policy.get("error", "")},
        "backup": {"action": "Studio → Data → Download portable backup",
                   "restore_token": "IMPORT REVIEWED BACKUP",
                   "excludes": ["credentials", "live processes", "checkpoints", "OS keys"],
                   "recent": backup_rows},
        "anomalies": anomalies(state, events, chain, sessions, two_factor, policy),
        "notes": ["chain_limits", "policy_not_sandbox", "vault_no_echo", "two_factor_step_up"],
        "tokens": {"session": SESSION_TOKEN, "others": OTHERS_TOKEN, "all": ALL_TOKEN,
                   "vaultSave": VAULT_SAVE, "vaultDelete": VAULT_DELETE, "policy": POLICY_TOKEN,
                   "enable": ENROL_TOKEN, "disable": DISABLE_TOKEN},
        "limits": {"events": MAX_EVENTS, "sessions": MAX_SESSIONS, "findings": MAX_SCAN_FINDINGS},
    }
