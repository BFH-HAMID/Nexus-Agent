"""Operator-owned autonomy risk classes, live status narration policy, and notification config."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .studio import redact
from .types import RiskClass

MAX_AUTONOMY_BYTES = 64_000

FORBIDDEN_INLINE_SECRET_KEYS = frozenset({
    "api_key",
    "api_secret",
    "access_key",
    "secret",
    "token",
    "password",
    "pin",
    "bot_token",
})


SAFE_VERBOSITIES = frozenset({"silent", "milestones", "chatty"})
VALID_VERBOSITY_LEVELS = SAFE_VERBOSITIES
SAFE_RISK_MODES = frozenset({"automatic", "automatic_notify", "fresh_approval"})
MEDIUM_RISK_MODES = frozenset({"automatic_notify", "fresh_approval"})
DANGEROUS_RISK_MODES = frozenset({"fresh_approval"})
PRESENCE_MODES = frozenset({"auto", "present", "away"})
AWAY_CHANNELS = frozenset({"auto", "telegram", "desktop", "none"})

# Built-in baseline risk classes by tool name. Unknown tools default to DANGEROUS.
BUILTIN_READ_TOOLS = frozenset({
    "security_status", "security_monitor", "security_report",
    "list_files",
    "read_file",
    "grep",
    "skill_view",
    "recall",
    "ask_agent",
    "finish",
    "request_clarification",
    "domain_fetch",
    "integration_gmail_search",
    "integration_gmail_read",
    "integration_gmail_summarize",
    "integration_calendar_agenda",
    "integration_calendar_free_time",
    "integration_contacts_resolve",
    "integration_drive_search",
    "integration_drive_read",
    "integration_docs_read",
    "integration_notes_search",
    "integration_notes_read",
})

BUILTIN_MEDIUM_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "remember",
    "skill_manage",
    "memory",
    "git_prepare",
    "gh_create_pr",
    "office_file",
    "sandbox_command",
    "integration_gmail_draft",
    "integration_gmail_label",
    "integration_gmail_archive",
})

BUILTIN_DANGEROUS_TOOLS = frozenset({
    "delete_file",
    "gh_merge_pr",
    "browser_action",
    "vscode_open",
    "project_action",
    "trace_reexecute",
    "integration_gmail_send",
})

# Commands treated as SAFE/MEDIUM test-runner invocations when clean (no chaining/redirection).
_SHELL_META_RE = re.compile(r"[;&|`$><\n\r]")
_TEST_CMD_RE = re.compile(
    r"^\s*(?:(?:\S*[/\\])?python(?:\d+(?:\.\d+)?)?\s+-m\s+(?:pytest|unittest)|"
    r"(?:\S*[/\\])?pytest|(?:\S*[/\\])?ruff\s+check|"
    r"(?:npm|pnpm|yarn)\s+test|cargo\s+test|go\s+test)\b",
    re.I,
)
_DANGEROUS_SHELL_RE = re.compile(
    r"\b(?:rm|del|rmdir|mkfs|dd|shutdown|reboot|curl|wget|ssh|scp|rsync|chmod|chown|"
    r"sudo|su|docker\s+push|kubectl\s+apply|kubectl\s+delete|terraform\s+apply|"
    r"helm\s+upgrade|npm\s+publish|twine\s+upload|pip\s+install|apt|yum|brew|"
    r"sendmail|mail|nc|ncat|python\s+-c)\b",
    re.I,
)
_DANGEROUS_ACTION_WORDS_RE = re.compile(
    r"\b(?:deploy|publish|release|send[-_ ]?(?:email|mail|message|sms|tweet|post)|"
    r"delete|remove|drop|wipe|destroy|spend|pay|charge|transfer|purchase|buy|"
    r"chmod|chown|grant|revoke)\b",
    re.I,
)
_PROTECTED_POLICY_NAMES = frozenset({
    "permissions.yaml",
    "autonomy.yaml",
    "chief.yaml",
    "voice.yaml",
    "persona.yaml",
    "proactive.yaml",
    "mcp.yaml",
    "telegram.yaml",
    "hud.yaml",
    "widget.yaml",
    "context.yaml",
    "vision.yaml",
    "memory.yaml",
    "security.yaml",
})

_GIT_READONLY_SUBS = frozenset({
    "status", "diff", "log", "show", "branch", "remote", "tag",
    "rev-parse", "ls-files", "describe", "shortlog", "config", "stash",
})
_GIT_MEDIUM_SUBS = frozenset({
    "add", "commit", "checkout", "switch", "branch", "tag", "stash", "restore",
})
_GH_READONLY_PAIRS = frozenset({
    ("pr", "list"), ("pr", "view"), ("pr", "diff"), ("pr", "checks"),
    ("issue", "list"), ("issue", "view"), ("repo", "list"), ("repo", "view"),
    ("run", "list"), ("run", "view"), ("workflow", "list"), ("workflow", "view"),
    ("release", "list"), ("release", "view"),
})


@dataclass(frozen=True)
class RiskPolicy:
    SAFE: str = "automatic"
    MEDIUM: str = "automatic_notify"
    DANGEROUS: str = "fresh_approval"

    def mode_for(self, risk_class: RiskClass) -> str:
        if risk_class is RiskClass.SAFE:
            return self.SAFE
        if risk_class is RiskClass.MEDIUM:
            return self.MEDIUM
        return self.DANGEROUS

    def to_dict(self) -> dict[str, str]:
        return {
            "SAFE": self.SAFE,
            "MEDIUM": self.MEDIUM,
            "DANGEROUS": self.DANGEROUS,
        }


@dataclass(frozen=True)
class NarrationPolicy:
    enabled: bool = False
    verbosity: str = "milestones"  # silent | milestones | chatty
    cooldown_seconds: float = 0.0
    max_chars: int = 180

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "verbosity": self.verbosity,
            "cooldown_seconds": self.cooldown_seconds,
            "max_chars": self.max_chars,
        }


@dataclass(frozen=True)
class NotificationPolicy:
    enabled: bool = False
    presence: str = "auto"  # auto | present | away
    present_ttl_seconds: int = 300
    voice_when_present: bool = True
    away_channel: str = "auto"  # auto | telegram | desktop | none

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "presence": self.presence,
            "present_ttl_seconds": self.present_ttl_seconds,
            "voice_when_present": self.voice_when_present,
            "away_channel": self.away_channel,
        }


@dataclass(frozen=True)
class SchedulerPolicy:
    enabled: bool = False
    timezone: str = ""
    max_schedules: int = 64
    auto_resume_crashed: bool = False
    default_target: str = "queue"  # queue | chief

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "timezone": self.timezone,
            "max_schedules": self.max_schedules,
            "auto_resume_crashed": self.auto_resume_crashed,
            "default_target": self.default_target,
        }


@dataclass(frozen=True)
class WatchdogPolicy:
    enabled: bool = False
    step_timeout_seconds: float = 300.0
    max_identical_tool_calls: int = 3
    poll_seconds: float = 5.0
    notify_on_trigger: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "step_timeout_seconds": self.step_timeout_seconds,
            "max_identical_tool_calls": self.max_identical_tool_calls,
            "poll_seconds": self.poll_seconds,
            "notify_on_trigger": self.notify_on_trigger,
        }


@dataclass(frozen=True)
class AutonomyConfig:
    schema: int = 1
    level: str = "L1"
    max_queue: int = 20
    max_depth: int = 3
    cooldown_seconds: int = 60
    ttl_seconds: int = 86400
    max_reserved_cost: float = 1.0
    timezone: str = ""
    risk_classes: RiskPolicy = field(default_factory=RiskPolicy)
    status_narration: NarrationPolicy = field(default_factory=NarrationPolicy)
    notifications: NotificationPolicy = field(default_factory=NotificationPolicy)
    scheduler: SchedulerPolicy = field(default_factory=SchedulerPolicy)
    watchdog: WatchdogPolicy = field(default_factory=WatchdogPolicy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "level": self.level,
            "max_queue": self.max_queue,
            "max_depth": self.max_depth,
            "cooldown_seconds": self.cooldown_seconds,
            "ttl_seconds": self.ttl_seconds,
            "max_reserved_cost": self.max_reserved_cost,
            "timezone": self.timezone,
            "risk_classes": self.risk_classes.to_dict(),
            "status_narration": self.status_narration.to_dict(),
            "notifications": self.notifications.to_dict(),
            "scheduler": self.scheduler.to_dict(),
            "watchdog": self.watchdog.to_dict(),
        }


def _scan_no_inline_secrets(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).strip().lower()
            if lowered in FORBIDDEN_INLINE_SECRET_KEYS and isinstance(value, str) and value.strip():
                raise ValueError(f"autonomy.yaml must not contain inline secrets ({key})")
            _scan_no_inline_secrets(value)
    elif isinstance(node, list):
        for item in node:
            _scan_no_inline_secrets(item)
    elif isinstance(node, str) and node.strip() and redact(node) != node:
        raise ValueError("autonomy.yaml must not contain inline secrets")


def parse_risk_classes(value: Any) -> dict[str, str]:
    if value is None:
        return RiskPolicy().to_dict()
    if not isinstance(value, dict):
        raise ValueError("autonomy.yaml risk_classes must be a mapping")
    allowed = {"SAFE", "MEDIUM", "DANGEROUS"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown risk_classes key(s): {', '.join(sorted(unknown))}")
    safe_mode = value.get("SAFE", "automatic")
    med_mode = value.get("MEDIUM", "automatic_notify")
    danger_mode = value.get("DANGEROUS", "fresh_approval")
    if not isinstance(safe_mode, str) or safe_mode not in SAFE_RISK_MODES:
        raise ValueError("risk_classes.SAFE must be automatic, automatic_notify, or fresh_approval")
    if not isinstance(med_mode, str) or med_mode not in MEDIUM_RISK_MODES:
        raise ValueError("risk_classes.MEDIUM must be automatic_notify or fresh_approval")
    if not isinstance(danger_mode, str) or danger_mode not in DANGEROUS_RISK_MODES:
        raise ValueError("risk_classes.DANGEROUS must always be fresh_approval")
    return RiskPolicy(SAFE=safe_mode, MEDIUM=med_mode, DANGEROUS=danger_mode).to_dict()


def parse_status_narration(value: Any) -> dict[str, Any]:
    if value is None:
        return NarrationPolicy().to_dict()
    if not isinstance(value, dict):
        raise ValueError("autonomy.yaml status_narration must be a mapping")
    unknown = set(value) - {"enabled", "verbosity", "cooldown_seconds", "max_chars"}
    if unknown:
        raise ValueError(f"Unknown status_narration key(s): {', '.join(sorted(unknown))}")
    enabled = value.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("status_narration.enabled must be true or false")
    verbosity = value.get("verbosity", "milestones")
    if not isinstance(verbosity, str) or verbosity not in SAFE_VERBOSITIES:
        raise ValueError("status_narration.verbosity must be silent, milestones, or chatty")
    cooldown = value.get("cooldown_seconds", 0.0)
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or not math.isfinite(cooldown) or not 0.0 <= float(cooldown) <= 60.0:
        raise ValueError("status_narration.cooldown_seconds must be a finite number 0..60")
    max_chars = value.get("max_chars", 180)
    if type(max_chars) is not int or not 40 <= max_chars <= 500:
        raise ValueError("status_narration.max_chars must be an integer 40..500")
    return NarrationPolicy(
        enabled=enabled,
        verbosity=verbosity,
        cooldown_seconds=float(cooldown),
        max_chars=max_chars,
    ).to_dict()


def parse_notifications_policy(value: Any) -> dict[str, Any]:
    if value is None:
        return NotificationPolicy().to_dict()
    if not isinstance(value, dict):
        raise ValueError("autonomy.yaml notifications must be a mapping")
    unknown = set(value) - {"enabled", "presence", "present_ttl_seconds", "voice_when_present", "away_channel"}
    if unknown:
        raise ValueError(f"Unknown notifications key(s): {', '.join(sorted(unknown))}")
    enabled = value.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("notifications.enabled must be true or false")
    presence = value.get("presence", "auto")
    if not isinstance(presence, str) or presence not in PRESENCE_MODES:
        raise ValueError("notifications.presence must be auto, present, or away")
    ttl = value.get("present_ttl_seconds", 300)
    if type(ttl) is not int or not 10 <= ttl <= 86400:
        raise ValueError("notifications.present_ttl_seconds must be an integer 10..86400")
    voice_when_present = value.get("voice_when_present", True)
    if type(voice_when_present) is not bool:
        raise ValueError("notifications.voice_when_present must be true or false")
    away_channel = value.get("away_channel", "auto")
    if not isinstance(away_channel, str) or away_channel not in AWAY_CHANNELS:
        raise ValueError("notifications.away_channel must be auto, telegram, desktop, or none")
    return NotificationPolicy(
        enabled=enabled,
        presence=presence,
        present_ttl_seconds=ttl,
        voice_when_present=voice_when_present,
        away_channel=away_channel,
    ).to_dict()


def _validate_tz_name(value: Any, label: str = "timezone") -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"autonomy.yaml {label} must be an IANA timezone string")
    cleaned = value.strip()
    if not cleaned:
        return ""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown IANA {label}: {cleaned!r}") from exc
    return cleaned


def parse_scheduler_policy(value: Any) -> dict[str, Any]:
    if value is None:
        return SchedulerPolicy().to_dict()
    if not isinstance(value, dict):
        raise ValueError("autonomy.yaml scheduler must be a mapping")
    unknown = set(value) - {"enabled", "timezone", "max_schedules", "auto_resume_crashed", "default_target"}
    if unknown:
        raise ValueError(f"Unknown scheduler key(s): {', '.join(sorted(unknown))}")
    enabled = value.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("scheduler.enabled must be true or false")
    timezone = _validate_tz_name(value.get("timezone", ""), "scheduler.timezone")
    max_schedules = value.get("max_schedules", 64)
    if type(max_schedules) is not int or not 1 <= max_schedules <= 500:
        raise ValueError("scheduler.max_schedules must be an integer 1..500")
    auto_resume = value.get("auto_resume_crashed", False)
    if type(auto_resume) is not bool:
        raise ValueError("scheduler.auto_resume_crashed must be true or false")
    default_target = value.get("default_target", "queue")
    if not isinstance(default_target, str) or default_target not in {"queue", "chief"}:
        raise ValueError("scheduler.default_target must be queue or chief")
    return SchedulerPolicy(
        enabled=enabled,
        timezone=timezone,
        max_schedules=max_schedules,
        auto_resume_crashed=auto_resume,
        default_target=default_target,
    ).to_dict()


def parse_watchdog_policy(value: Any) -> dict[str, Any]:
    if value is None:
        return WatchdogPolicy().to_dict()
    if not isinstance(value, dict):
        raise ValueError("autonomy.yaml watchdog must be a mapping")
    unknown = set(value) - {
        "enabled",
        "step_timeout_seconds",
        "max_identical_tool_calls",
        "poll_seconds",
        "notify_on_trigger",
    }
    if unknown:
        raise ValueError(f"Unknown watchdog key(s): {', '.join(sorted(unknown))}")
    enabled = value.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("watchdog.enabled must be true or false")
    step_timeout = value.get("step_timeout_seconds", 300.0)
    if (
        isinstance(step_timeout, bool)
        or not isinstance(step_timeout, (int, float))
        or not math.isfinite(step_timeout)
        or not 0.05 <= float(step_timeout) <= 86400.0
    ):
        raise ValueError("watchdog.step_timeout_seconds must be a finite number 0.05..86400")
    max_identical = value.get("max_identical_tool_calls", 3)
    if type(max_identical) is not int or not 2 <= max_identical <= 50:
        raise ValueError("watchdog.max_identical_tool_calls must be an integer 2..50")
    poll_seconds = value.get("poll_seconds", 5.0)
    if (
        isinstance(poll_seconds, bool)
        or not isinstance(poll_seconds, (int, float))
        or not math.isfinite(poll_seconds)
        or not 0.02 <= float(poll_seconds) <= 60.0
    ):
        raise ValueError("watchdog.poll_seconds must be a finite number 0.02..60")
    notify_on_trigger = value.get("notify_on_trigger", True)
    if type(notify_on_trigger) is not bool:
        raise ValueError("watchdog.notify_on_trigger must be true or false")
    return WatchdogPolicy(
        enabled=enabled,
        step_timeout_seconds=float(step_timeout),
        max_identical_tool_calls=max_identical,
        poll_seconds=float(poll_seconds),
        notify_on_trigger=notify_on_trigger,
    ).to_dict()


AUTONOMY_ALLOWED_KEYS = frozenset({
    "schema",
    "level",
    "max_queue",
    "max_depth",
    "cooldown_seconds",
    "ttl_seconds",
    "max_reserved_cost",
    "timezone",
    "runtime",
    "triggers",
    "risk_classes",
    "status_narration",
    "notifications",
    "scheduler",
    "watchdog",
})


def load_autonomy_dict(workspace: Path | str | None = None) -> dict[str, Any]:
    """Read and strictly validate operator-owned ``autonomy.yaml``."""
    root = Path(workspace or Path.cwd()).resolve()
    path = root / "autonomy.yaml"
    defaults: dict[str, Any] = {
        "schema": 1,
        "level": "L1",
        "max_queue": 20,
        "max_depth": 3,
        "cooldown_seconds": 60,
        "ttl_seconds": 86400,
        "max_reserved_cost": 1.0,
        "timezone": "",
        "risk_classes": RiskPolicy().to_dict(),
        "status_narration": NarrationPolicy().to_dict(),
        "notifications": NotificationPolicy().to_dict(),
        "scheduler": SchedulerPolicy().to_dict(),
        "watchdog": WatchdogPolicy().to_dict(),
    }
    if not path.exists():
        return defaults
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_AUTONOMY_BYTES:
        raise ValueError("Autonomy requires a bounded regular operator-owned YAML file")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Malformed autonomy policy ({type(exc).__name__})") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - AUTONOMY_ALLOWED_KEYS:
        raise ValueError("Invalid autonomy policy keys")
    _scan_no_inline_secrets(raw)

    schema = raw.get("schema", 1)
    if type(schema) is not int or schema != 1:
        raise ValueError("Invalid autonomy policy schema")
    level = raw.get("level", "L1")
    if not isinstance(level, str) or level not in {"L0", "L1", "L2", "L3"}:
        raise ValueError("Unknown autonomy level")

    policy = {**defaults, **raw, "level": level}
    for key, low, high in (
        ("max_queue", 1, 100),
        ("max_depth", 0, 5),
        ("cooldown_seconds", 1, 86400),
        ("ttl_seconds", 60, 604800),
    ):
        if type(policy[key]) is not int or not low <= policy[key] <= high:
            raise ValueError("Invalid bounded autonomy limit")

    cost = policy["max_reserved_cost"]
    if type(cost) not in {int, float} or not math.isfinite(cost) or not 0 <= float(cost) <= 100:
        raise ValueError("Invalid queue cost reservation limit")
    policy["max_reserved_cost"] = float(cost)
    policy["timezone"] = _validate_tz_name(raw.get("timezone", ""), "timezone")
    policy["risk_classes"] = parse_risk_classes(raw.get("risk_classes"))
    policy["status_narration"] = parse_status_narration(raw.get("status_narration"))
    policy["notifications"] = parse_notifications_policy(raw.get("notifications"))
    policy["scheduler"] = parse_scheduler_policy(raw.get("scheduler"))
    policy["watchdog"] = parse_watchdog_policy(raw.get("watchdog"))
    return policy


def load_autonomy_config(workspace: Path | str | None = None) -> AutonomyConfig:
    data = load_autonomy_dict(workspace)
    return AutonomyConfig(
        schema=1,
        level=data["level"],
        max_queue=data["max_queue"],
        max_depth=data["max_depth"],
        cooldown_seconds=data["cooldown_seconds"],
        ttl_seconds=data["ttl_seconds"],
        max_reserved_cost=data["max_reserved_cost"],
        timezone=data["timezone"],
        risk_classes=RiskPolicy(**data["risk_classes"]),
        status_narration=NarrationPolicy(**data["status_narration"]),
        notifications=NotificationPolicy(**data["notifications"]),
        scheduler=SchedulerPolicy(**data["scheduler"]),
        watchdog=WatchdogPolicy(**data["watchdog"]),
    )


def resolve_timezone(workspace: Path | str | None = None, explicit: str | None = None) -> str:
    """Resolve user's configured IANA timezone (explicit -> autonomy -> agents/env -> UTC)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if explicit and explicit.strip():
        return _validate_tz_name(explicit.strip(), "explicit timezone")
    root = Path(workspace or Path.cwd()).resolve()
    try:
        policy = load_autonomy_dict(root)
        sched_tz = str((policy.get("scheduler") or {}).get("timezone") or "").strip()
        if sched_tz:
            return sched_tz
        top_tz = str(policy.get("timezone") or "").strip()
        if top_tz:
            return top_tz
    except (ValueError, OSError):
        pass
    try:
        from .config import load_config

        cfg_tz = str(getattr(load_config(root).settings, "timezone", "") or "").strip()
        if cfg_tz:
            ZoneInfo(cfg_tz)
            return cfg_tz
    except (Exception, ZoneInfoNotFoundError):
        pass
    import os

    env_tz = (os.environ.get("NEXUS_TIMEZONE") or os.environ.get("TZ") or "").strip()
    if env_tz:
        try:
            ZoneInfo(env_tz)
            return env_tz
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return "UTC"


def _targets_protected_policy(args: dict[str, Any]) -> bool:
    for key in ("path", "directory", "cwd"):
        val = args.get(key)
        if not isinstance(val, str) or not val.strip():
            continue
        lowered = val.replace("\\", "/").strip().casefold()
        name = Path(lowered).name
        if name in _PROTECTED_POLICY_NAMES or "/plugins/" in f"/{lowered}/" or lowered.startswith("plugins/"):
            return True
    return False


def _git_parts(args: dict[str, Any]) -> list[str]:
    raw = args.get("args")
    if isinstance(raw, str):
        import shlex

        try:
            return shlex.split(raw)
        except ValueError:
            return raw.split()
    if isinstance(raw, (list, tuple)):
        return [str(p) for p in raw]
    return []


def classify_tool_risk(
    spec_or_name: Any,
    args: dict[str, Any] | None = None,
    *,
    registry: Any = None,
) -> RiskClass:
    """Classify a tool or tool call into SAFE / MEDIUM / DANGEROUS.

    Unknown or undeclared tools ALWAYS default to DANGEROUS.
    """
    call_args = args if isinstance(args, dict) else {}

    if isinstance(spec_or_name, str):
        name = spec_or_name.strip()
        if registry is not None:
            if hasattr(registry, "has") and not registry.has(name):
                return RiskClass.DANGEROUS
            tool = getattr(registry, "_tools", {}).get(name)
            if tool is not None:
                return classify_tool_risk(tool.spec, call_args, registry=None)
        if name in BUILTIN_READ_TOOLS:
            return RiskClass.SAFE
        if name in BUILTIN_MEDIUM_TOOLS or name in {"git", "gh", "run_command"}:
            spec_like_name = name
        else:
            # Unknown tool name defaults strictly to DANGEROUS.
            return RiskClass.DANGEROUS
        declared_risk = None
        risk_declared = True
    else:
        spec = spec_or_name
        if spec is None:
            return RiskClass.DANGEROUS
        spec_like_name = str(getattr(spec, "name", "") or "").strip()
        risk_declared = bool(getattr(spec, "risk_declared", True))
        declared_risk = getattr(spec, "risk", None)

    if not spec_like_name:
        return RiskClass.DANGEROUS

    # Undeclared tools outside known safe/medium built-ins default to DANGEROUS.
    if not risk_declared and spec_like_name not in BUILTIN_READ_TOOLS and spec_like_name not in BUILTIN_MEDIUM_TOOLS:
        return RiskClass.DANGEROUS
    if spec_like_name.startswith("mcp_") or spec_like_name in BUILTIN_DANGEROUS_TOOLS:
        return RiskClass.DANGEROUS
    if declared_risk is not None and getattr(declared_risk, "value", str(declared_risk)) == "danger":
        return RiskClass.DANGEROUS

    # Policy file edits are always DANGEROUS regardless of tool name.
    if spec_like_name in {"write_file", "edit_file", "delete_file", "office_file"} and _targets_protected_policy(call_args):
        return RiskClass.DANGEROUS

    # Argument-level classification for multi-purpose tools
    if spec_like_name == "office_file":
        return RiskClass.SAFE if call_args.get("action") == "read" else RiskClass.MEDIUM

    if spec_like_name in {"integration_calendar_create", "integration_calendar_update"}:
        attendees = call_args.get("attendees")
        if isinstance(attendees, (list, tuple)) and any(str(a).strip() for a in attendees):
            return RiskClass.DANGEROUS
        return RiskClass.MEDIUM

    if spec_like_name == "git":
        parts = _git_parts(call_args)
        if not parts:
            return RiskClass.DANGEROUS
        sub = parts[0].strip("-")
        if sub in _GIT_READONLY_SUBS:
            if sub == "branch" and not (len(parts) == 1 or parts[1:] in (["--list"], ["-l"])):
                if any(flag in parts[1:] for flag in ("-d", "-D", "--delete")):
                    return RiskClass.DANGEROUS
                return RiskClass.MEDIUM
            if sub == "tag" and not (len(parts) == 1 or parts[1:] in (["--list"], ["-l"])):
                if any(flag in parts[1:] for flag in ("-d", "--delete")):
                    return RiskClass.DANGEROUS
                return RiskClass.MEDIUM
            if sub == "stash" and len(parts) > 1 and parts[1] not in {"list", "show"}:
                return RiskClass.MEDIUM
            if sub == "config" and not (len(parts) in {2, 3} and parts[1] in {"--get", "--get-all", "--list"}):
                return RiskClass.DANGEROUS
            return RiskClass.SAFE
        if sub == "push":
            return RiskClass.DANGEROUS
        if sub in {"send-email", "clean", "reset", "rebase", "merge", "cherry-pick", "filter-branch"}:
            return RiskClass.DANGEROUS
        if sub in _GIT_MEDIUM_SUBS:
            return RiskClass.MEDIUM
        return RiskClass.DANGEROUS

    if spec_like_name == "git_prepare":
        if call_args.get("push"):
            branch = str(call_args.get("branch") or "").casefold()
            if branch in {"main", "master"} or not branch:
                return RiskClass.DANGEROUS
            return RiskClass.DANGEROUS
        return RiskClass.MEDIUM

    if spec_like_name == "gh":
        parts = _git_parts(call_args)
        if len(parts) >= 2 and (parts[0], parts[1]) in _GH_READONLY_PAIRS:
            return RiskClass.SAFE
        if len(parts) >= 2 and (parts[0], parts[1]) == ("pr", "create"):
            return RiskClass.MEDIUM
        return RiskClass.DANGEROUS

    if spec_like_name == "run_command":
        cmd = str(call_args.get("command") or "").strip()
        if not cmd or _SHELL_META_RE.search(cmd) or _DANGEROUS_SHELL_RE.search(cmd):
            return RiskClass.DANGEROUS
        if _TEST_CMD_RE.match(cmd):
            return RiskClass.MEDIUM
        return RiskClass.DANGEROUS

    if _DANGEROUS_ACTION_WORDS_RE.search(spec_like_name):
        return RiskClass.DANGEROUS

    if declared_risk is not None:
        rval = getattr(declared_risk, "value", str(declared_risk))
        if rval == "read":
            return RiskClass.SAFE
        if rval == "write":
            return RiskClass.MEDIUM
        if rval == "danger":
            return RiskClass.DANGEROUS
    if spec_like_name in BUILTIN_READ_TOOLS:
        return RiskClass.SAFE
    if spec_like_name in BUILTIN_MEDIUM_TOOLS:
        return RiskClass.MEDIUM
    return RiskClass.DANGEROUS


def classify_action_risk(
    spec_or_name: Any,
    args: dict[str, Any] | None = None,
    *,
    registry: Any = None,
) -> RiskClass:
    """Alias for ``classify_tool_risk`` evaluating both tool declaration and call arguments."""
    return classify_tool_risk(spec_or_name, args, registry=registry)


__all__ = [
    "AUTONOMY_ALLOWED_KEYS",
    "AWAY_CHANNELS",
    "BUILTIN_DANGEROUS_TOOLS",
    "BUILTIN_MEDIUM_TOOLS",
    "BUILTIN_READ_TOOLS",
    "DANGEROUS_RISK_MODES",
    "MEDIUM_RISK_MODES",
    "PRESENCE_MODES",
    "SAFE_RISK_MODES",
    "SAFE_VERBOSITIES",
    "VALID_VERBOSITY_LEVELS",
    "AutonomyConfig",
    "NarrationPolicy",
    "NotificationPolicy",
    "RiskClass",
    "RiskPolicy",
    "classify_action_risk",
    "classify_tool_risk",
    "load_autonomy_config",
    "load_autonomy_dict",
    "parse_notifications_policy",
    "parse_risk_classes",
    "parse_status_narration",
]
