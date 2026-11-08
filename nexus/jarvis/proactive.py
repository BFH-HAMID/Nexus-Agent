"""Proactive assistant engine extending ``nexus.jarvis`` (OFF by default).

Enabled and configured strictly via operator-owned ``proactive.yaml``.
Proactive features ONLY propose actions; any execution follows existing
``autonomy.yaml``, ``permissions.yaml``, ``Queue``, ``ChiefOfStaff``, and
``ApprovalGate`` policies. Text from emails, calendar events, RSS feeds,
web endpoints, or logs is untrusted data and is never executed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import yaml
from filelock import FileLock

from .. import audit as auditlog
from ..autonomy import load_autonomy_dict, resolve_timezone
from ..config import nexus_home
from ..github_safety import scan_text
from ..notifications import is_user_present
from ..permissions import Permissions
from ..redaction import mask
from ..redaction import secrets as known_secrets
from ..scheduler import (
    MAX_TASK_CHARS,
    Scheduler,
    _adjust_hour,
    _clean_task_phrase,
    _extract_time_of_day,
    normalize_bangla_digits,
)
from ..studio import redact
from ..untrusted import escape_delimiters, flag_names
from .sources import bounded, github

MAX_PROACTIVE_BYTES = 64_000
MAX_MONITORS = 50
MAX_SUPPRESSED_SUGGESTIONS = 200
MAX_HUD_TOASTS = 100
MAX_LOG_BYTES = 262_144

SEVERITY_LEVELS: tuple[str, ...] = ("info", "low", "medium", "high", "critical")
SEVERITY_RANK: dict[str, int] = {name: idx for idx, name in enumerate(SEVERITY_LEVELS)}
VALID_CHANNELS: frozenset[str] = frozenset({"hud_toast", "voice", "telegram", "desktop"})
MONITOR_KINDS: frozenset[str] = frozenset({
    "website",
    "server",
    "github_ci",
    "disk",
    "cpu",
    "memory",
    "system",
    "price",
    "stock",
    "market",
    "log_pattern",
    "rss_keyword",
})

FORBIDDEN_INLINE_SECRET_KEYS: frozenset[str] = frozenset({
    "api_key",
    "api_secret",
    "access_key",
    "secret",
    "token",
    "password",
    "pin",
    "bot_token",
})

PROACTIVE_ALLOWED_KEYS: frozenset[str] = frozenset({
    "schema",
    "enabled",
    "timezone",
    "morning_brief",
    "voice_reminders",
    "monitors",
    "suggestions",
    "dnd",
    "channels",
})

_WEEKDAY_ALIASES: dict[str, int] = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_DAY_INDEX_TO_REMINDER = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

_WEATHER_CODE_TEXT: dict[int, str] = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "cloudy",
    45: "foggy",
    48: "foggy",
    51: "light drizzle",
    61: "rain",
    63: "moderate rain",
    65: "heavy rain",
    71: "snow",
    80: "showers",
    95: "thunderstorm",
    96: "thunderstorm",
    99: "severe thunderstorm",
}


class ProactiveError(ValueError):
    """Base error for proactive assistant configuration or runtime failures."""


class ProactiveDisabledError(ProactiveError):
    """Raised when proactive features are invoked while disabled in ``proactive.yaml``."""


# ── config dataclasses & validation ────────────────────────────────────── #


@dataclass(frozen=True)
class MorningBriefPolicy:
    enabled: bool = False
    time: str = "08:00"
    deliver_on_greeting: bool = True
    deliver_on_schedule: bool = True
    spoken_max_chars: int = 320
    news_topics: tuple[str, ...] = ()
    max_news_items: int = 5
    include_weather: bool = True
    include_calendar: bool = True
    include_email: bool = True
    include_tasks: bool = True
    include_system: bool = True
    include_ci: bool = True
    ci_repo: str = ""
    ci_ref: str = "HEAD"

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "time": self.time,
            "deliver_on_greeting": self.deliver_on_greeting,
            "deliver_on_schedule": self.deliver_on_schedule,
            "spoken_max_chars": self.spoken_max_chars,
            "news_topics": list(self.news_topics),
            "max_news_items": self.max_news_items,
            "include_weather": self.include_weather,
            "include_calendar": self.include_calendar,
            "include_email": self.include_email,
            "include_tasks": self.include_tasks,
            "include_system": self.include_system,
            "include_ci": self.include_ci,
            "ci_repo": self.ci_repo,
            "ci_ref": self.ci_ref,
        }


@dataclass(frozen=True)
class VoiceRemindersPolicy:
    enabled: bool = False
    default_recurring_target: str = "queue"

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "default_recurring_target": self.default_recurring_target,
        }


@dataclass(frozen=True)
class MonitorSpec:
    id: str
    kind: str
    enabled: bool = False
    threshold: float = 0.0
    cooldown_seconds: float = 300.0
    poll_seconds: float = 60.0
    severity: str = "medium"
    notify_on_recovery: bool = False
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "enabled": self.enabled,
            "threshold": self.threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "poll_seconds": self.poll_seconds,
            "severity": self.severity,
            "notify_on_recovery": self.notify_on_recovery,
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class MonitorsPolicy:
    enabled: bool = False
    items: tuple[MonitorSpec, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class SuggestionsPolicy:
    enabled: bool = False
    max_speech_per_hour: int = 3
    meeting_soon_minutes: int = 30
    deadline_hours: int = 24
    cooldown_seconds: float = 900.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_speech_per_hour": self.max_speech_per_hour,
            "meeting_soon_minutes": self.meeting_soon_minutes,
            "deadline_hours": self.deadline_hours,
            "cooldown_seconds": self.cooldown_seconds,
        }


@dataclass(frozen=True)
class DNDSchedulePolicy:
    enabled: bool = False
    start: str = "22:00"
    end: str = "07:00"
    days: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "start": self.start,
            "end": self.end,
            "days": [_DAY_INDEX_TO_REMINDER[d].lower() for d in self.days],
        }


@dataclass(frozen=True)
class UrgentOverridePolicy:
    enabled: bool = False
    min_severity: str = "critical"
    allowed_sources: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "min_severity": self.min_severity,
            "allowed_sources": list(self.allowed_sources),
        }


@dataclass(frozen=True)
class DNDPolicy:
    enabled: bool = False
    schedule: DNDSchedulePolicy = field(default_factory=DNDSchedulePolicy)
    focus_default_minutes: int = 60
    urgent_override: UrgentOverridePolicy = field(default_factory=UrgentOverridePolicy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "schedule": self.schedule.to_dict(),
            "focus_default_minutes": self.focus_default_minutes,
            "urgent_override": self.urgent_override.to_dict(),
        }


def _default_severity_routing() -> dict[str, tuple[str, ...]]:
    return {
        "info": ("hud_toast",),
        "low": ("hud_toast",),
        "medium": ("hud_toast", "voice"),
        "high": ("hud_toast", "voice", "telegram", "desktop"),
        "critical": ("hud_toast", "voice", "telegram", "desktop"),
    }


@dataclass(frozen=True)
class ChannelsPolicy:
    enabled: bool = False
    away_fallback: bool = True
    severity_routing: dict[str, tuple[str, ...]] = field(default_factory=_default_severity_routing)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "away_fallback": self.away_fallback,
            "severity_routing": {k: list(v) for k, v in self.severity_routing.items()},
        }


@dataclass(frozen=True)
class ProactiveConfig:
    schema: int = 1
    enabled: bool = False
    timezone: str = ""
    morning_brief: MorningBriefPolicy = field(default_factory=MorningBriefPolicy)
    voice_reminders: VoiceRemindersPolicy = field(default_factory=VoiceRemindersPolicy)
    monitors: MonitorsPolicy = field(default_factory=MonitorsPolicy)
    suggestions: SuggestionsPolicy = field(default_factory=SuggestionsPolicy)
    dnd: DNDPolicy = field(default_factory=DNDPolicy)
    channels: ChannelsPolicy = field(default_factory=ChannelsPolicy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "enabled": self.enabled,
            "timezone": self.timezone,
            "morning_brief": self.morning_brief.to_dict(),
            "voice_reminders": self.voice_reminders.to_dict(),
            "monitors": self.monitors.to_dict(),
            "suggestions": self.suggestions.to_dict(),
            "dnd": self.dnd.to_dict(),
            "channels": self.channels.to_dict(),
        }


def _scan_no_inline_secrets(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).strip().lower()
            if lowered in FORBIDDEN_INLINE_SECRET_KEYS and isinstance(value, str) and value.strip():
                raise ProactiveError(f"proactive.yaml must not contain inline secrets ({key})")
            _scan_no_inline_secrets(value)
    elif isinstance(node, list):
        for item in node:
            _scan_no_inline_secrets(item)
    elif isinstance(node, str) and node.strip() and redact(node) != node:
        raise ProactiveError("proactive.yaml must not contain inline secrets")


def _require_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ProactiveError(f"proactive.yaml {label} must be true or false")
    return value


def _require_hhmm(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value.strip()):
        raise ProactiveError(f"proactive.yaml {label} must be HH:MM")
    cleaned = value.strip()
    try:
        datetime.strptime(cleaned, "%H:%M")
    except ValueError as exc:
        raise ProactiveError(f"proactive.yaml {label} must be a valid 24h HH:MM") from exc
    return cleaned


def _require_tz(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProactiveError(f"proactive.yaml {label} must be an IANA timezone string")
    cleaned = value.strip()
    if not cleaned:
        return ""
    try:
        ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ProactiveError(f"Unknown IANA timezone in {label}: {cleaned!r}") from exc
    return cleaned


def _parse_morning_brief(raw: Any) -> MorningBriefPolicy:
    if raw is None:
        return MorningBriefPolicy()
    if not isinstance(raw, dict):
        raise ProactiveError("proactive.yaml morning_brief must be a mapping")
    allowed = {
        "enabled", "time", "deliver_on_greeting", "deliver_on_schedule", "spoken_max_chars",
        "news_topics", "max_news_items", "include_weather", "include_calendar",
        "include_email", "include_tasks", "include_system", "include_ci", "ci_repo", "ci_ref",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ProactiveError(f"Unknown morning_brief key(s): {', '.join(sorted(unknown))}")
    enabled = _require_bool(raw.get("enabled", False), "morning_brief.enabled")
    brief_time = _require_hhmm(raw.get("time", "08:00"), "morning_brief.time")
    deliver_on_greeting = _require_bool(raw.get("deliver_on_greeting", True), "morning_brief.deliver_on_greeting")
    deliver_on_schedule = _require_bool(raw.get("deliver_on_schedule", True), "morning_brief.deliver_on_schedule")
    spoken_max = raw.get("spoken_max_chars", 320)
    if type(spoken_max) is not int or not 60 <= spoken_max <= 600:
        raise ProactiveError("morning_brief.spoken_max_chars must be an integer 60..600")
    topics_raw = raw.get("news_topics", [])
    if not isinstance(topics_raw, list) or len(topics_raw) > 20:
        raise ProactiveError("morning_brief.news_topics must be a list of up to 20 strings")
    topics: list[str] = []
    for item in topics_raw:
        if not isinstance(item, str) or not 1 <= len(item.strip()) <= 60:
            raise ProactiveError("Each morning_brief.news_topics item must be 1..60 chars")
        topics.append(item.strip())
    max_news = raw.get("max_news_items", 5)
    if type(max_news) is not int or not 1 <= max_news <= 20:
        raise ProactiveError("morning_brief.max_news_items must be an integer 1..20")
    ci_repo = str(raw.get("ci_repo", "") or "").strip()
    if ci_repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", ci_repo):
        raise ProactiveError("morning_brief.ci_repo must be owner/repo")
    ci_ref = str(raw.get("ci_ref", "HEAD") or "HEAD").strip()
    if not 1 <= len(ci_ref) <= 180:
        raise ProactiveError("morning_brief.ci_ref must be 1..180 chars")
    return MorningBriefPolicy(
        enabled=enabled,
        time=brief_time,
        deliver_on_greeting=deliver_on_greeting,
        deliver_on_schedule=deliver_on_schedule,
        spoken_max_chars=spoken_max,
        news_topics=tuple(dict.fromkeys(topics)),
        max_news_items=max_news,
        include_weather=_require_bool(raw.get("include_weather", True), "morning_brief.include_weather"),
        include_calendar=_require_bool(raw.get("include_calendar", True), "morning_brief.include_calendar"),
        include_email=_require_bool(raw.get("include_email", True), "morning_brief.include_email"),
        include_tasks=_require_bool(raw.get("include_tasks", True), "morning_brief.include_tasks"),
        include_system=_require_bool(raw.get("include_system", True), "morning_brief.include_system"),
        include_ci=_require_bool(raw.get("include_ci", True), "morning_brief.include_ci"),
        ci_repo=ci_repo,
        ci_ref=ci_ref,
    )


def _parse_voice_reminders(raw: Any) -> VoiceRemindersPolicy:
    if raw is None:
        return VoiceRemindersPolicy()
    if not isinstance(raw, dict):
        raise ProactiveError("proactive.yaml voice_reminders must be a mapping")
    unknown = set(raw) - {"enabled", "default_recurring_target"}
    if unknown:
        raise ProactiveError(f"Unknown voice_reminders key(s): {', '.join(sorted(unknown))}")
    enabled = _require_bool(raw.get("enabled", False), "voice_reminders.enabled")
    target = str(raw.get("default_recurring_target", "queue") or "queue").strip()
    if target not in {"queue", "chief"}:
        raise ProactiveError("voice_reminders.default_recurring_target must be queue or chief")
    return VoiceRemindersPolicy(enabled=enabled, default_recurring_target=target)


def _validate_url(url: Any, *, https_only: bool = False) -> str:
    if not isinstance(url, str):
        raise ProactiveError("Monitor url must be a string")
    cleaned = url.strip()
    parsed = urlsplit(cleaned)
    schemes = {"https"} if https_only else {"https", "http"}
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password or len(cleaned) > 2048:
        raise ProactiveError("Invalid monitor URL")
    return cleaned


def _parse_monitor_item(raw: Any, workspace: Path, seen_ids: set[str]) -> MonitorSpec:
    if not isinstance(raw, dict):
        raise ProactiveError("Each monitor entry must be a mapping")
    allowed = {
        "id", "kind", "enabled", "threshold", "cooldown_seconds", "poll_seconds",
        "severity", "notify_on_recovery",
        # Kind-specific explicit options (also permitted directly or inside options):
        "url", "status", "max_latency_ms", "host", "port",
        "repo", "ref",
        "metric", "direction",
        "symbol", "field",
        "path", "pattern", "case_sensitive",
        "keywords", "source_ids",
        "options",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ProactiveError(f"Unknown monitor key(s): {', '.join(sorted(unknown))}")
    ident = raw.get("id")
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", ident) or ident in seen_ids:
        raise ProactiveError("Monitor id must be unique 1..64 identifier characters")
    seen_ids.add(ident)
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in MONITOR_KINDS:
        raise ProactiveError(f"Unknown monitor kind {kind!r}")
    enabled = _require_bool(raw.get("enabled", False), f"monitors.{ident}.enabled")
    severity = str(raw.get("severity", "medium") or "medium").lower()
    if severity not in SEVERITY_LEVELS:
        raise ProactiveError(f"Invalid monitor severity {severity!r}")
    cooldown = raw.get("cooldown_seconds", 300.0)
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or not math.isfinite(cooldown) or not 0 <= float(cooldown) <= 86400:
        raise ProactiveError("Monitor cooldown_seconds must be 0..86400")
    poll_s = raw.get("poll_seconds", 60.0)
    if isinstance(poll_s, bool) or not isinstance(poll_s, (int, float)) or not math.isfinite(poll_s) or not 1 <= float(poll_s) <= 86400:
        raise ProactiveError("Monitor poll_seconds must be 1..86400")
    notify_rec = _require_bool(raw.get("notify_on_recovery", False), f"monitors.{ident}.notify_on_recovery")

    opts: dict[str, Any] = {}
    if raw.get("options") is not None:
        if not isinstance(raw["options"], dict):
            raise ProactiveError("monitor options must be a mapping")
        opts.update(raw["options"])
    for k in (
        "url", "status", "max_latency_ms", "host", "port",
        "repo", "ref", "metric", "direction", "symbol", "field",
        "path", "pattern", "case_sensitive", "keywords", "source_ids",
    ):
        if k in raw and raw[k] is not None:
            opts[k] = raw[k]

    default_threshold = 90.0 if kind in {"disk", "cpu", "memory", "system"} else 1.0
    threshold = raw.get("threshold", opts.get("threshold", default_threshold))
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
        raise ProactiveError(f"Monitor {ident} threshold must be a finite number")
    threshold_f = float(threshold)

    if kind == "website":
        opts["url"] = _validate_url(opts.get("url"), https_only=False)
        status = opts.get("status", 200)
        if type(status) is not int or not 100 <= status <= 599:
            raise ProactiveError("Website monitor status must be 100..599")
        opts["status"] = status
        if opts.get("max_latency_ms") is not None:
            ml = opts["max_latency_ms"]
            if isinstance(ml, bool) or not isinstance(ml, (int, float)) or not 1 <= float(ml) <= 120_000:
                raise ProactiveError("Website max_latency_ms must be 1..120000")
    elif kind == "server":
        if opts.get("url"):
            opts["url"] = _validate_url(opts["url"], https_only=False)
        elif opts.get("host"):
            host = str(opts["host"]).strip()
            port = opts.get("port")
            if not host or len(host) > 255 or type(port) is not int or not 1 <= port <= 65535:
                raise ProactiveError("Server monitor host/port invalid")
            opts["host"] = host
        else:
            raise ProactiveError("Server monitor requires url or host+port")
    elif kind == "github_ci":
        repo = opts.get("repo")
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ProactiveError("github_ci monitor requires repo 'owner/name'")
        ref = str(opts.get("ref", "HEAD") or "HEAD")
        if not 1 <= len(ref) <= 180:
            raise ProactiveError("github_ci ref invalid")
        opts["ref"] = ref
    elif kind in {"disk", "cpu", "memory", "system"}:
        metric = opts.get("metric") or ("disk_used" if kind == "disk" else kind if kind in {"cpu", "memory"} else "cpu")
        if metric not in {"cpu", "memory", "disk_free", "disk_used"}:
            raise ProactiveError("System monitor metric must be cpu, memory, disk_free, or disk_used")
        if not 0 <= threshold_f <= 100:
            raise ProactiveError("System monitor threshold must be 0..100")
        opts["metric"] = metric
    elif kind in {"price", "stock", "market"}:
        if opts.get("url") is not None:
            opts["url"] = _validate_url(opts["url"], https_only=True)
        elif not opts.get("symbol"):
            raise ProactiveError(f"{kind} monitor requires url or symbol")
        direction = str(opts.get("direction", "above") or "above").lower()
        if direction not in {"above", "below", "change_pct"}:
            raise ProactiveError("Price/stock direction must be above, below, or change_pct")
        opts["direction"] = direction
        if opts.get("url") and not opts.get("field"):
            opts["field"] = "price"
    elif kind == "log_pattern":
        raw_path = opts.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ProactiveError("log_pattern monitor requires workspace-relative path")
        Permissions(workspace).path(raw_path)
        pat = opts.get("pattern")
        if not isinstance(pat, str) or not 1 <= len(pat) <= 240:
            raise ProactiveError("log_pattern monitor requires pattern (1..240 chars)")
        try:
            re.compile(pat)
        except re.error as exc:
            raise ProactiveError(f"Invalid log_pattern regex: {exc}") from exc
        if threshold_f < 1:
            threshold_f = 1.0
    elif kind == "rss_keyword":
        kw_raw = opts.get("keywords")
        if not isinstance(kw_raw, list) or not kw_raw or len(kw_raw) > 30:
            raise ProactiveError("rss_keyword monitor requires 1..30 keywords")
        keywords = []
        for k in kw_raw:
            if not isinstance(k, str) or not 1 <= len(k.strip()) <= 80:
                raise ProactiveError("Each rss_keyword must be 1..80 chars")
            keywords.append(k.strip())
        opts["keywords"] = keywords
        if opts.get("source_ids") is not None:
            sids = opts["source_ids"]
            if not isinstance(sids, list) or any(not isinstance(s, str) or not s.strip() for s in sids):
                raise ProactiveError("rss_keyword source_ids must be a list of strings")
        if threshold_f < 1:
            threshold_f = 1.0

    return MonitorSpec(
        id=ident,
        kind=kind,
        enabled=enabled,
        threshold=threshold_f,
        cooldown_seconds=float(cooldown),
        poll_seconds=float(poll_s),
        severity=severity,
        notify_on_recovery=notify_rec,
        options=opts,
    )


def _parse_monitors(raw: Any, workspace: Path) -> MonitorsPolicy:
    if raw is None:
        return MonitorsPolicy()
    if not isinstance(raw, dict):
        raise ProactiveError("proactive.yaml monitors must be a mapping")
    unknown = set(raw) - {"enabled", "items"}
    if unknown:
        raise ProactiveError(f"Unknown monitors key(s): {', '.join(sorted(unknown))}")
    enabled = _require_bool(raw.get("enabled", False), "monitors.enabled")
    items_raw = raw.get("items", [])
    if not isinstance(items_raw, list) or len(items_raw) > MAX_MONITORS:
        raise ProactiveError(f"monitors.items must be a list of at most {MAX_MONITORS} entries")
    seen: set[str] = set()
    items = tuple(_parse_monitor_item(entry, workspace, seen) for entry in items_raw)
    return MonitorsPolicy(enabled=enabled, items=items)


def _parse_suggestions(raw: Any) -> SuggestionsPolicy:
    if raw is None:
        return SuggestionsPolicy()
    if not isinstance(raw, dict):
        raise ProactiveError("proactive.yaml suggestions must be a mapping")
    unknown = set(raw) - {"enabled", "max_speech_per_hour", "meeting_soon_minutes", "deadline_hours", "cooldown_seconds"}
    if unknown:
        raise ProactiveError(f"Unknown suggestions key(s): {', '.join(sorted(unknown))}")
    enabled = _require_bool(raw.get("enabled", False), "suggestions.enabled")
    max_speech = raw.get("max_speech_per_hour", 3)
    if type(max_speech) is not int or not 0 <= max_speech <= 60:
        raise ProactiveError("suggestions.max_speech_per_hour must be an integer 0..60")
    meeting_min = raw.get("meeting_soon_minutes", 30)
    if type(meeting_min) is not int or not 1 <= meeting_min <= 240:
        raise ProactiveError("suggestions.meeting_soon_minutes must be an integer 1..240")
    deadline_h = raw.get("deadline_hours", 24)
    if type(deadline_h) is not int or not 1 <= deadline_h <= 168:
        raise ProactiveError("suggestions.deadline_hours must be an integer 1..168")
    cooldown = raw.get("cooldown_seconds", 900.0)
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or not math.isfinite(cooldown) or not 0 <= float(cooldown) <= 86400:
        raise ProactiveError("suggestions.cooldown_seconds must be 0..86400")
    return SuggestionsPolicy(
        enabled=enabled,
        max_speech_per_hour=max_speech,
        meeting_soon_minutes=meeting_min,
        deadline_hours=deadline_h,
        cooldown_seconds=float(cooldown),
    )


def _parse_dnd(raw: Any) -> DNDPolicy:
    if raw is None:
        return DNDPolicy()
    if not isinstance(raw, dict):
        raise ProactiveError("proactive.yaml dnd must be a mapping")
    unknown = set(raw) - {"enabled", "schedule", "focus_default_minutes", "urgent_override"}
    if unknown:
        raise ProactiveError(f"Unknown dnd key(s): {', '.join(sorted(unknown))}")
    enabled = _require_bool(raw.get("enabled", False), "dnd.enabled")
    focus_min = raw.get("focus_default_minutes", 60)
    if type(focus_min) is not int or not 1 <= focus_min <= 1440:
        raise ProactiveError("dnd.focus_default_minutes must be an integer 1..1440")

    sched_raw = raw.get("schedule") or {}
    if not isinstance(sched_raw, dict):
        raise ProactiveError("dnd.schedule must be a mapping")
    s_unknown = set(sched_raw) - {"enabled", "start", "end", "days"}
    if s_unknown:
        raise ProactiveError(f"Unknown dnd.schedule key(s): {', '.join(sorted(s_unknown))}")
    days_raw = sched_raw.get("days", [])
    if not isinstance(days_raw, list) or len(days_raw) > 7:
        raise ProactiveError("dnd.schedule.days must be a list of weekday names")
    days: list[int] = []
    for d in days_raw:
        if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6:
            days.append(d)
        elif isinstance(d, str) and d.strip().casefold() in _WEEKDAY_ALIASES:
            days.append(_WEEKDAY_ALIASES[d.strip().casefold()])
        else:
            raise ProactiveError(f"Invalid dnd.schedule day {d!r}")
    sched = DNDSchedulePolicy(
        enabled=_require_bool(sched_raw.get("enabled", False), "dnd.schedule.enabled"),
        start=_require_hhmm(sched_raw.get("start", "22:00"), "dnd.schedule.start"),
        end=_require_hhmm(sched_raw.get("end", "07:00"), "dnd.schedule.end"),
        days=tuple(dict.fromkeys(days)),
    )

    ov_raw = raw.get("urgent_override") or {}
    if not isinstance(ov_raw, dict):
        raise ProactiveError("dnd.urgent_override must be a mapping")
    o_unknown = set(ov_raw) - {"enabled", "min_severity", "allowed_sources"}
    if o_unknown:
        raise ProactiveError(f"Unknown dnd.urgent_override key(s): {', '.join(sorted(o_unknown))}")
    min_sev = str(ov_raw.get("min_severity", "critical") or "critical").lower()
    if min_sev not in {"high", "critical"}:
        raise ProactiveError("dnd.urgent_override.min_severity must be high or critical")
    sources_raw = ov_raw.get("allowed_sources", [])
    if not isinstance(sources_raw, list) or len(sources_raw) > 50 or any(not isinstance(s, str) or not s.strip() for s in sources_raw):
        raise ProactiveError("dnd.urgent_override.allowed_sources must be a list of strings")
    override = UrgentOverridePolicy(
        enabled=_require_bool(ov_raw.get("enabled", False), "dnd.urgent_override.enabled"),
        min_severity=min_sev,
        allowed_sources=tuple(s.strip() for s in sources_raw),
    )
    return DNDPolicy(enabled=enabled, schedule=sched, focus_default_minutes=focus_min, urgent_override=override)


def _parse_channels(raw: Any) -> ChannelsPolicy:
    if raw is None:
        return ChannelsPolicy()
    if not isinstance(raw, dict):
        raise ProactiveError("proactive.yaml channels must be a mapping")
    unknown = set(raw) - {"enabled", "away_fallback", "severity_routing"}
    if unknown:
        raise ProactiveError(f"Unknown channels key(s): {', '.join(sorted(unknown))}")
    enabled = _require_bool(raw.get("enabled", False), "channels.enabled")
    away_fallback = _require_bool(raw.get("away_fallback", True), "channels.away_fallback")
    routing = _default_severity_routing()
    raw_routes = raw.get("severity_routing")
    if raw_routes is not None:
        if not isinstance(raw_routes, dict):
            raise ProactiveError("channels.severity_routing must be a mapping")
        bad_sev = set(raw_routes) - set(SEVERITY_LEVELS)
        if bad_sev:
            raise ProactiveError(f"Unknown severity level(s) in severity_routing: {', '.join(sorted(bad_sev))}")
        for sev, chans in raw_routes.items():
            if not isinstance(chans, list) or any(not isinstance(c, str) or c not in VALID_CHANNELS for c in chans):
                raise ProactiveError(f"Invalid channels list for severity {sev!r}")
            routing[sev] = tuple(dict.fromkeys(chans))
    return ChannelsPolicy(enabled=enabled, away_fallback=away_fallback, severity_routing=routing)


def load_proactive_config(workspace: Path | str | None = None) -> ProactiveConfig:
    """Read and strictly validate operator-owned ``proactive.yaml``."""
    root = Path(workspace or Path.cwd()).resolve()
    path = root / "proactive.yaml"
    if not path.exists():
        return ProactiveConfig()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PROACTIVE_BYTES:
        raise ProactiveError("proactive.yaml must be a bounded regular operator-owned YAML file")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProactiveError(f"Malformed proactive.yaml ({type(exc).__name__})") from exc
    if raw is None:
        return ProactiveConfig()
    if not isinstance(raw, dict):
        raise ProactiveError("proactive.yaml top-level must be a mapping")
    unknown = set(raw) - PROACTIVE_ALLOWED_KEYS
    if unknown:
        raise ProactiveError(f"Unknown proactive.yaml key(s): {', '.join(sorted(unknown))}")
    _scan_no_inline_secrets(raw)

    schema = raw.get("schema", 1)
    if type(schema) is not int or schema != 1:
        raise ProactiveError("proactive.yaml schema must equal 1")
    enabled = _require_bool(raw.get("enabled", False), "enabled")
    tz = _require_tz(raw.get("timezone", ""), "timezone")
    return ProactiveConfig(
        schema=1,
        enabled=enabled,
        timezone=tz,
        morning_brief=_parse_morning_brief(raw.get("morning_brief")),
        voice_reminders=_parse_voice_reminders(raw.get("voice_reminders")),
        monitors=_parse_monitors(raw.get("monitors"), root),
        suggestions=_parse_suggestions(raw.get("suggestions")),
        dnd=_parse_dnd(raw.get("dnd")),
        channels=_parse_channels(raw.get("channels")),
    )


def effective_timezone(workspace: Path, cfg: ProactiveConfig | None = None) -> str:
    loaded = cfg or load_proactive_config(workspace)
    if loaded.timezone:
        return loaded.timezone
    return resolve_timezone(workspace)


# ── untrusted text sanitization ────────────────────────────────────────── #


def _clean_untrusted(workspace: Path, text: Any, limit: int = 240) -> tuple[str, list[str]]:
    raw = str(text or "")
    flags: list[str] = []
    try:
        flags = flag_names(raw[:16_000])
    except Exception:
        flags = ["scanner_unavailable"]
    collapsed = " ".join(raw.split())
    masked = mask(collapsed, known_secrets(workspace))
    escaped = re.sub(r"<(/?\s*(?:system|assistant|instruction)\b[^>]*)>", r"[\1]", escape_delimiters(masked), flags=re.I)
    safe = redact(escaped)
    return " ".join(str(safe).split())[:limit], flags


def _clean_safe(workspace: Path, text: Any, limit: int = 320) -> str:
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return ""
    masked = mask(collapsed, known_secrets(workspace))
    redacted = redact(masked)
    try:
        from ..voice.tts import sanitize_tts_text

        redacted = sanitize_tts_text(redacted, workspace=workspace)
    except Exception:
        pass
    return " ".join(str(redacted).split())[:limit]


# ── durable workspace state store ──────────────────────────────────────── #


def _default_state() -> dict[str, Any]:
    return {
        "schema": 1,
        "brief_last_date": "",
        "last_brief": None,
        "monitor_states": {},
        "speech_timestamps": [],
        "suppressed_suggestions": [],
        "last_suggestion": None,
        "suggestion_last_proposed": {},
        "focus_mode": {
            "active": False,
            "until": None,
            "started_at": None,
            "reason": "",
        },
        "hud_toasts": [],
    }


class ProactiveStateStore:
    """Thread- and process-safe durable state under ``$NEXUS_HOME/proactive/``."""

    def __init__(self, workspace: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self.workspace = Path(workspace).resolve()
        self.clock = clock
        home = nexus_home().resolve()
        key = hashlib.sha256(str(self.workspace).encode("utf-8")).hexdigest()[:32]
        self.dir = (home / "proactive" / key).resolve()
        if not self.dir.is_relative_to(home):
            raise ProactiveError("Proactive state directory escaped NEXUS_HOME")
        self.path = self.dir / "state.json"
        self.lock_path = self.dir / "state.lock"
        self._thread_lock = threading.RLock()

    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise ProactiveError("Unsafe proactive state file path")

    def _read_unlocked(self) -> dict[str, Any]:
        base = _default_state()
        if not self.path.exists():
            return base
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return base
        if not isinstance(raw, dict):
            return base
        for key in base:
            if key in raw and raw[key] is not None:
                base[key] = raw[key]
        return base

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self._ensure_dir()
        data["schema"] = 1
        data["updated_at"] = float(self.clock())
        tmp = self.dir / f".state.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def read(self) -> dict[str, Any]:
        with self._thread_lock:
            self._ensure_dir()
            with FileLock(str(self.lock_path), timeout=5):
                return self._read_unlocked()

    def update(self, mutator: Callable[[dict[str, Any]], Any]) -> Any:
        with self._thread_lock:
            self._ensure_dir()
            with FileLock(str(self.lock_path), timeout=5):
                data = self._read_unlocked()
                result = mutator(data)
                self._write_unlocked(data)
                return result


# ── DND Manager (schedules, focus mode, urgent overrides) ──────────────── #


class DNDManager:
    """Evaluates recurring DND schedules, ``focus mode``, and urgent overrides."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        store: ProactiveStateStore | None = None,
        studio: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.clock = clock
        self.store = store or ProactiveStateStore(self.workspace, clock=clock)
        self.studio = studio

    def focus_status(self, *, now: float | None = None) -> dict[str, Any]:
        ts = float(self.clock() if now is None else now)

        def _check(data: dict[str, Any]) -> dict[str, Any]:
            focus = dict(data.get("focus_mode") or {})
            active = bool(focus.get("active", False))
            until = focus.get("until")
            if active and isinstance(until, (int, float)) and ts >= float(until):
                focus = {"active": False, "until": None, "started_at": None, "reason": "expired"}
                data["focus_mode"] = focus
                return dict(focus)
            return focus

        state = self.store.read()
        focus = dict(state.get("focus_mode") or {})
        until = focus.get("until")
        if focus.get("active") and isinstance(until, (int, float)) and ts >= float(until):
            focus = self.store.update(_check)
            if self.studio is not None:
                try:
                    from .inbox import Inbox

                    Inbox(self.studio).configure(quiet=False, minutes=0)
                except Exception:
                    pass
        remaining = max(0.0, float(focus["until"]) - ts) if focus.get("active") and isinstance(focus.get("until"), (int, float)) else None
        return {
            "active": bool(focus.get("active", False)),
            "until": focus.get("until"),
            "remaining_seconds": round(remaining, 1) if remaining is not None else None,
            "started_at": focus.get("started_at"),
            "reason": str(focus.get("reason") or ""),
        }

    def set_focus_mode(
        self,
        active: bool,
        *,
        minutes: int | None = None,
        reason: str = "manual",
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = float(self.clock() if now is None else now)
        cfg = load_proactive_config(self.workspace)
        dur = cfg.dnd.focus_default_minutes if minutes is None else int(minutes)
        if active and not 1 <= dur <= 1440:
            raise ProactiveError("Focus mode duration must be 1..1440 minutes")

        def _apply(data: dict[str, Any]) -> dict[str, Any]:
            if active:
                data["focus_mode"] = {
                    "active": True,
                    "until": ts + dur * 60.0 if minutes is not None or cfg.dnd.focus_default_minutes else None,
                    "started_at": ts,
                    "reason": str(reason or "focus_mode")[:80],
                }
            else:
                data["focus_mode"] = {
                    "active": False,
                    "until": None,
                    "started_at": None,
                    "reason": "disabled",
                }
            return dict(data["focus_mode"])

        updated = self.store.update(_apply)
        # Mirror into Jarvis Inbox DND when studio is attached (timed DND only, never sticky quiet).
        if self.studio is not None:
            try:
                from .inbox import Inbox

                inbox = Inbox(self.studio)
                if active:
                    inbox.configure(quiet=False, minutes=dur)
                else:
                    inbox.configure(quiet=False, minutes=0)
            except Exception:
                pass
        return self.focus_status(now=ts) if updated is not None else {"active": active}

    def schedule_active(
        self,
        *,
        cfg: ProactiveConfig | None = None,
        now: float | None = None,
    ) -> tuple[bool, str]:
        policy = cfg or load_proactive_config(self.workspace)
        if not policy.dnd.enabled or not policy.dnd.schedule.enabled:
            return False, ""
        ts = float(self.clock() if now is None else now)
        tz_name = effective_timezone(self.workspace, policy)
        dt = datetime.fromtimestamp(ts, tz=ZoneInfo(tz_name))
        sched = policy.dnd.schedule
        sh, sm = (int(p) for p in sched.start.split(":"))
        eh, em = (int(p) for p in sched.end.split(":"))
        cur_min = dt.hour * 60 + dt.minute
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        if start_min == end_min:
            return False, ""
        if start_min < end_min:
            in_window = start_min <= cur_min < end_min
            ref_weekday = dt.weekday()
        else:
            # Overnight window, e.g. 22:00 -> 07:00
            if cur_min >= start_min:
                in_window = True
                ref_weekday = dt.weekday()
            elif cur_min < end_min:
                in_window = True
                ref_weekday = (dt.weekday() - 1) % 7
            else:
                in_window = False
                ref_weekday = dt.weekday()
        if not in_window:
            return False, ""
        if sched.days and ref_weekday not in sched.days:
            return False, ""
        return True, f"dnd_schedule:{sched.start}-{sched.end}"

    def evaluate(
        self,
        severity: str = "medium",
        source: str = "proactive",
        *,
        now: float | None = None,
        explicit_user_request: bool = False,
        cfg: ProactiveConfig | None = None,
    ) -> dict[str, Any]:
        policy = cfg or load_proactive_config(self.workspace)
        sev = str(severity or "medium").lower()
        if sev not in SEVERITY_RANK:
            raise ProactiveError(f"Unknown severity {severity!r}")
        ts = float(self.clock() if now is None else now)

        if explicit_user_request:
            return {"suppressed": False, "overridden": False, "reason": "explicit_user_request"}

        focus = self.focus_status(now=ts)
        sched_on, sched_reason = self.schedule_active(cfg=policy, now=ts)

        inbox_quiet = False
        if self.studio is not None:
            try:
                from .inbox import Inbox

                delivery = Inbox(self.studio).delivery_policy()
                inbox_quiet = bool(delivery.get("quiet") or float(delivery.get("dnd_until") or 0) > ts)
            except Exception:
                pass

        active_reason = (
            "focus_mode" if focus["active"]
            else sched_reason if sched_on
            else "inbox_dnd" if inbox_quiet
            else ""
        )
        if not active_reason:
            return {"suppressed": False, "overridden": False, "reason": "clear"}

        override = policy.dnd.urgent_override
        if policy.dnd.enabled and override.enabled:
            if SEVERITY_RANK[sev] >= SEVERITY_RANK[override.min_severity]:
                if not override.allowed_sources or source in override.allowed_sources:
                    return {
                        "suppressed": False,
                        "overridden": True,
                        "reason": f"urgent_override:{active_reason}",
                    }
        return {"suppressed": True, "overridden": False, "reason": active_reason}


# ── Severity-Routed Channel Dispatcher (HUD toast, voice, Telegram, OS) ── #


class ProactiveChannelRouter:
    """Routes proactive alerts and briefs by severity across HUD toast, voice, Telegram, desktop."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        store: ProactiveStateStore | None = None,
        dnd: DNDManager | None = None,
        studio: Any = None,
        server_state: Any = None,
        voice_deliverer: Callable[[str], Any] | None = None,
        telegram_sender: Callable[[str, str], Any] | None = None,
        desktop_notifier: Callable[[str, str], Any] | None = None,
        hud_toast_sink: Callable[[dict[str, Any]], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.clock = clock
        self.store = store or ProactiveStateStore(self.workspace, clock=clock)
        self.studio = studio
        self.server_state = server_state
        self.dnd = dnd or DNDManager(self.workspace, store=self.store, studio=studio, clock=clock)
        self.voice_deliverer = voice_deliverer
        self.telegram_sender = telegram_sender
        self.desktop_notifier = desktop_notifier
        self.hud_toast_sink = hud_toast_sink

    def proactive_speech_count_last_hour(self, *, now: float | None = None) -> int:
        ts = float(self.clock() if now is None else now)
        stamps = self.store.read().get("speech_timestamps") or []
        return sum(1 for t in stamps if isinstance(t, (int, float)) and 0 <= ts - float(t) < 3600.0)

    def _try_consume_speech_budget(self, max_per_hour: int, *, now: float) -> bool:
        if max_per_hour <= 0:
            return False
        consumed = [False]

        def _mut(data: dict[str, Any]) -> None:
            stamps = [float(t) for t in (data.get("speech_timestamps") or []) if isinstance(t, (int, float)) and 0 <= now - float(t) < 86400.0]
            recent = [t for t in stamps if now - t < 3600.0]
            if len(recent) >= max_per_hour:
                data["speech_timestamps"] = stamps
                consumed[0] = False
                return
            stamps.append(now)
            data["speech_timestamps"] = stamps[-500:]
            consumed[0] = True

        self.store.update(_mut)
        return consumed[0]

    def _emit_hud_toast(
        self,
        *,
        title: str,
        message: str,
        severity: str,
        source: str,
        suppressed: bool,
        now: float,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        toast_id = "toast-" + uuid.uuid4().hex[:12]
        entry = {
            "id": toast_id,
            "title": title,
            "message": message,
            "severity": severity,
            "source": source,
            "created_at": now,
            "read": False,
            "suppressed": suppressed,
            "data": dict(extra or {}),
        }

        def _add(data: dict[str, Any]) -> None:
            rows = list(data.get("hud_toasts") or [])
            rows.insert(0, entry)
            data["hud_toasts"] = rows[:MAX_HUD_TOASTS]

        self.store.update(_add)
        if self.hud_toast_sink is not None:
            try:
                self.hud_toast_sink(entry)
            except Exception:
                pass
        if self.server_state is not None:
            try:
                mission = getattr(self.server_state, "mission", None)
                if mission is not None:
                    mission_sev = "high" if severity in {"high", "critical"} else "warn" if severity == "medium" else "info"
                    mission.publish(
                        "alert.new",
                        {
                            "id": toast_id,
                            "title": title,
                            "detail": message[:280],
                            "severity": mission_sev,
                            "source": f"proactive:{source}",
                            "observed_at": now,
                        },
                    )
            except Exception:
                pass
            try:
                from .. import channels

                channels.record(self.server_state, f"proactive:{source}", state_name="suppressed" if suppressed else "pending", title=title, detail=message[:500])
            except Exception:
                pass
        elif self.studio is not None:
            try:
                self.studio.put(
                    "jarvis-notice",
                    {
                        "job_id": toast_id,
                        "state": f"proactive:{severity}",
                        "summary": f"{title}: {message}"[:500],
                        "created": now,
                        "read": False,
                        "suppressed": suppressed,
                    },
                )
            except Exception:
                pass
        return entry

    def _send_voice(self, text: str) -> tuple[bool, str]:
        if self.voice_deliverer is not None:
            self.voice_deliverer(text)
            return True, "voice_deliverer"
        try:
            from ..voice.audio_io import AudioOutputPlayer
            from ..voice.config import load_voice_config
            from ..voice.tts import create_tts

            vcfg = load_voice_config(self.workspace)
            if not vcfg.enabled:
                return False, "voice disabled in voice.yaml"
            clip = create_tts(vcfg, workspace=self.workspace).synthesize(text)
            AudioOutputPlayer(device=vcfg.output_device).play(clip)
            return True, f"tts:{vcfg.tts.provider}"
        except Exception as exc:
            return False, f"voice unavailable: {type(exc).__name__}"

    def _send_telegram(self, title: str, text: str, *, severity: str, now: float) -> tuple[bool, str]:
        msg = f"[{title}] {text}" if title else text
        if self.telegram_sender is not None:
            ok = bool(self.telegram_sender(title, text))
            return ok, "telegram_sender" if ok else "telegram_sender refused"
        try:
            from ..notifications import _in_quiet_hours
            from ..server import ServerState
            from ..telegram.bot import Bot
            from ..telegram.bot import settings as telegram_settings

            tcfg = telegram_settings(self.workspace)
            if not tcfg.get("enabled"):
                return False, "telegram disabled in telegram.yaml"
            if severity != "critical" and _in_quiet_hours(tcfg.get("quiet_utc_hours") or [], now):
                return False, "telegram quiet_utc_hours active"
            studio = self.studio or ServerState(self.workspace, quiet=True).workbench.studio
            bot = Bot(studio)
            try:
                safe = bot.clean(msg)
                for uid in tcfg["user_ids"]:
                    bot.api("sendMessage", {"chat_id": uid, "text": safe})
                return True, "telegram"
            finally:
                bot.close()
        except Exception as exc:
            return False, f"telegram unavailable: {type(exc).__name__}"

    def _send_desktop(self, title: str, text: str) -> tuple[bool, str]:
        if self.desktop_notifier is not None:
            res = self.desktop_notifier(title, text)
            ok = bool(res if isinstance(res, bool) else (res or {}).get("submitted", True))
            return ok, "desktop_notifier" if ok else "desktop_notifier refused"
        try:
            from .notifications import submit_text

            submit_text(title or "Nexus", text)
            return True, "desktop"
        except Exception as exc:
            return False, f"desktop unavailable: {type(exc).__name__}"

    def dispatch(
        self,
        *,
        title: str,
        message: str,
        spoken_text: str | None = None,
        hud_text: str | None = None,
        severity: str = "medium",
        source: str = "proactive",
        present: bool | None = None,
        explicit_user_request: bool = False,
        rate_limit_speech: bool = False,
        channels_override: list[str] | tuple[str, ...] | None = None,
        extra_hud_data: dict[str, Any] | None = None,
        now: float | None = None,
        cfg: ProactiveConfig | None = None,
    ) -> dict[str, Any]:
        policy = cfg or load_proactive_config(self.workspace)
        sev = str(severity or "medium").lower()
        if sev not in SEVERITY_RANK:
            raise ProactiveError(f"Unknown severity {severity!r}")
        ts = float(self.clock() if now is None else now)

        safe_title = _clean_safe(self.workspace, title, limit=96) or "Nexus"
        safe_msg = _clean_safe(self.workspace, message, limit=500)
        safe_spoken = _clean_safe(self.workspace, spoken_text or message, limit=400)
        safe_hud = _clean_safe(self.workspace, hud_text or message, limit=8000)

        dnd_decision = self.dnd.evaluate(
            sev,
            source,
            now=ts,
            explicit_user_request=explicit_user_request,
            cfg=policy,
        )
        suppressed = bool(dnd_decision["suppressed"])

        # Always retain HUD toast locally (marked suppressed if DND muted live alerts).
        toast = self._emit_hud_toast(
            title=safe_title,
            message=safe_hud,
            severity=sev,
            source=source,
            suppressed=suppressed,
            now=ts,
            extra=extra_hud_data,
        )

        delivered_channels: list[str] = ["hud_toast"]
        skipped: dict[str, str] = {}

        if suppressed:
            return {
                "toast_id": toast["id"],
                "severity": sev,
                "source": source,
                "suppressed": True,
                "dnd_reason": dnd_decision["reason"],
                "overridden": False,
                "channels": delivered_channels,
                "skipped": {"all": dnd_decision["reason"]},
                "speech_rate_limited": False,
            }

        user_present = bool(present) if present is not None else bool(is_user_present(self.workspace, now=ts))

        if channels_override is not None:
            targets = list(channels_override)
        elif policy.channels.enabled:
            targets = list(policy.channels.severity_routing.get(sev, ("hud_toast",)))
        else:
            # Channels section off -> local HUD toast only unless caller explicitly routed a brief.
            targets = ["hud_toast"]

        # Expand away fallback when voice is targeted while user is away.
        expanded_targets: list[str] = []
        for ch in targets:
            if ch not in VALID_CHANNELS:
                continue
            if ch == "voice" and not user_present and policy.channels.away_fallback:
                for fallback_ch in ("telegram", "desktop"):
                    if fallback_ch not in expanded_targets:
                        expanded_targets.append(fallback_ch)
            elif ch not in expanded_targets:
                expanded_targets.append(ch)

        speech_rate_limited = False
        for ch in expanded_targets:
            if ch == "hud_toast":
                continue
            if ch == "voice":
                if not user_present and not explicit_user_request:
                    skipped["voice"] = "user_away"
                    continue
                if rate_limit_speech and not self._try_consume_speech_budget(policy.suggestions.max_speech_per_hour, now=ts):
                    speech_rate_limited = True
                    skipped["voice"] = "rate_limited"
                    continue
                ok, reason = self._send_voice(safe_spoken)
                if ok:
                    delivered_channels.append("voice")
                else:
                    skipped["voice"] = reason
            elif ch == "telegram":
                ok, reason = self._send_telegram(safe_title, safe_msg, severity=sev, now=ts)
                if ok:
                    delivered_channels.append("telegram")
                else:
                    skipped["telegram"] = reason
            elif ch == "desktop":
                ok, reason = self._send_desktop(safe_title, safe_msg)
                if ok:
                    delivered_channels.append("desktop")
                else:
                    skipped["desktop"] = reason

        try:
            auditlog.record(
                self.workspace,
                "proactive.notify",
                who="nexus",
                target=",".join(delivered_channels),
                result="suppressed" if suppressed else "delivered",
                risk="low",
                detail={"severity": sev, "source": source, "overridden": bool(dnd_decision["overridden"])},
            )
        except OSError:
            pass

        return {
            "toast_id": toast["id"],
            "severity": sev,
            "source": source,
            "suppressed": False,
            "dnd_reason": dnd_decision["reason"],
            "overridden": bool(dnd_decision["overridden"]),
            "channels": delivered_channels,
            "skipped": skipped,
            "speech_rate_limited": speech_rate_limited,
            "present": user_present,
        }


# ── Voice reminder & recurring task NL parsing ─────────────────────────── #


_VOICE_GREETING_RE = re.compile(
    r"^\s*(?:(?:hey\s+nexus|nexus)[,\s]+)?"
    r"(?:good\s+morning(?:\s+nexus)?|"
    r"(?:give\s+me\s+|what(?:'s|\s+is)\s+(?:my\s+)?)?morning\s+brief(?:ing)?|"
    r"শুভ\s*সকাল|সকালের\s*ব্রিফ(?:িং)?)\s*[.!?।]?\s*$",
    re.I,
)

_VOICE_FOCUS_ON_RE = re.compile(
    r"^\s*(?:(?:hey\s+nexus|nexus)[,\s]+)?"
    r"(?:turn\s+on\s+|enable\s+|start\s+|enter\s+)?"
    r"(?:focus\s+mode|do\s+not\s+disturb|dnd|ফোকাস\s*মোড)"
    r"(?:\s+(?:on|for\s+(?P<mins>\d+)\s*(?:min|minute|minutes|m|মিনিট)|(?P<hours>\d+)\s*(?:hr|hour|hours|ঘণ্টা)))?\s*[.!?।]?\s*$",
    re.I,
)

_VOICE_FOCUS_OFF_RE = re.compile(
    r"^\s*(?:(?:hey\s+nexus|nexus)[,\s]+)?"
    r"(?:(?:turn\s+off|disable|stop|exit|end|leave)\s+(?:focus\s+mode|do\s+not\s+disturb|dnd)|"
    r"(?:focus\s+mode|do\s+not\s+disturb|dnd)\s+off|"
    r"ফোকাস\s*মোড\s*বন্ধ(?:\s*করো)?)\s*[.!?।]?\s*$",
    re.I,
)

_VOICE_STOP_SUGGEST_RE = re.compile(
    r"^\s*(?:(?:hey\s+nexus|nexus)[,\s]+)?"
    r"(?:stop\s+suggesting(?:\s+(?:this|that|it|about))?(?:\s+(?P<topic>.+?))?|"
    r"don'?t\s+suggest(?:\s+(?:this|that|it|about))?(?:\s+(?P<topic2>.+?))?|"
    r"never\s+suggest(?:\s+(?:this|that|it|about))?(?:\s+(?P<topic3>.+?))?|"
    r"এটা\s*আর\s*সাজেস্ট\s*করো\s*না)\s*[.!?।]?\s*$",
    re.I,
)

_VOICE_REMIND_LEAD_RE = re.compile(
    r"^\s*(?:(?:hey\s+nexus|nexus)[,\s]+)?"
    r"(?:please\s+)?(?:remind\s+me|set\s+(?:a\s+)?reminder(?:\s+for\s+me)?)\s+(?P<body>.+?)\s*[.!?।]?\s*$",
    re.I,
)

_VOICE_RECURRING_TASK_RE = re.compile(
    r"^\s*(?:(?:hey\s+nexus|nexus)[,\s]+)?"
    r"(?:please\s+)?(?:schedule(?:\s+a)?(?:\s+recurring)?\s+task(?:\s+to)?|"
    r"create\s+(?:a\s+)?recurring\s+task(?:\s+to)?)\s+(?P<body>.+?)\s*[.!?।]?\s*$",
    re.I,
)

_BN_REMIND_RE = re.compile(
    r"^\s*(?:(?:হে\s+নেক্সাস|নেক্সাস)[,\s]+)?(?:আমাকে\s+)?(?P<body>.+?)\s*মনে\s*করিয়ে\s*(?:দিও|দাও|দিয়ো)\s*[.!?।]?\s*$"
)


def detect_proactive_voice_intent(text: str) -> tuple[str, dict[str, Any]] | None:
    """Classify a spoken/text utterance into a proactive intent without executing anything."""
    raw = " ".join((text or "").split())
    if not raw:
        return None
    norm = normalize_bangla_digits(raw)

    if _VOICE_GREETING_RE.match(norm):
        return "morning_brief", {}

    m_off = _VOICE_FOCUS_OFF_RE.match(norm)
    if m_off:
        return "focus_mode", {"active": False}

    m_on = _VOICE_FOCUS_ON_RE.match(norm)
    if m_on:
        minutes = None
        if m_on.group("mins"):
            minutes = int(m_on.group("mins"))
        elif m_on.group("hours"):
            minutes = int(m_on.group("hours")) * 60
        return "focus_mode", {"active": True, "minutes": minutes}

    m_stop = _VOICE_STOP_SUGGEST_RE.match(norm)
    if m_stop:
        topic = (m_stop.group("topic") or m_stop.group("topic2") or m_stop.group("topic3") or "").strip()
        return "stop_suggesting", {"topic": topic}

    if _VOICE_REMIND_LEAD_RE.match(norm) or _BN_REMIND_RE.match(norm):
        return "remind", {"text": raw}

    if _VOICE_RECURRING_TASK_RE.match(norm):
        return "recurring_task", {"text": raw}

    return None


@dataclass(frozen=True)
class ParsedVoiceReminder:
    kind: str  # once | interval | daily | weekly
    task: str
    is_task: bool
    timezone: str
    run_at: float | None = None
    every_minutes: int | None = None
    daily: str = ""
    weekly: str = ""
    cron_expr: str = ""
    summary: str = ""


def _resolve_next_clock_today_or_tomorrow(
    now_dt: datetime,
    hour: int,
    minute: int,
    *,
    explicit_ampm: bool,
) -> datetime:
    candidate = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > now_dt:
        return candidate
    if not explicit_ampm and 1 <= hour <= 11:
        pm_candidate = now_dt.replace(hour=hour + 12, minute=minute, second=0, microsecond=0)
        if pm_candidate > now_dt:
            return pm_candidate
    return candidate + timedelta(days=1)


def parse_voice_reminder(
    utterance: str,
    *,
    timezone: str = "UTC",
    now: float | None = None,
    as_task: bool = False,
) -> ParsedVoiceReminder:
    """Parse natural-language voice reminders and recurring tasks (English & Bangla)."""
    tz = ZoneInfo(timezone or "UTC")
    now_ts = time.time() if now is None else float(now)
    now_dt = datetime.fromtimestamp(now_ts, tz=tz)

    raw = " ".join((utterance or "").split())
    norm = normalize_bangla_digits(raw)
    is_task = as_task

    m_rem = _VOICE_REMIND_LEAD_RE.match(norm)
    m_tsk = _VOICE_RECURRING_TASK_RE.match(norm)
    m_bn = _BN_REMIND_RE.match(norm)
    if m_rem:
        body = m_rem.group("body").strip()
    elif m_tsk:
        body = m_tsk.group("body").strip()
        is_task = True
    elif m_bn:
        body = m_bn.group("body").strip()
    else:
        body = norm

    if not body:
        raise ProactiveError("Reminder content cannot be empty")

    # 1. Interval: "every N minutes/hours ..."
    m_interval = re.search(
        r"\bevery\s+(?:(?P<n>\d+)\s+)?(?P<unit>minute|minutes|min|hour|hours|hr|hrs)\b"
        r"|প্রতি\s+(?:(?P<bn_n>\d+)\s+)?(?P<bn_unit>মিনিট|মিনিটে|ঘণ্টা|ঘন্টা|ঘণ্টায়)(?:\s*পর)?",
        body,
        re.I,
    )
    if m_interval:
        count = int(m_interval.group("n") or m_interval.group("bn_n") or 1)
        unit = (m_interval.group("unit") or m_interval.group("bn_unit") or "").casefold()
        minutes = count if (unit.startswith(("min",)) or "মিনিট" in unit) else count * 60
        if not 1 <= minutes <= 10_080:
            raise ProactiveError("Recurring interval must be 1..10080 minutes")
        remainder = (body[: m_interval.start()] + " " + body[m_interval.end() :]).strip()
        cleaned_task = _clean_task_phrase(remainder)
        if not cleaned_task:
            raise ProactiveError("Reminder task cannot be empty")
        return ParsedVoiceReminder(
            kind="interval",
            task=cleaned_task,
            is_task=is_task,
            timezone=timezone,
            every_minutes=minutes,
            summary=f"every {minutes}m: {cleaned_task}",
        )

    # 2. Relative delay: "in N minutes/hours/days ..."
    m_rel = re.search(
        r"\bin\s+(?P<n>\d+)\s+(?P<unit>minute|minutes|min|hour|hours|hr|hrs|day|days)\b"
        r"|(?P<bn_n>\d+)\s*(?P<bn_unit>মিনিট|ঘণ্টা|ঘন্টা|দিন)\s*(?:পর|পরে)",
        body,
        re.I,
    )
    if m_rel:
        count = int(m_rel.group("n") or m_rel.group("bn_n"))
        unit = (m_rel.group("unit") or m_rel.group("bn_unit") or "").casefold()
        if count <= 0:
            raise ProactiveError("Relative reminder delay must be positive")
        if unit.startswith(("min",)) or "মিনিট" in unit:
            delta = timedelta(minutes=count)
        elif unit.startswith(("hour", "hr")) or "ঘণ্ট" in unit or "ঘন্ট" in unit:
            delta = timedelta(hours=count)
        else:
            delta = timedelta(days=count)
        remainder = (body[: m_rel.start()] + " " + body[m_rel.end() :]).strip()
        cleaned_task = _clean_task_phrase(remainder)
        if not cleaned_task:
            raise ProactiveError("Reminder task cannot be empty")
        target = now_dt + delta
        return ParsedVoiceReminder(
            kind="once",
            task=cleaned_task,
            is_task=is_task,
            timezone=timezone,
            run_at=target.timestamp(),
            summary=f"at {target.strftime('%H:%M')} ({timezone}): {cleaned_task}",
        )

    # 3. Weekly recurring: "every Monday ..."
    wd_alt = "|".join(sorted((re.escape(k) for k in _WEEKDAY_ALIASES if len(k) > 3), key=len, reverse=True))
    m_weekly = re.search(rf"\bevery\s+(?P<wd>{wd_alt})\b", body, re.I)
    if m_weekly:
        py_wd = _WEEKDAY_ALIASES[m_weekly.group("wd").casefold()]
        remainder = (body[: m_weekly.start()] + " " + body[m_weekly.end() :]).strip()
        hour, minute, remainder2 = _extract_time_of_day(remainder, default_hour=9, default_minute=0)
        cleaned_task = _clean_task_phrase(remainder2)
        if not cleaned_task:
            raise ProactiveError("Weekly reminder task cannot be empty")
        cron_wd = (py_wd + 1) % 7
        return ParsedVoiceReminder(
            kind="weekly",
            task=cleaned_task,
            is_task=is_task,
            timezone=timezone,
            weekly=f"{_DAY_INDEX_TO_REMINDER[py_wd]}:{hour:02d}:{minute:02d}",
            cron_expr=f"{minute} {hour} * * {cron_wd}",
            summary=f"weekly {_DAY_INDEX_TO_REMINDER[py_wd]} {hour:02d}:{minute:02d}: {cleaned_task}",
        )

    # 4. Daily recurring: "every day ...", "daily ...", "every morning/evening ...", "প্রতিদিন ..."
    m_daily = re.search(
        r"\b(?:every\s+day|daily|every\s+(?=morning|afternoon|evening|night))\b|প্রতি\s*দিন|রোজ",
        body,
        re.I,
    )
    if m_daily:
        remainder = (body[: m_daily.start()] + " " + body[m_daily.end() :]).strip()
        hour, minute, remainder2 = _extract_time_of_day(remainder, default_hour=9, default_minute=0)
        cleaned_task = _clean_task_phrase(remainder2)
        if not cleaned_task:
            raise ProactiveError("Daily reminder task cannot be empty")
        return ParsedVoiceReminder(
            kind="daily",
            task=cleaned_task,
            is_task=is_task,
            timezone=timezone,
            daily=f"{hour:02d}:{minute:02d}",
            cron_expr=f"{minute} {hour} * * *",
            summary=f"daily at {hour:02d}:{minute:02d}: {cleaned_task}",
        )

    # 5. Tomorrow / Today with optional clock time
    m_day = re.search(
        r"\b(?P<day>tomorrow|today)\b|(?:^|(?<=\s))(?P<bn_day>আগামীকাল|কাল|আজ)(?:কে|ের)?(?=$|\s)",
        body,
        re.I,
    )
    if m_day:
        day_tok = (m_day.group("day") or m_day.group("bn_day") or "").casefold()
        is_tomorrow = day_tok in {"tomorrow", "আগামীকাল", "কাল"}
        remainder = (body[: m_day.start()] + " " + body[m_day.end() :]).strip()
        explicit_ampm = bool(re.search(r"\b(?:am|pm)\b|সকাল|দুপুর|বিকাল|বিকেল|সন্ধ্যা|রাত", remainder, re.I))
        hour, minute, remainder2 = _extract_time_of_day(remainder, default_hour=9, default_minute=0)
        cleaned_task = _clean_task_phrase(remainder2)
        if not cleaned_task:
            raise ProactiveError("Reminder task cannot be empty")
        base_day = now_dt + timedelta(days=1 if is_tomorrow else 0)
        target_dt = base_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not is_tomorrow and target_dt <= now_dt and not explicit_ampm and 1 <= hour <= 11:
            alt = base_day.replace(hour=hour + 12, minute=minute, second=0, microsecond=0)
            if alt > now_dt:
                target_dt = alt
        if target_dt <= now_dt:
            raise ProactiveError("Scheduled time today has already passed")
        return ParsedVoiceReminder(
            kind="once",
            task=cleaned_task,
            is_task=is_task,
            timezone=timezone,
            run_at=target_dt.timestamp(),
            summary=f"at {target_dt.strftime('%Y-%m-%d %H:%M')} ({timezone}): {cleaned_task}",
        )

    # 6. Clock time anywhere in phrase: e.g. "at 5 to call mom" or "to call mom at 5pm"
    explicit_ampm = bool(re.search(r"\b(?:am|pm)\b|ভোর|সকাল|দুপুর|বিকাল|বিকেল|সন্ধ্যা|রাত", body, re.I))
    has_clock = bool(
        re.search(r"\b(?:at|@)\s*\d{1,2}(?::\d{2})?(?:\s*(?:am|pm))?\b", body, re.I)
        or re.search(r"\d{1,2}\s*(?:টা|টায়|বাজে)", body)
    )
    if has_clock:
        hour, minute, remainder = _extract_time_of_day(body, default_hour=9, default_minute=0)
        # Re-check 12-hour ambiguity if "at 5" has no AM/PM and time already passed in AM
        m_plain_at = re.search(r"\b(?:at|@)\s*(\d{1,2})(?::(\d{2}))?\b", body, re.I)
        if m_plain_at and not explicit_ampm:
            raw_h = int(m_plain_at.group(1))
            if 1 <= raw_h <= 11:
                hour = _adjust_hour(raw_h, None, None)
        cleaned_task = _clean_task_phrase(remainder)
        if not cleaned_task:
            raise ProactiveError("Reminder task cannot be empty")
        target_dt = _resolve_next_clock_today_or_tomorrow(now_dt, hour, minute, explicit_ampm=explicit_ampm)
        return ParsedVoiceReminder(
            kind="once",
            task=cleaned_task,
            is_task=is_task,
            timezone=timezone,
            run_at=target_dt.timestamp(),
            summary=f"at {target_dt.strftime('%Y-%m-%d %H:%M')} ({timezone}): {cleaned_task}",
        )

    raise ProactiveError(f"Could not understand reminder timing in {utterance!r}")


# ── Morning Brief Mission Engine ───────────────────────────────────────── #


class MorningBriefEngine:
    """Gathers 6 morning brief sources and formats short spoken + full HUD output."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        studio: Any = None,
        world: Any = None,
        weather_source: Callable[[], dict[str, Any]] | None = None,
        calendar_source: Callable[[str, str], list[dict[str, Any]]] | None = None,
        email_source: Callable[[], list[dict[str, Any]]] | None = None,
        news_source: Callable[[], dict[str, Any] | list[dict[str, Any]]] | None = None,
        system_sampler: Callable[[], dict[str, Any]] | None = None,
        ci_source: Callable[[str, str], tuple[dict[str, Any], bool]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.studio = studio
        self.world = world
        self.weather_source = weather_source
        self.calendar_source = calendar_source
        self.email_source = email_source
        self.news_source = news_source
        self.system_sampler = system_sampler
        self.ci_source = ci_source
        self.clock = clock

    def _gather_weather(self) -> dict[str, Any]:
        try:
            if self.weather_source is not None:
                payload = self.weather_source()
            elif self.world is not None:
                payload = self.world.weather()
            else:
                from ..hud.world.core import World

                w = World(self.workspace, clock=self.clock)
                if not w.config.get("enabled", True):
                    return {"state": "disabled", "reason": "World widget disabled", "data": None}
                payload = w.weather()
            if not isinstance(payload, dict):
                return {"state": "unavailable", "reason": "Weather unavailable", "data": None}
            if payload.get("status") in {"configuration-required", "disabled"} and not payload.get("data"):
                return {"state": "disabled", "reason": str(payload.get("status")), "data": None}
            data = payload.get("data") if "current" not in payload else payload
            if not isinstance(data, dict) or not isinstance(data.get("current"), dict):
                return {"state": "unavailable", "reason": str(payload.get("status") or "no weather data"), "data": None}
            cur = data["current"]
            daily = data.get("daily") or {}
            wcode = cur.get("weather_code")
            cond = _WEATHER_CODE_TEXT.get(int(wcode), "mixed") if isinstance(wcode, int) else str(cur.get("condition") or "mixed")
            high = (daily.get("temperature_2m_max") or [None])[0]
            low = (daily.get("temperature_2m_min") or [None])[0]
            return {
                "state": "ok",
                "data": {
                    "temp_c": cur.get("temperature_2m"),
                    "condition": cond,
                    "high_c": high,
                    "low_c": low,
                    "alerts": list(data.get("alerts") or []),
                    "provider": data.get("provider") or payload.get("provider") or "WeatherAPI",
                },
            }
        except Exception as exc:
            return {"state": "unavailable", "reason": type(exc).__name__, "data": None}

    def _gather_calendar(self, start_iso: str, end_iso: str) -> dict[str, Any]:
        try:
            raw_rows: list[Any]
            if self.calendar_source is not None:
                raw_rows = list(self.calendar_source(start_iso, end_iso) or [])
            elif os.environ.get("NEXUS_GOOGLE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
                from ..assistant.personal_data import GoogleWorkspace, NativeVault

                raw_rows = GoogleWorkspace(NativeVault()).calendar(time_min=start_iso, time_max=end_iso, limit=15)
            else:
                return {"state": "disabled", "reason": "Calendar source not configured (Task 9 / NEXUS_GOOGLE_ENABLED)", "data": []}

            events: list[dict[str, Any]] = []
            flags: list[str] = []
            for row in raw_rows[:20]:
                if isinstance(row, dict):
                    raw_title = row.get("title") or row.get("summary") or "Untitled event"
                    start_v = row.get("start") or ""
                    loc_v = row.get("location") or ""
                    ident = str(row.get("id") or "")
                else:
                    raw_title = getattr(row, "text", "") or "Event"
                    meta = getattr(row, "metadata", {}) or {}
                    start_v = meta.get("start", "")
                    loc_v = meta.get("location", "")
                    ident = str(getattr(row, "source", ""))
                clean_title, tflags = _clean_untrusted(self.workspace, raw_title, limit=140)
                clean_loc, lflags = _clean_untrusted(self.workspace, loc_v, limit=80)
                flags.extend(tflags + lflags)
                events.append({
                    "id": ident or hashlib.sha256(clean_title.encode()).hexdigest()[:10],
                    "title": clean_title,
                    "start": start_v,
                    "location": clean_loc,
                    "untrusted": True,
                })
            return {"state": "ok", "data": events, "injection_flags": sorted(set(flags))}
        except Exception as exc:
            return {"state": "unavailable", "reason": type(exc).__name__, "data": []}

    def _gather_email(self) -> dict[str, Any]:
        try:
            raw_rows: list[Any]
            if self.email_source is not None:
                raw_rows = list(self.email_source() or [])
            elif os.environ.get("NEXUS_GOOGLE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
                from ..assistant.personal_data import GoogleWorkspace, NativeVault

                raw_rows = GoogleWorkspace(NativeVault()).gmail(query="is:important newer_than:1d", limit=10)
            else:
                return {"state": "disabled", "reason": "Email source not configured (Task 9 / NEXUS_GOOGLE_ENABLED)", "data": []}

            emails: list[dict[str, Any]] = []
            flags: list[str] = []
            for row in raw_rows[:15]:
                if isinstance(row, dict):
                    subj_raw = row.get("subject") or "(no subject)"
                    sender_raw = row.get("from") or row.get("sender") or ""
                    snip_raw = row.get("snippet") or row.get("body") or ""
                    important = bool(row.get("important", True))
                    ident = str(row.get("id") or "")
                else:
                    subj_raw = getattr(row, "text", "")[:120]
                    sender_raw = (getattr(row, "metadata", {}) or {}).get("from", "")
                    snip_raw = getattr(row, "text", "")
                    important = True
                    ident = str(getattr(row, "source", ""))
                if not important:
                    continue
                subj, sf = _clean_untrusted(self.workspace, subj_raw, limit=140)
                sender, ff = _clean_untrusted(self.workspace, sender_raw, limit=80)
                snip, snf = _clean_untrusted(self.workspace, snip_raw, limit=160)
                flags.extend(sf + ff + snf)
                emails.append({
                    "id": ident or hashlib.sha256(subj.encode()).hexdigest()[:10],
                    "from": sender,
                    "subject": subj,
                    "snippet": snip,
                    "untrusted": True,
                })
            return {"state": "ok", "data": emails, "injection_flags": sorted(set(flags))}
        except Exception as exc:
            return {"state": "unavailable", "reason": type(exc).__name__, "data": []}

    def _gather_tasks(self, now_ts: float) -> dict[str, Any]:
        from ..assistant.proactive import ReminderStore

        items: list[dict[str, Any]] = []
        horizon = now_ts + 86400.0
        try:
            for row in ReminderStore(self.workspace).list():
                if not row.get("enabled"):
                    continue
                nxt = float(row.get("next_run") or 0)
                if nxt <= horizon:
                    items.append({
                        "kind": "reminder",
                        "id": row["id"],
                        "title": _clean_safe(self.workspace, row["text"], limit=140),
                        "due_at": nxt,
                        "overdue": nxt <= now_ts,
                    })
        except Exception:
            pass
        try:
            for sched in Scheduler(self.workspace, studio=self.studio, clock=self.clock).list():
                if not sched.enabled:
                    continue
                if sched.next_run_at <= horizon:
                    items.append({
                        "kind": "schedule",
                        "id": sched.id,
                        "title": _clean_safe(self.workspace, sched.task, limit=140),
                        "due_at": sched.next_run_at,
                        "overdue": sched.next_run_at <= now_ts,
                    })
        except Exception:
            pass
        if self.studio is not None:
            try:
                from .queue import Queue

                for job in Queue(self.studio)._jobs():
                    if job.get("status") in {"awaiting_approval", "approved", "running"}:
                        items.append({
                            "kind": "jarvis_job",
                            "id": job["id"],
                            "title": _clean_safe(self.workspace, job.get("task"), limit=140),
                            "status": job.get("status"),
                            "due_at": float(job.get("created") or now_ts),
                            "overdue": False,
                        })
            except Exception:
                pass
        items.sort(key=lambda r: (not r.get("overdue", False), float(r.get("due_at") or now_ts)))
        return {"state": "ok", "data": items[:20]}

    def _gather_system_and_ci(self, cfg: ProactiveConfig) -> dict[str, Any]:
        sys_data: dict[str, Any] = {}
        try:
            if self.system_sampler is not None:
                sys_data = dict(self.system_sampler() or {})
            else:
                import psutil

                disk = psutil.disk_usage(str(self.workspace))
                sys_data = {
                    "cpu_percent": round(float(psutil.cpu_percent(interval=None)), 1),
                    "memory_percent": round(float(psutil.virtual_memory().percent), 1),
                    "disk_free_percent": round(100.0 * disk.free / max(1, disk.total), 1),
                }
        except Exception as exc:
            sys_data = {"error": type(exc).__name__}

        if self.studio is not None:
            try:
                from ..monitoring import collect_self_monitoring

                mon = collect_self_monitoring(self.workspace, studio=self.studio)
                sys_data["queue_depth"] = mon.get("queue_depth")
                sys_data["running_missions"] = mon.get("running_missions")
            except Exception:
                pass

        ci_section: dict[str, Any] = {"state": "disabled", "failing": False, "checks": 0}
        if cfg.morning_brief.include_ci and (self.ci_source is not None or cfg.morning_brief.ci_repo):
            repo = cfg.morning_brief.ci_repo or "local/repo"
            ref = cfg.morning_brief.ci_ref
            try:
                if self.ci_source is not None:
                    raw_ci, failing = self.ci_source(repo, ref)
                else:
                    raw_ci, failing = github(self.workspace, repo, ref)
                ci_section = {
                    "state": "ok",
                    "repo": repo,
                    "ref": ref,
                    "failing": bool(failing),
                    "checks": len((raw_ci or {}).get("checks") or []),
                }
            except Exception as exc:
                ci_section = {"state": "unavailable", "repo": repo, "reason": type(exc).__name__, "failing": False}

        return {"state": "ok", "data": {"server": sys_data, "ci": ci_section}}

    def _gather_news(self, cfg: ProactiveConfig) -> dict[str, Any]:
        topics = [t.casefold() for t in cfg.morning_brief.news_topics]
        try:
            if self.news_source is not None:
                raw = self.news_source()
                raw_items = raw.get("items", []) if isinstance(raw, dict) else list(raw or [])
            elif self.world is not None:
                res = self.world.news()
                if res.get("status") == "disabled":
                    return {"state": "disabled", "reason": "World news disabled", "data": []}
                raw_items = res.get("items") or []
            else:
                from ..hud.world.core import World

                w = World(self.workspace, clock=self.clock)
                if not w.config.get("enabled", True):
                    return {"state": "disabled", "reason": "World news disabled", "data": []}
                res = w.news()
                raw_items = res.get("items") or []

            matched: list[dict[str, Any]] = []
            flags: list[str] = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                title_raw = item.get("title") or ""
                summary_raw = item.get("summary") or ""
                haystack = f"{title_raw} {summary_raw}".casefold()
                matched_topics = [orig for orig, low in zip(cfg.morning_brief.news_topics, topics, strict=True) if low in haystack]
                if topics and not matched_topics:
                    continue
                clean_t, tf = _clean_untrusted(self.workspace, title_raw, limit=180)
                clean_src, sf = _clean_untrusted(self.workspace, item.get("source") or "", limit=60)
                flags.extend(tf + sf)
                matched.append({
                    "title": clean_t,
                    "source": clean_src,
                    "topics": matched_topics,
                    "untrusted": True,
                })
                if len(matched) >= cfg.morning_brief.max_news_items:
                    break
            return {"state": "ok", "data": matched, "topics": list(cfg.morning_brief.news_topics), "injection_flags": sorted(set(flags))}
        except Exception as exc:
            return {"state": "unavailable", "reason": type(exc).__name__, "data": []}

    def build(self, *, cfg: ProactiveConfig | None = None, now: float | None = None) -> dict[str, Any]:
        policy = cfg or load_proactive_config(self.workspace)
        ts = float(self.clock() if now is None else now)
        tz_name = effective_timezone(self.workspace, policy)
        tz = ZoneInfo(tz_name)
        now_dt = datetime.fromtimestamp(ts, tz=tz)
        day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        mb = policy.morning_brief
        sections = {
            "weather": self._gather_weather() if mb.include_weather else {"state": "disabled", "data": None},
            "calendar": self._gather_calendar(day_start.isoformat(), day_end.isoformat()) if mb.include_calendar else {"state": "disabled", "data": []},
            "email": self._gather_email() if mb.include_email else {"state": "disabled", "data": []},
            "tasks": self._gather_tasks(ts) if mb.include_tasks else {"state": "disabled", "data": []},
            "system": self._gather_system_and_ci(policy) if mb.include_system else {"state": "disabled", "data": {}},
            "news": self._gather_news(policy),
        }

        spoken_parts: list[str] = ["Good morning."]
        w = sections["weather"]
        if w["state"] == "ok" and w["data"]:
            wd = w["data"]
            temp = wd.get("temp_c")
            t_str = f"{round(float(temp))} degrees" if isinstance(temp, (int, float)) else "current weather"
            spoken_parts.append(f"Weather is {t_str} and {wd.get('condition', 'clear')}.")
        c = sections["calendar"]
        if c["state"] == "ok":
            evs = c["data"]
            spoken_parts.append(f"{len(evs)} calendar event{'s' if len(evs) != 1 else ''} today." if evs else "Calendar is clear.")
        em = sections["email"]
        if em["state"] == "ok":
            ems = em["data"]
            spoken_parts.append(f"{len(ems)} important email{'s' if len(ems) != 1 else ''}." if ems else "No urgent emails.")
        tk = sections["tasks"]
        if tk["state"] == "ok":
            tks = tk["data"]
            spoken_parts.append(f"{len(tks)} task{'s' if len(tks) != 1 else ''} due." if tks else "No tasks due.")
        sys_sec = sections["system"]
        if sys_sec["state"] == "ok":
            ci = (sys_sec["data"] or {}).get("ci") or {}
            if ci.get("state") == "ok":
                spoken_parts.append("CI is failing." if ci.get("failing") else "CI is passing.")
        nw = sections["news"]
        if nw["state"] == "ok" and nw["data"]:
            spoken_parts.append(f"{len(nw['data'])} news update{'s' if len(nw['data']) != 1 else ''} on HUD.")

        spoken_raw = " ".join(spoken_parts)
        spoken_text = _clean_safe(self.workspace, spoken_raw, limit=mb.spoken_max_chars)
        if len(spoken_raw) > mb.spoken_max_chars:
            spoken_text = spoken_text.rstrip(". ") + ". Full details on the HUD."

        # Build full structured HUD text.
        hud_lines = [f"# Morning Brief — {now_dt.strftime('%Y-%m-%d')} ({tz_name})"]
        if w["state"] == "ok" and w["data"]:
            wd = w["data"]
            hud_lines.append(f"## Weather\n- {wd.get('temp_c')}°C, {wd.get('condition')} (high {wd.get('high_c')}°C / low {wd.get('low_c')}°C)")
        else:
            hud_lines.append(f"## Weather\n- {w['state']}: {w.get('reason', '')}")

        if c["state"] == "ok":
            evs = c["data"]
            hud_lines.append("## Calendar\n" + ("\n".join(f"- {e['start']} {e['title']}" for e in evs) if evs else "- No events today"))
        else:
            hud_lines.append(f"## Calendar\n- {c['state']}: {c.get('reason', '')}")

        if em["state"] == "ok":
            ems = em["data"]
            hud_lines.append("## Important Emails (Untrusted)\n" + ("\n".join(f"- {e['from']}: {e['subject']}" for e in ems) if ems else "- No important emails"))
        else:
            hud_lines.append(f"## Important Emails\n- {em['state']}: {em.get('reason', '')}")

        if tk["state"] == "ok":
            tks = tk["data"]
            hud_lines.append("## Tasks Due\n" + ("\n".join(f"- [{t['kind']}] {t['title']}" for t in tks) if tks else "- No tasks due"))

        if sys_sec["state"] == "ok":
            srv = (sys_sec["data"] or {}).get("server") or {}
            ci = (sys_sec["data"] or {}).get("ci") or {}
            hud_lines.append(
                f"## System / CI / Server\n"
                f"- CPU {srv.get('cpu_percent')}% · RAM {srv.get('memory_percent')}% · Disk free {srv.get('disk_free_percent')}%\n"
                f"- CI ({ci.get('state')}): {'failing' if ci.get('failing') else 'ok'}"
            )

        if nw["state"] == "ok":
            items = nw["data"]
            hud_lines.append("## News (Untrusted)\n" + ("\n".join(f"- {n['title']} ({n['source']})" for n in items) if items else "- No matching topic items"))
        else:
            hud_lines.append(f"## News\n- {nw['state']}: {nw.get('reason', '')}")

        hud_text = "\n\n".join(hud_lines)
        return {
            "schema": 1,
            "generated_at": ts,
            "date": now_dt.strftime("%Y-%m-%d"),
            "timezone": tz_name,
            "sections": sections,
            "spoken_text": spoken_text,
            "hud_text": hud_text,
        }


# ── Opt-in Monitors Evaluator (with threshold, cooldown, severity) ─────── #


def _evaluate_single_monitor(
    workspace: Path,
    spec: MonitorSpec,
    *,
    http_checker: Callable[[MonitorSpec], dict[str, Any]] | None = None,
    server_checker: Callable[[MonitorSpec], dict[str, Any]] | None = None,
    ci_checker: Callable[[str, str], tuple[dict[str, Any], bool]] | None = None,
    system_sampler: Callable[[], dict[str, Any]] | None = None,
    market_checker: Callable[[MonitorSpec], dict[str, Any]] | None = None,
    news_source: Callable[[], dict[str, Any] | list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    kind = spec.kind
    opts = spec.options

    if kind == "website":
        if http_checker is not None:
            res = dict(http_checker(spec))
            status = res.get("status")
            latency = float(res.get("latency_ms", 0.0) or 0.0)
            ok = bool(res.get("ok", status == opts.get("status", 200)))
            if opts.get("max_latency_ms") and latency > float(opts["max_latency_ms"]):
                ok = False
            if spec.threshold and spec.threshold > 1 and latency > spec.threshold:
                ok = False
            return {"ok": ok, "alert": not ok, "summary": f"website {opts['url']} status={status} latency={latency:.0f}ms", **res}
        try:
            t0 = time.monotonic()
            resp = httpx.get(opts["url"], timeout=10, follow_redirects=False, trust_env=False)
            latency_ms = (time.monotonic() - t0) * 1000.0
            expected = int(opts.get("status", 200))
            max_lat = float(opts.get("max_latency_ms") or (spec.threshold if spec.threshold > 10 else 0))
            ok = resp.status_code == expected and (not max_lat or latency_ms <= max_lat)
            return {
                "ok": ok,
                "alert": not ok,
                "status": resp.status_code,
                "expected": expected,
                "latency_ms": round(latency_ms, 1),
                "summary": f"website {opts['url']} status={resp.status_code} ({latency_ms:.0f}ms)",
            }
        except httpx.HTTPError as exc:
            return {"ok": False, "alert": True, "reason": type(exc).__name__, "summary": f"website {opts['url']} unreachable ({type(exc).__name__})"}

    if kind == "server":
        if server_checker is not None:
            res = dict(server_checker(spec))
            ok = bool(res.get("ok", True))
            return {"ok": ok, "alert": not ok, "summary": str(res.get("summary") or f"server {spec.id} ok={ok}"), **res}
        if opts.get("url"):
            try:
                t0 = time.monotonic()
                resp = httpx.get(opts["url"], timeout=8, follow_redirects=False, trust_env=False)
                latency_ms = (time.monotonic() - t0) * 1000.0
                ok = 200 <= resp.status_code < 400 and (not opts.get("max_latency_ms") or latency_ms <= float(opts["max_latency_ms"]))
                return {"ok": ok, "alert": not ok, "status": resp.status_code, "latency_ms": round(latency_ms, 1), "summary": f"server {opts['url']} status={resp.status_code}"}
            except httpx.HTTPError as exc:
                return {"ok": False, "alert": True, "reason": type(exc).__name__, "summary": f"server {opts['url']} down ({type(exc).__name__})"}
        host, port = str(opts["host"]), int(opts["port"])
        try:
            t0 = time.monotonic()
            with socket.create_connection((host, port), timeout=5):
                latency_ms = (time.monotonic() - t0) * 1000.0
            ok = not opts.get("max_latency_ms") or latency_ms <= float(opts["max_latency_ms"])
            return {"ok": ok, "alert": not ok, "host": host, "port": port, "latency_ms": round(latency_ms, 1), "summary": f"server {host}:{port} reachable ({latency_ms:.0f}ms)"}
        except OSError as exc:
            return {"ok": False, "alert": True, "host": host, "port": port, "reason": type(exc).__name__, "summary": f"server {host}:{port} unreachable ({type(exc).__name__})"}

    if kind == "github_ci":
        repo = opts["repo"]
        ref = opts.get("ref", "HEAD")
        if ci_checker is not None:
            raw_ci, failing = ci_checker(repo, ref)
        else:
            raw_ci, failing = github(workspace, repo, ref)
        checks = (raw_ci or {}).get("checks") or []
        fail_count = sum(1 for c in checks if len(c) >= 3 and c[2] in {"failure", "cancelled", "timed_out", "action_required"})
        if failing and fail_count == 0:
            fail_count = 1
        alert = bool(failing) and fail_count >= max(1, int(spec.threshold or 1))
        return {
            "ok": not alert,
            "alert": alert,
            "repo": repo,
            "ref": ref,
            "failing_checks": fail_count,
            "summary": f"GitHub CI {repo}@{ref}: {fail_count} failing check(s)" if alert else f"GitHub CI {repo}@{ref} passing",
        }

    if kind in {"cpu", "memory", "disk", "system"}:
        metric = opts.get("metric") or ("disk_used" if kind == "disk" else kind)
        if system_sampler is not None:
            sample = dict(system_sampler())
            if metric in sample:
                val = float(sample[metric])
            elif metric == "cpu" and "cpu_percent" in sample:
                val = float(sample["cpu_percent"])
            elif metric == "memory" and "memory_percent" in sample:
                val = float(sample["memory_percent"])
            elif metric == "disk_used" and "disk_used_percent" in sample:
                val = float(sample["disk_used_percent"])
            elif metric == "disk_free" and "disk_free_percent" in sample:
                val = float(sample["disk_free_percent"])
            else:
                raise ProactiveError(f"System sampler missing metric {metric!r}")
        else:
            import psutil

            if metric == "cpu":
                val = float(psutil.cpu_percent(interval=None))
            elif metric == "memory":
                val = float(psutil.virtual_memory().percent)
            else:
                du = psutil.disk_usage(str(workspace))
                val = (100.0 * du.free / max(1, du.total)) if metric == "disk_free" else (100.0 * du.used / max(1, du.total))
        alert = val <= spec.threshold if metric == "disk_free" else val >= spec.threshold
        return {
            "ok": not alert,
            "alert": alert,
            "metric": metric,
            "value": round(val, 2),
            "threshold": spec.threshold,
            "summary": f"{metric}={val:.1f}% (threshold {spec.threshold:.1f}%)",
        }

    if kind in {"price", "stock", "market"}:
        if market_checker is not None:
            res = dict(market_checker(spec))
            val = float(res["value"] if "value" in res else res["price"])
        else:
            url = opts.get("url")
            if not url:
                raise ProactiveError("Market/price monitor requires https url or injected market_checker")
            resp = httpx.get(url, timeout=10, follow_redirects=False, trust_env=False)
            resp.raise_for_status()
            if len(resp.content) > 1_000_000:
                raise ProactiveError("Market response exceeds budget")
            cur = resp.json()
            for part in str(opts.get("field") or "price").split("."):
                cur = cur[int(part)] if isinstance(cur, list) and part.isdigit() else cur.get(part) if isinstance(cur, dict) else None
            if isinstance(cur, bool) or not isinstance(cur, (int, float)):
                raise ProactiveError("Price/stock endpoint missing numeric field")
            val = float(cur)
        direction = opts.get("direction", "above")
        if direction == "above":
            alert = val >= spec.threshold
        elif direction == "below":
            alert = val <= spec.threshold
        else:
            alert = abs(val) >= abs(spec.threshold)
        label = opts.get("symbol") or spec.id
        return {
            "ok": not alert,
            "alert": alert,
            "symbol": label,
            "value": val,
            "threshold": spec.threshold,
            "direction": direction,
            "summary": f"{label} price/change={val} ({direction} {spec.threshold})",
        }

    if kind == "log_pattern":
        target = Permissions(workspace).path(opts["path"])
        if not target.exists():
            return {"ok": True, "alert": False, "matches": 0, "summary": f"log {opts['path']} not present"}
        raw_bytes = bounded(target, limit=MAX_LOG_BYTES)
        text = raw_bytes.decode("utf-8", errors="replace")
        flags = 0 if opts.get("case_sensitive", False) else re.IGNORECASE
        regex = re.compile(opts["pattern"], flags)
        matched_lines: list[str] = []
        for line in text.splitlines():
            if regex.search(line):
                clean_line, _ = _clean_untrusted(workspace, line, limit=120)
                matched_lines.append(clean_line)
        count = len(matched_lines)
        alert = count >= max(1, int(spec.threshold or 1))
        return {
            "ok": not alert,
            "alert": alert,
            "matches": count,
            "sample": matched_lines[-1] if matched_lines else "",
            "untrusted": True,
            "summary": f"log {opts['path']} matched '{opts['pattern']}' {count} time(s)",
        }

    if kind == "rss_keyword":
        if news_source is not None:
            raw = news_source()
            items = raw.get("items", []) if isinstance(raw, dict) else list(raw or [])
        else:
            from ..hud.world.core import World

            w = World(workspace)
            if not w.config.get("enabled", True):
                return {"ok": True, "alert": False, "matches": 0, "summary": "RSS disabled in widget.yaml"}
            items = w.news().get("items") or []
        allowed_sources = set(opts.get("source_ids") or [])
        keywords = [k.casefold() for k in opts["keywords"]]
        hits: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if allowed_sources and item.get("source_id") not in allowed_sources:
                continue
            blob = f"{item.get('title', '')} {item.get('summary', '')}".casefold()
            if any(k in blob for k in keywords):
                clean_title, _ = _clean_untrusted(workspace, item.get("title") or "", limit=120)
                hits.append(clean_title)
        count = len(hits)
        alert = count >= max(1, int(spec.threshold or 1))
        return {
            "ok": not alert,
            "alert": alert,
            "matches": count,
            "top_match": hits[0] if hits else "",
            "untrusted": True,
            "summary": f"RSS matched {count} item(s) for {', '.join(opts['keywords'][:3])}",
        }

    raise ProactiveError(f"Unsupported monitor kind {kind!r}")


# ── Suggestion Engine (ONE proposal, rate limited, learns stop-suggesting) ─ #


class SuggestionEngine:
    """Proposes at most ONE context-aware action per pass without auto-executing."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        store: ProactiveStateStore | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.clock = clock
        self.store = store or ProactiveStateStore(self.workspace, clock=clock)

    @staticmethod
    def _normalize_topic(raw: str) -> str:
        cleaned = re.sub(r"[^a-z0-9_:\s-]+", " ", (raw or "").casefold())
        return " ".join(cleaned.split())[:80]

    def suppressed_list(self) -> list[dict[str, Any]]:
        return list(self.store.read().get("suppressed_suggestions") or [])

    def learn_stop_suggesting(
        self,
        topic: str = "",
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = float(self.clock() if now is None else now)
        norm_topic = self._normalize_topic(topic)

        def _mut(data: dict[str, Any]) -> dict[str, Any]:
            last = data.get("last_suggestion") or {}
            suppressed = list(data.get("suppressed_suggestions") or [])
            if not norm_topic and last:
                entry = {
                    "id": "sup-" + uuid.uuid4().hex[:10],
                    "rule_id": str(last.get("rule_id") or ""),
                    "topic": str(last.get("topic") or ""),
                    "pattern": self._normalize_topic(str(last.get("topic") or last.get("title") or "")),
                    "created_at": ts,
                    "reason": "user_stopped_last_suggestion",
                }
            else:
                resolved = norm_topic or "general"
                # Map common human phrases to canonical rule topics as well
                canonical = resolved
                if any(w in resolved for w in ("meeting", "calendar", "call")):
                    canonical = "meeting_soon"
                elif any(w in resolved for w in ("deadline", "due", "tomorrow")):
                    canonical = "deadline_tomorrow"
                elif any(w in resolved for w in ("build", "ci", "test", "failure", "failing")):
                    canonical = "build_failing"
                entry = {
                    "id": "sup-" + uuid.uuid4().hex[:10],
                    "rule_id": "",
                    "topic": canonical,
                    "pattern": resolved,
                    "created_at": ts,
                    "reason": "user_stopped_topic",
                }
            # Avoid duplicate entries
            if not any(
                s.get("rule_id") == entry["rule_id"] and s.get("topic") == entry["topic"] and s.get("pattern") == entry["pattern"]
                for s in suppressed
            ):
                suppressed.append(entry)
            data["suppressed_suggestions"] = suppressed[-MAX_SUPPRESSED_SUGGESTIONS:]
            return entry

        recorded = self.store.update(_mut)
        return {"learned": True, "suppression": recorded}

    def unmute(self, ident_or_topic: str) -> bool:
        target = (ident_or_topic or "").strip()
        removed = [False]

        def _mut(data: dict[str, Any]) -> None:
            rows = list(data.get("suppressed_suggestions") or [])
            kept = [
                r for r in rows
                if r.get("id") != target and r.get("topic") != target and r.get("rule_id") != target
            ]
            removed[0] = len(kept) != len(rows)
            data["suppressed_suggestions"] = kept

        self.store.update(_mut)
        return removed[0]

    @staticmethod
    def _is_suppressed(candidate: dict[str, Any], suppressed: list[dict[str, Any]]) -> bool:
        c_rule = str(candidate.get("rule_id") or "").casefold()
        c_topic = str(candidate.get("topic") or "").casefold()
        c_blob = f"{c_rule} {c_topic} {candidate.get('title', '')} {candidate.get('message', '')}".casefold()
        for entry in suppressed:
            e_rule = str(entry.get("rule_id") or "").casefold()
            e_topic = str(entry.get("topic") or "").casefold()
            e_pat = str(entry.get("pattern") or "").casefold()
            if e_rule and e_rule == c_rule:
                return True
            if e_topic and e_topic == c_topic:
                return True
            if e_pat and e_pat in c_blob:
                return True
        return False

    @staticmethod
    def _parse_iso_ts(value: Any) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            return dt.timestamp()
        except ValueError:
            return None

    def evaluate(
        self,
        *,
        calendar_events: list[dict[str, Any]] | None = None,
        tasks_due: list[dict[str, Any]] | None = None,
        failing_builds: list[dict[str, Any]] | None = None,
        cfg: ProactiveConfig | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        policy = cfg or load_proactive_config(self.workspace)
        if not policy.enabled or not policy.suggestions.enabled:
            return None
        ts = float(self.clock() if now is None else now)
        scfg = policy.suggestions
        state = self.store.read()
        suppressed = list(state.get("suppressed_suggestions") or [])
        last_by_rule = dict(state.get("suggestion_last_proposed") or {})

        candidates: list[dict[str, Any]] = []

        # 1. Failing builds / CI (highest urgency)
        for b in failing_builds or []:
            repo = str(b.get("repo") or b.get("id") or "workspace")
            rule_id = f"build_failing:{repo}"
            candidates.append({
                "priority": 0,
                "id": "sug-" + uuid.uuid4().hex[:10],
                "rule_id": rule_id,
                "topic": "build_failing",
                "severity": "high",
                "title": f"Build failing: {repo}",
                "message": f"CI/build is failing on {repo}. Want me to inspect the failure in read-only mode?",
                "proposed_action": {
                    "kind": "enqueue_task",
                    "target": "queue",
                    "task": f"Inspect failing build/CI checks for {repo} and summarize root cause (propose fix only).",
                    "requires_approval": True,
                },
            })

        # 2. Meeting starting soon
        soon_sec = scfg.meeting_soon_minutes * 60.0
        for ev in calendar_events or []:
            start_ts = self._parse_iso_ts(ev.get("start_ts") if ev.get("start_ts") is not None else ev.get("start"))
            if start_ts is None:
                continue
            delta = start_ts - ts
            if 0.0 <= delta <= soon_sec:
                ev_id = str(ev.get("id") or hashlib.sha256(str(ev.get("title", "")).encode()).hexdigest()[:8])
                title, _ = _clean_untrusted(self.workspace, ev.get("title") or "Meeting", limit=100)
                mins = max(1, int(round(delta / 60.0)))
                candidates.append({
                    "priority": 1,
                    "id": "sug-" + uuid.uuid4().hex[:10],
                    "rule_id": f"meeting_soon:{ev_id}",
                    "topic": "meeting_soon",
                    "severity": "medium",
                    "title": f"Meeting in {mins}m: {title}",
                    "message": f"'{title}' starts in {mins} minute(s). Want me to prepare brief meeting notes?",
                    "proposed_action": {
                        "kind": "enqueue_task",
                        "target": "queue",
                        "task": f"Prepare concise agenda and context notes for meeting '{title}'.",
                        "requires_approval": True,
                    },
                })

        # 3. Deadline tomorrow / within deadline_hours
        deadline_sec = scfg.deadline_hours * 3600.0
        for task in tasks_due or []:
            due_ts = self._parse_iso_ts(task.get("due_at"))
            if due_ts is None:
                continue
            delta = due_ts - ts
            if 0.0 < delta <= deadline_sec:
                tid = str(task.get("id") or hashlib.sha256(str(task.get("title", "")).encode()).hexdigest()[:8])
                title = _clean_safe(self.workspace, task.get("title") or "Task", limit=100)
                hours = max(1, int(round(delta / 3600.0)))
                candidates.append({
                    "priority": 2,
                    "id": "sug-" + uuid.uuid4().hex[:10],
                    "rule_id": f"deadline_tomorrow:{tid}",
                    "topic": "deadline_tomorrow",
                    "severity": "medium",
                    "title": f"Deadline in {hours}h: {title}",
                    "message": f"'{title}' is due in ~{hours}h. Want me to queue a prep checklist?",
                    "proposed_action": {
                        "kind": "enqueue_task",
                        "target": "queue",
                        "task": f"Prepare step-by-step checklist for upcoming deadline: {title}",
                        "requires_approval": True,
                    },
                })

        # Sort by priority, filter suppressed and cooled-down, propose AT MOST ONE.
        candidates.sort(key=lambda c: c["priority"])
        chosen: dict[str, Any] | None = None
        for cand in candidates:
            if self._is_suppressed(cand, suppressed):
                continue
            prev_ts = last_by_rule.get(cand["rule_id"])
            if isinstance(prev_ts, (int, float)) and ts - float(prev_ts) < scfg.cooldown_seconds:
                continue
            chosen = dict(cand)
            chosen["proposed_at"] = ts
            break

        if chosen is None:
            return None

        def _record(data: dict[str, Any]) -> None:
            data["last_suggestion"] = chosen
            times = dict(data.get("suggestion_last_proposed") or {})
            times[chosen["rule_id"]] = ts
            data["suggestion_last_proposed"] = times

        self.store.update(_record)
        return chosen


# ── Unified Proactive Engine ───────────────────────────────────────────── #


class ProactiveEngine:
    """Cohesive proactive assistant engine extending ``nexus.jarvis``."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        studio: Any = None,
        server_state: Any = None,
        world: Any = None,
        weather_source: Callable[[], dict[str, Any]] | None = None,
        calendar_source: Callable[[str, str], list[dict[str, Any]]] | None = None,
        email_source: Callable[[], list[dict[str, Any]]] | None = None,
        news_source: Callable[[], dict[str, Any] | list[dict[str, Any]]] | None = None,
        system_sampler: Callable[[], dict[str, Any]] | None = None,
        ci_source: Callable[[str, str], tuple[dict[str, Any], bool]] | None = None,
        http_checker: Callable[[MonitorSpec], dict[str, Any]] | None = None,
        server_checker: Callable[[MonitorSpec], dict[str, Any]] | None = None,
        market_checker: Callable[[MonitorSpec], dict[str, Any]] | None = None,
        voice_deliverer: Callable[[str], Any] | None = None,
        telegram_sender: Callable[[str, str], Any] | None = None,
        desktop_notifier: Callable[[str, str], Any] | None = None,
        hud_toast_sink: Callable[[dict[str, Any]], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.studio = studio
        self.server_state = server_state
        self.clock = clock
        self.store = ProactiveStateStore(self.workspace, clock=clock)
        self.dnd = DNDManager(self.workspace, store=self.store, studio=studio, clock=clock)
        self.channels = ProactiveChannelRouter(
            self.workspace,
            store=self.store,
            dnd=self.dnd,
            studio=studio,
            server_state=server_state,
            voice_deliverer=voice_deliverer,
            telegram_sender=telegram_sender,
            desktop_notifier=desktop_notifier,
            hud_toast_sink=hud_toast_sink,
            clock=clock,
        )
        self.brief_engine = MorningBriefEngine(
            self.workspace,
            studio=studio,
            world=world,
            weather_source=weather_source,
            calendar_source=calendar_source,
            email_source=email_source,
            news_source=news_source,
            system_sampler=system_sampler,
            ci_source=ci_source,
            clock=clock,
        )
        self.suggestions = SuggestionEngine(self.workspace, store=self.store, clock=clock)
        self.http_checker = http_checker
        self.server_checker = server_checker
        self.market_checker = market_checker
        self.ci_source = ci_source
        self.system_sampler = system_sampler
        self.news_source = news_source

    def config(self) -> ProactiveConfig:
        return load_proactive_config(self.workspace)

    def _ensure_enabled(self) -> ProactiveConfig:
        cfg = self.config()
        if not cfg.enabled:
            raise ProactiveDisabledError("Proactive engine is disabled in proactive.yaml (enabled: false)")
        return cfg

    # ── 1. Morning brief delivery ──────────────────────────────────────── #

    def run_morning_brief(
        self,
        *,
        trigger: str = "manual",
        present: bool | None = None,
        now: float | None = None,
        force: bool = True,
    ) -> dict[str, Any]:
        cfg = self._ensure_enabled()
        if not cfg.morning_brief.enabled:
            raise ProactiveDisabledError("morning_brief is disabled in proactive.yaml")
        ts = float(self.clock() if now is None else now)
        tz = ZoneInfo(effective_timezone(self.workspace, cfg))
        local_dt = datetime.fromtimestamp(ts, tz=tz)
        today_str = local_dt.strftime("%Y-%m-%d")

        if trigger == "schedule" and not force:
            if not cfg.morning_brief.deliver_on_schedule:
                return {"skipped": True, "reason": "deliver_on_schedule disabled"}
            if local_dt.strftime("%H:%M") < cfg.morning_brief.time:
                return {"skipped": True, "reason": "before scheduled time"}
            if self.store.read().get("brief_last_date") == today_str:
                return {"skipped": True, "reason": "already delivered today"}
        if trigger == "greeting" and not cfg.morning_brief.deliver_on_greeting:
            return {"skipped": True, "reason": "deliver_on_greeting disabled"}

        brief = self.brief_engine.build(cfg=cfg, now=ts)
        user_present = True if trigger == "greeting" else (bool(present) if present is not None else is_user_present(self.workspace, now=ts))
        route_channels = ("hud_toast", "voice") if user_present else ("hud_toast", "telegram", "desktop")

        delivery = self.channels.dispatch(
            title="Morning Brief",
            message=brief["spoken_text"],
            spoken_text=brief["spoken_text"],
            hud_text=brief["hud_text"],
            severity="medium",
            source="morning_brief",
            present=user_present,
            explicit_user_request=(trigger in {"greeting", "manual"}),
            rate_limit_speech=False,
            channels_override=route_channels,
            extra_hud_data={"brief_date": today_str, "sections": brief["sections"]},
            now=ts,
            cfg=cfg,
        )

        def _save(data: dict[str, Any]) -> None:
            data["brief_last_date"] = today_str
            data["last_brief"] = {
                "date": today_str,
                "generated_at": ts,
                "spoken_text": brief["spoken_text"],
                "hud_text": brief["hud_text"],
                "delivery": delivery,
            }

        self.store.update(_save)
        return {"brief": brief, "delivery": delivery, "trigger": trigger, "skipped": False}

    # ── 2. Voice reminders & recurring tasks ───────────────────────────── #

    def create_from_voice(self, utterance: str, *, now: float | None = None, as_task: bool = False) -> dict[str, Any]:
        cfg = self._ensure_enabled()
        if not cfg.voice_reminders.enabled:
            raise ProactiveDisabledError("voice_reminders is disabled in proactive.yaml")
        ts = float(self.clock() if now is None else now)
        tz_name = effective_timezone(self.workspace, cfg)
        parsed = parse_voice_reminder(utterance, timezone=tz_name, now=ts, as_task=as_task)
        scan_text(parsed.task, workspace=self.workspace)

        if parsed.is_task:
            # Recurring/scheduled task -> stored in Scheduler, queued awaiting_approval under L1.
            sched = Scheduler(self.workspace, studio=self.studio, clock=self.clock)
            expr = parsed.cron_expr or (
                f"every {parsed.every_minutes} minutes" if parsed.every_minutes
                else datetime.fromtimestamp(float(parsed.run_at or ts), tz=ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")
            )
            entry = sched.add(
                expr,
                task=parsed.task[:MAX_TASK_CHARS],
                timezone=tz_name,
                target=cfg.voice_reminders.default_recurring_target,
            )
            ack = f"Scheduled recurring task ({parsed.summary}). It will require approval when due."
            return {"kind": "task_schedule", "schedule": entry.to_dict(), "reply": ack, "requires_approval": True}

        from ..assistant.proactive import ReminderStore

        store = ReminderStore(self.workspace)
        record = store.add(
            parsed.task,
            run_at=parsed.run_at,
            every_minutes=parsed.every_minutes,
            daily=parsed.daily,
            weekly=parsed.weekly,
            timezone=tz_name,
            now=ts,
        )
        ack = f"Reminder set: {parsed.summary}."
        return {"kind": "reminder", "reminder": record, "reply": ack, "requires_approval": False}

    # ── 3. Monitors (opt-in, threshold, cooldown, severity) ────────────── #

    def run_monitors(self, *, now: float | None = None, force: bool = False, present: bool | None = None) -> list[dict[str, Any]]:
        cfg = self._ensure_enabled()
        if not cfg.monitors.enabled:
            return []
        ts = float(self.clock() if now is None else now)
        results: list[dict[str, Any]] = []

        for spec in cfg.monitors.items:
            if not spec.enabled:
                continue
            state = self.store.read()
            prev = dict((state.get("monitor_states") or {}).get(spec.id) or {})
            last_checked = float(prev.get("last_checked_at") or 0.0)
            if not force and last_checked > 0 and (ts - last_checked) < spec.poll_seconds:
                continue

            try:
                outcome = _evaluate_single_monitor(
                    self.workspace,
                    spec,
                    http_checker=self.http_checker,
                    server_checker=self.server_checker,
                    ci_checker=self.ci_source,
                    system_sampler=self.system_sampler,
                    market_checker=self.market_checker,
                    news_source=self.news_source,
                )
            except Exception as exc:
                outcome = {"ok": False, "alert": True, "reason": type(exc).__name__, "summary": f"Monitor {spec.id} error ({type(exc).__name__})"}

            triggered = bool(outcome.get("alert"))
            cooldown_active = False
            notified: dict[str, Any] | None = None
            last_alert_at = float(prev.get("last_alert_at") or 0.0)
            was_in_alert = bool(prev.get("in_alert", False))

            if triggered:
                if was_in_alert and (ts - last_alert_at) < spec.cooldown_seconds:
                    cooldown_active = True
                else:
                    notified = self.channels.dispatch(
                        title=f"Monitor [{spec.severity.upper()}]: {spec.id}",
                        message=str(outcome.get("summary") or spec.id),
                        severity=spec.severity,
                        source=spec.kind,
                        present=present,
                        rate_limit_speech=True,
                        extra_hud_data={"monitor_id": spec.id, "kind": spec.kind, "outcome": outcome},
                        now=ts,
                        cfg=cfg,
                    )
                    last_alert_at = ts
            elif was_in_alert and spec.notify_on_recovery:
                notified = self.channels.dispatch(
                    title=f"Recovered: {spec.id}",
                    message=str(outcome.get("summary") or f"{spec.id} recovered"),
                    severity="info",
                    source=spec.kind,
                    present=present,
                    rate_limit_speech=False,
                    extra_hud_data={"monitor_id": spec.id, "kind": spec.kind, "recovered": True},
                    now=ts,
                    cfg=cfg,
                )

            def _update_mon(data: dict[str, Any], _spec=spec, _triggered=triggered, _last_alert=last_alert_at, _outcome=outcome) -> None:
                mstates = dict(data.get("monitor_states") or {})
                mstates[_spec.id] = {
                    "last_checked_at": ts,
                    "last_alert_at": _last_alert,
                    "in_alert": _triggered,
                    "last_severity": _spec.severity,
                    "last_outcome": _outcome,
                }
                data["monitor_states"] = mstates

            self.store.update(_update_mon)
            results.append({
                "id": spec.id,
                "kind": spec.kind,
                "severity": spec.severity,
                "outcome": outcome,
                "triggered": triggered,
                "cooldown_active": cooldown_active,
                "notification": notified,
            })

        return results

    # ── 4. Suggestions (one proposal, rate-limited, stop-suggesting) ───── #

    def evaluate_suggestion(
        self,
        *,
        calendar_events: list[dict[str, Any]] | None = None,
        tasks_due: list[dict[str, Any]] | None = None,
        failing_builds: list[dict[str, Any]] | None = None,
        present: bool | None = None,
        now: float | None = None,
        deliver: bool = True,
    ) -> dict[str, Any] | None:
        cfg = self._ensure_enabled()
        if not cfg.suggestions.enabled:
            return None
        ts = float(self.clock() if now is None else now)

        # Auto-gather signals when caller did not supply overrides
        if calendar_events is None and cfg.morning_brief.include_calendar:
            tz_name = effective_timezone(self.workspace, cfg)
            dt = datetime.fromtimestamp(ts, tz=ZoneInfo(tz_name))
            cal = self.brief_engine._gather_calendar(dt.isoformat(), (dt + timedelta(hours=4)).isoformat())
            calendar_events = cal.get("data") if cal.get("state") == "ok" else []
        if tasks_due is None and cfg.morning_brief.include_tasks:
            tk = self.brief_engine._gather_tasks(ts)
            tasks_due = tk.get("data") if tk.get("state") == "ok" else []
        if failing_builds is None:
            fb: list[dict[str, Any]] = []
            for mid, mst in (self.store.read().get("monitor_states") or {}).items():
                out = (mst or {}).get("last_outcome") or {}
                if mst.get("in_alert") and out.get("repo"):
                    fb.append({"id": mid, "repo": out["repo"]})
            if not fb and cfg.morning_brief.include_ci and (self.ci_source is not None or cfg.morning_brief.ci_repo):
                sys_ci = self.brief_engine._gather_system_and_ci(cfg)
                ci = ((sys_ci.get("data") or {}).get("ci") or {})
                if ci.get("failing"):
                    fb.append({"id": "ci", "repo": ci.get("repo") or "workspace"})
            failing_builds = fb

        suggestion = self.suggestions.evaluate(
            calendar_events=calendar_events,
            tasks_due=tasks_due,
            failing_builds=failing_builds,
            cfg=cfg,
            now=ts,
        )
        if suggestion is None:
            return None

        if deliver:
            delivery = self.channels.dispatch(
                title=suggestion["title"],
                message=suggestion["message"],
                spoken_text=suggestion["message"],
                severity=suggestion["severity"],
                source="suggestion",
                present=present,
                rate_limit_speech=True,
                extra_hud_data={"suggestion": suggestion},
                now=ts,
                cfg=cfg,
            )
            suggestion["delivery"] = delivery
        return suggestion

    def accept_suggestion(self, suggestion_id: str = "") -> dict[str, Any]:
        """Convert a proposed suggestion into a normal queued item (strictly awaiting approval under L1)."""
        self._ensure_enabled()
        state = self.store.read()
        target = state.get("last_suggestion") or {}
        if not target or (suggestion_id and target.get("id") != suggestion_id):
            raise ProactiveError("No matching active suggestion to queue")
        action = target.get("proposed_action") or {}
        task_text = str(action.get("task") or target.get("title") or "").strip()
        if not task_text:
            raise ProactiveError("Suggestion has no actionable proposal")
        if self.studio is None:
            from ..server import ServerState

            self.studio = ServerState(self.workspace, quiet=True).workbench.studio
        from .queue import Queue

        queued = Queue(self.studio).enqueue(task_text, source="event:user")
        return {"queued": True, "job": queued, "requires_approval": queued.get("status") == "awaiting_approval"}

    # ── 5. Due Reminders + Voice Command Handling + Daemon Tick ────────── #

    def fire_due_reminders(self, *, now: float | None = None, present: bool | None = None) -> list[dict[str, Any]]:
        cfg = self._ensure_enabled()
        ts = float(self.clock() if now is None else now)
        from ..assistant.proactive import ReminderStore

        store = ReminderStore(self.workspace)
        fired: list[dict[str, Any]] = []
        for row in store.due(ts):
            delivery = self.channels.dispatch(
                title="Reminder",
                message=row["text"],
                spoken_text=f"Reminder: {row['text']}",
                severity="medium",
                source="reminder",
                present=present,
                rate_limit_speech=True,
                extra_hud_data={"reminder_id": row["id"]},
                now=ts,
                cfg=cfg,
            )
            store.mark_fired(row["id"], ts)
            fired.append({"id": row["id"], "text": row["text"], "delivery": delivery})
        return fired

    def handle_voice_utterance(self, text: str, *, present: bool = True, now: float | None = None) -> dict[str, Any] | None:
        """Handle proactive voice intents if enabled in ``proactive.yaml``; returns None when not matched or disabled."""
        cfg = self.config()
        if not cfg.enabled:
            return None
        intent = detect_proactive_voice_intent(text)
        if intent is None:
            return None
        kind, params = intent
        ts = float(self.clock() if now is None else now)

        if kind == "morning_brief":
            if not cfg.morning_brief.enabled or not cfg.morning_brief.deliver_on_greeting:
                return None
            res = self.run_morning_brief(trigger="greeting", present=present, now=ts, force=True)
            return {"intent": "morning_brief", "reply": res["brief"]["spoken_text"], "hud_text": res["brief"]["hud_text"], "result": res}

        if kind == "focus_mode":
            if not cfg.dnd.enabled:
                return None
            active = bool(params.get("active", True))
            mins = params.get("minutes")
            status = self.dnd.set_focus_mode(active, minutes=mins, reason="voice_command", now=ts)
            reply = (
                f"Focus mode enabled{f' for {mins} minutes' if mins else ''}. Non-urgent alerts paused."
                if active
                else "Focus mode disabled. Notifications resumed."
            )
            return {"intent": "focus_mode", "reply": reply, "focus": status}

        if kind == "stop_suggesting":
            if not cfg.suggestions.enabled:
                return None
            learned = self.suggestions.learn_stop_suggesting(params.get("topic") or "", now=ts)
            topic_label = learned["suppression"].get("topic") or "that"
            return {"intent": "stop_suggesting", "reply": f"Got it. I will stop suggesting {topic_label}.", "result": learned}

        if kind in {"remind", "recurring_task"}:
            if not cfg.voice_reminders.enabled:
                return None
            created = self.create_from_voice(text, now=ts, as_task=(kind == "recurring_task"))
            return {"intent": kind, "reply": created["reply"], "result": created}

        return None

    def tick_once(self, *, now: float | None = None, present: bool | None = None) -> dict[str, Any]:
        """Run one bounded proactive maintenance tick (called by Daemon.tick_once or CLI/API)."""
        cfg = self.config()
        if not cfg.enabled:
            return {"enabled": False, "skipped": True}
        # Respect L0 policy and emergency stop at all times.
        try:
            from ..computer_control import EmergencyStop

            if EmergencyStop().stopped() or load_autonomy_dict(self.workspace).get("level") == "L0":
                return {"enabled": True, "skipped": True, "reason": "L0_or_emergency_stop"}
        except Exception:
            pass

        ts = float(self.clock() if now is None else now)
        brief_res = None
        if cfg.morning_brief.enabled and cfg.morning_brief.deliver_on_schedule:
            try:
                brief_res = self.run_morning_brief(trigger="schedule", present=present, now=ts, force=False)
            except Exception as exc:
                brief_res = {"error": type(exc).__name__}

        reminders_fired = self.fire_due_reminders(now=ts, present=present)
        monitors_out = self.run_monitors(now=ts, force=False, present=present)

        failing = [
            {"id": m["id"], "repo": m["outcome"].get("repo") or m["id"]}
            for m in monitors_out
            if m.get("triggered") and m.get("kind") in {"github_ci"}
        ]
        suggestion_res = self.evaluate_suggestion(
            failing_builds=failing if failing else None,
            present=present,
            now=ts,
            deliver=True,
        )
        return {
            "enabled": True,
            "skipped": False,
            "brief": brief_res,
            "reminders_fired": reminders_fired,
            "monitors": monitors_out,
            "suggestion": suggestion_res,
            "focus": self.dnd.focus_status(now=ts),
        }

    def status(self, *, now: float | None = None) -> dict[str, Any]:
        cfg = self.config()
        ts = float(self.clock() if now is None else now)
        state = self.store.read()
        from ..assistant.proactive import ReminderStore

        return {
            "schema": 1,
            "enabled": cfg.enabled,
            "timezone": effective_timezone(self.workspace, cfg),
            "config": cfg.to_dict(),
            "focus": self.dnd.focus_status(now=ts),
            "dnd_schedule_active": self.dnd.schedule_active(cfg=cfg, now=ts)[0],
            "speech_last_hour": self.channels.proactive_speech_count_last_hour(now=ts),
            "max_speech_per_hour": cfg.suggestions.max_speech_per_hour,
            "last_brief": state.get("last_brief"),
            "last_suggestion": state.get("last_suggestion"),
            "suppressed_suggestions": list(state.get("suppressed_suggestions") or []),
            "hud_toasts": list(state.get("hud_toasts") or [])[:30],
            "monitor_states": dict(state.get("monitor_states") or {}),
            "reminders": ReminderStore(self.workspace).list(),
        }

    def ack_toast(self, toast_id: str) -> bool:
        found = [False]

        def _mut(data: dict[str, Any]) -> None:
            rows = list(data.get("hud_toasts") or [])
            for row in rows:
                if row.get("id") == toast_id:
                    row["read"] = True
                    found[0] = True
            data["hud_toasts"] = rows

        self.store.update(_mut)
        return found[0]

    def ack_hud_toast(self, toast_id: str) -> dict[str, Any]:
        return {"id": toast_id, "acknowledged": self.ack_toast(toast_id)}

    def set_focus_mode(
        self,
        active: bool,
        *,
        minutes: int | None = None,
        reason: str = "manual",
        now: float | None = None,
    ) -> dict[str, Any]:
        self._ensure_enabled()
        focus = self.dnd.set_focus_mode(active, minutes=minutes, reason=reason, now=now)
        return {"focus": focus, "enabled": True}

    def learn_stop_suggesting(self, topic: str = "", *, now: float | None = None) -> dict[str, Any]:
        self._ensure_enabled()
        return self.suggestions.learn_stop_suggesting(topic, now=now)

    def unmute_suggestion(self, ident_or_topic: str) -> dict[str, Any]:
        self._ensure_enabled()
        return {"unmuted": self.suggestions.unmute(ident_or_topic), "target": ident_or_topic}

    def evaluate_suggestions(
        self,
        *,
        calendar_events: list[dict[str, Any]] | None = None,
        tasks_due: list[dict[str, Any]] | None = None,
        failing_builds: list[dict[str, Any]] | None = None,
        present: bool | None = None,
        now: float | None = None,
        deliver: bool = True,
    ) -> dict[str, Any]:
        proposed = self.evaluate_suggestion(
            calendar_events=calendar_events,
            tasks_due=tasks_due,
            failing_builds=failing_builds,
            present=present,
            now=now,
            deliver=deliver,
        )
        return {"proposed": proposed}


__all__ = [
    "ChannelsPolicy",
    "DNDManager",
    "DNDPolicy",
    "DNDSchedulePolicy",
    "MONITOR_KINDS",
    "MonitorSpec",
    "MonitorsPolicy",
    "MorningBriefEngine",
    "MorningBriefPolicy",
    "PROACTIVE_ALLOWED_KEYS",
    "ProactiveChannelRouter",
    "ProactiveConfig",
    "ProactiveDisabledError",
    "ProactiveEngine",
    "ProactiveError",
    "ProactiveStateStore",
    "ParsedVoiceReminder",
    "SEVERITY_LEVELS",
    "SEVERITY_RANK",
    "SuggestionEngine",
    "SuggestionsPolicy",
    "UrgentOverridePolicy",
    "VALID_CHANNELS",
    "VoiceRemindersPolicy",
    "detect_proactive_voice_intent",
    "effective_timezone",
    "load_proactive_config",
    "parse_voice_reminder",
]
