"""Optional second-factor confirmation for dangerous actions (off by default)."""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from filelock import FileLock

SECOND_FACTOR_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "method": "pin",
    "pin_env": "NEXUS_APPROVAL_PIN",
    "tools": [],
    "risks": ["dangerous"],
    "timeout_seconds": 60,
}

_METHODS = frozenset({"pin", "telegram", "either"})
_RISKS = frozenset({"read", "write", "dangerous"})
MANDATORY_SECOND_FACTOR_TOOLS = frozenset({"integration_gmail_send"})
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_FAIL_WINDOW_S = 300.0
_FAIL_LIMIT = 5


def parse_second_factor_policy(value: Any) -> dict[str, Any]:
    """Validate optional ``second_factor:`` policy block from ``permissions.yaml``."""
    if value is None:
        return dict(SECOND_FACTOR_DEFAULTS)
    if not isinstance(value, dict):
        raise ValueError("Invalid second_factor policy: expected a mapping")
    unknown = set(value) - set(SECOND_FACTOR_DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown second_factor field(s): {', '.join(sorted(unknown))}")
    cfg = {**SECOND_FACTOR_DEFAULTS, **value}
    if type(cfg["enabled"]) is not bool:
        raise ValueError("second_factor.enabled must be true or false")
    if not isinstance(cfg["method"], str) or cfg["method"] not in _METHODS:
        raise ValueError("second_factor.method must be pin, telegram, or either")
    if not isinstance(cfg["pin_env"], str) or not _ENV_NAME_RE.fullmatch(cfg["pin_env"]):
        raise ValueError("second_factor.pin_env must be an uppercase environment variable name")
    tools = cfg["tools"]
    if not isinstance(tools, list) or len(tools) > 100 or any(not isinstance(t, str) or not _TOOL_NAME_RE.fullmatch(t) for t in tools):
        raise ValueError("second_factor.tools must be a bounded list of tool names")
    risks = cfg["risks"]
    if not isinstance(risks, list) or len(risks) > 3 or any(not isinstance(r, str) or r not in _RISKS for r in risks):
        raise ValueError("second_factor.risks must contain read, write, or dangerous")
    timeout = cfg["timeout_seconds"]
    if type(timeout) is not int or not 5 <= timeout <= 300:
        raise ValueError("second_factor.timeout_seconds must be an integer between 5 and 300")
    cfg["tools"] = list(tools)
    cfg["risks"] = list(risks)
    return cfg


def requires_second_factor(
    cfg: dict[str, Any],
    spec: Any,
    args: dict[str, Any],
    *,
    readonly: bool,
    tainted: bool = False,
) -> bool:
    """Return True when the current call must pass a second-factor confirmation."""
    if readonly:
        return False
    if getattr(spec, "name", "") in MANDATORY_SECOND_FACTOR_TOOLS:
        return True
    if not cfg.get("enabled"):
        return False
    risk_value = getattr(getattr(spec, "risk", None), "value", str(getattr(spec, "risk", "")))
    if risk_value in cfg.get("risks", []):
        return True
    if "dangerous" in cfg.get("risks", []):
        from .autonomy import RiskClass, classify_action_risk

        if classify_action_risk(spec, args) is RiskClass.DANGEROUS:
            return True
    if getattr(spec, "name", "") in cfg.get("tools", []):
        return True
    if tainted:
        from .untrusted import is_high_impact_action

        return is_high_impact_action(spec, args)
    return False


def configured_pin(cfg: dict[str, Any]) -> str:
    """Read the configured PIN from the environment (never from repo/YAML)."""
    env_name = str(cfg.get("pin_env") or "NEXUS_APPROVAL_PIN")
    if not _ENV_NAME_RE.fullmatch(env_name):
        return ""
    value = os.environ.get(env_name, "").strip()
    if not value and env_name == "NEXUS_APPROVAL_PIN":
        value = os.environ.get("TELEGRAM_APPROVAL_PIN", "").strip()
    if not (6 <= len(value) <= 64) or any(ch.isspace() for ch in value):
        return ""
    return value


def _failure_path(workspace: Path | str) -> Path:
    from .config import nexus_home

    home = nexus_home().resolve()
    key = hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()[:16]
    root = (home / "security" / key).resolve()
    if not root.is_relative_to(home):
        raise ValueError("Security state must remain inside Nexus home")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root / "2fa_failures.json"


def _check_and_record_failure(workspace: Path | str, *, record_failure: bool = False) -> bool:
    """Return True if under failure cap; False if locked out."""
    path = _failure_path(workspace)
    lock = path.with_suffix(".lock")
    if path.is_symlink() or lock.is_symlink():
        return False
    now = time.time()
    cutoff = now - _FAIL_WINDOW_S
    with FileLock(str(lock), timeout=5):
        attempts: list[float] = []
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    attempts = [float(x) for x in raw if isinstance(x, (int, float)) and float(x) > cutoff]
            except (OSError, ValueError, json.JSONDecodeError):
                attempts = []
        if len(attempts) >= _FAIL_LIMIT:
            return False
        if record_failure:
            attempts.append(now)
            tmp = path.with_name(f".2fa.{uuid.uuid4().hex[:8]}.tmp")
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(json.dumps(attempts).encode("utf-8"))
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
            return len(attempts) < _FAIL_LIMIT
        return True


def _clear_failures(workspace: Path | str) -> None:
    path = _failure_path(workspace)
    if path.exists() and not path.is_symlink():
        path.unlink(missing_ok=True)


def _audit_2fa(workspace: Path | str, spec_name: str, method: str, ok: bool, detail: str) -> None:
    from . import audit

    try:
        audit.record(
            workspace,
            "security.second_factor",
            who="user" if ok else "agent",
            target=spec_name,
            result="approved" if ok else "denied",
            risk="high",
            detail={"method": method, "reason": detail[:200]},
        )
    except (OSError, ValueError):
        pass


def verify(
    workspace: Path | str,
    cfg: dict[str, Any],
    spec: Any,
    args: dict[str, Any],
    preview: str,
    *,
    decision: Any = None,
    pin_callback: Callable[[Any, dict[str, Any], str], str] | None = None,
    telegram_bot_factory: Callable[[], Any] | None = None,
) -> tuple[bool, str]:
    """Verify second-factor confirmation for a dangerous/configured action."""
    method = str(cfg.get("method") or "pin")
    spec_name = getattr(spec, "name", "unknown")

    if method in {"pin", "either"}:
        expected_pin = configured_pin(cfg)
        supplied = str(getattr(decision, "second_factor", "") or "").strip() if decision is not None else ""
        if not supplied and pin_callback is not None:
            supplied = str(pin_callback(spec, args, preview) or "").strip()
        if not supplied and method == "pin" and sys.stdin.isatty():
            try:
                supplied = getpass.getpass(f"Second-factor PIN for {spec_name}: ").strip()
            except (EOFError, KeyboardInterrupt):
                supplied = ""
        if supplied:
            if not expected_pin:
                _audit_2fa(workspace, spec_name, "pin", False, "PIN env var missing or invalid")
                return False, f"second-factor PIN required in {cfg.get('pin_env', 'NEXUS_APPROVAL_PIN')}"
            if not _check_and_record_failure(workspace, record_failure=False):
                _audit_2fa(workspace, spec_name, "pin", False, "too many failed PIN attempts")
                return False, "second-factor PIN temporarily locked after too many failed attempts"
            if hmac.compare_digest(supplied.encode("utf-8"), expected_pin.encode("utf-8")):
                _clear_failures(workspace)
                _audit_2fa(workspace, spec_name, "pin", True, "PIN verified")
                return True, "second-factor PIN verified"
            _check_and_record_failure(workspace, record_failure=True)
            _audit_2fa(workspace, spec_name, "pin", False, "invalid PIN")
            return False, "invalid second-factor PIN"
        if method == "pin":
            _audit_2fa(workspace, spec_name, "pin", False, "no PIN provided")
            return False, "second-factor PIN confirmation required"

    if method in {"telegram", "either"}:
        bot = None
        try:
            if telegram_bot_factory is not None:
                bot = telegram_bot_factory()
            else:
                from .server import ServerState
                from .telegram.bot import Bot

                bot = Bot(ServerState(Path(workspace), quiet=True).workbench.studio)
            ok, reason = bot.request_second_factor(
                spec_name,
                args,
                preview=preview,
                timeout=int(cfg.get("timeout_seconds") or 60),
            )
            _audit_2fa(workspace, spec_name, "telegram", ok, reason)
            return ok, reason
        except Exception as exc:  # noqa: BLE001 - fail closed
            reason = f"Telegram second-factor unavailable: {exc}"
            _audit_2fa(workspace, spec_name, "telegram", False, reason)
            return False, reason
        finally:
            if bot is not None and telegram_bot_factory is None:
                try:
                    bot.close()
                except Exception:  # noqa: BLE001
                    pass

    return False, "second-factor verification refused"
