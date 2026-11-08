"""Timezone-aware recurring and delayed mission/job scheduler (cron + English/Bangla NL)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from filelock import FileLock

from .autonomy import load_autonomy_dict, resolve_timezone
from .config import nexus_home
from .github_safety import scan_text
from .studio import redact

MAX_SCHEDULES_LIMIT = 500
MAX_EXPR_CHARS = 500
MAX_TASK_CHARS = 8000

_BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

_CRON_MACROS: dict[str, str] = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}

# Python datetime.weekday(): Monday=0 .. Sunday=6
_WEEKDAY_MAP: dict[str, int] = {
    "mon": 0, "monday": 0, "সোম": 0, "সোমবার": 0,
    "tue": 1, "tues": 1, "tuesday": 1, "মঙ্গল": 1, "মঙ্গলবার": 1,
    "wed": 2, "wednesday": 2, "বুধ": 2, "বুধবার": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "বৃহস্পতি": 3, "বৃহস্পতিবার": 3,
    "fri": 4, "friday": 4, "শুক্র": 4, "শুক্রবার": 4,
    "sat": 5, "saturday": 5, "শনি": 5, "শনিবার": 5,
    "sun": 6, "sunday": 6, "রবি": 6, "রবিবার": 6, "রোববার": 6,
}

# Cron DOW: Sunday=0 (and 7), Monday=1 .. Saturday=6
def _py_weekday_to_cron(py_wd: int) -> int:
    return (py_wd + 1) % 7


_CRON_DOW_ALIASES: dict[str, int] = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}
_CRON_MON_ALIASES: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_TIME_PERIOD_DEFAULT_HOUR: dict[str, int] = {
    "morning": 8, "সকাল": 8, "ভোর": 6,
    "noon": 12, "দুপুর": 13,
    "afternoon": 15, "বিকাল": 16, "বিকেল": 16,
    "evening": 18, "সন্ধ্যা": 18,
    "night": 21, "রাত": 21,
}


def normalize_bangla_digits(text: str) -> str:
    return (text or "").translate(_BANGLA_DIGITS)


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone: {tz_name!r}") from exc


def _parse_cron_field(
    raw: str,
    low: int,
    high: int,
    *,
    aliases: dict[str, int] | None = None,
    allow_seven_as_zero: bool = False,
) -> set[int]:
    if not raw:
        raise ValueError("Empty cron field")
    result: set[int] = set()
    aliases = aliases or {}

    def _val(tok: str) -> int:
        t = tok.strip().casefold()
        if t in aliases:
            v = aliases[t]
        else:
            if not re.fullmatch(r"\d+", t):
                raise ValueError(f"Invalid cron token: {tok!r}")
            v = int(t)
        if allow_seven_as_zero and v == 7:
            v = 0
        if not low <= v <= high:
            raise ValueError(f"Cron value {v} out of range {low}..{high}")
        return v

    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise ValueError("Empty comma item in cron field")
        if "/" in part:
            base, step_s = part.split("/", 1)
            if not re.fullmatch(r"\d+", step_s) or int(step_s) <= 0:
                raise ValueError(f"Invalid cron step: {part!r}")
            step = int(step_s)
        else:
            base, step = part, 1

        if base == "*":
            start, end = low, high
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = _val(a), _val(b)
            if start > end:
                raise ValueError(f"Invalid cron range {part!r}")
        else:
            start = _val(base)
            end = high if "/" in part else start

        for num in range(start, end + 1, step):
            result.add(0 if (allow_seven_as_zero and num == 7) else num)
    if not result:
        raise ValueError(f"Cron field produced empty set: {raw!r}")
    return result


@dataclass(frozen=True)
class CronSpec:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    dom_wildcard: bool
    dow_wildcard: bool

    @classmethod
    def parse(cls, expr: str) -> CronSpec:
        cleaned = " ".join((expr or "").strip().split())
        if cleaned.casefold() in _CRON_MACROS:
            cleaned = _CRON_MACROS[cleaned.casefold()]
        parts = cleaned.split(" ")
        if len(parts) != 5:
            raise ValueError(f"Cron expression must have 5 fields: {expr!r}")
        m_s, h_s, dom_s, mon_s, dow_s = parts
        return cls(
            expr=cleaned,
            minutes=frozenset(_parse_cron_field(m_s, 0, 59)),
            hours=frozenset(_parse_cron_field(h_s, 0, 23)),
            days=frozenset(_parse_cron_field(dom_s, 1, 31)),
            months=frozenset(_parse_cron_field(mon_s, 1, 12, aliases=_CRON_MON_ALIASES)),
            weekdays=frozenset(
                _parse_cron_field(dow_s, 0, 7, aliases=_CRON_DOW_ALIASES, allow_seven_as_zero=True)
            ),
            dom_wildcard=(dom_s == "*"),
            dow_wildcard=(dow_s == "*"),
        )

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minutes or dt.hour not in self.hours or dt.month not in self.months:
            return False
        dom_match = dt.day in self.days
        cron_wd = _py_weekday_to_cron(dt.weekday())
        dow_match = cron_wd in self.weekdays
        if not self.dom_wildcard and not self.dow_wildcard:
            return dom_match or dow_match
        return dom_match and dow_match

    def next_after(self, after_ts: float, tz_name: str) -> float:
        tz = _zone(tz_name)
        base = datetime.fromtimestamp(after_ts, tz=tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit_dt = base + timedelta(days=366 * 2)
        cur = base
        while cur <= limit_dt:
            if cur.month not in self.months:
                # Jump to first day of next month
                if cur.month == 12:
                    cur = cur.replace(year=cur.year + 1, month=1, day=1, hour=0, minute=0)
                else:
                    cur = cur.replace(month=cur.month + 1, day=1, hour=0, minute=0)
                continue
            dom_ok = cur.day in self.days
            dow_ok = _py_weekday_to_cron(cur.weekday()) in self.weekdays
            day_ok = (dom_ok or dow_ok) if (not self.dom_wildcard and not self.dow_wildcard) else (dom_ok and dow_ok)
            if not day_ok:
                cur = (cur + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if cur.hour not in self.hours:
                cur = (cur + timedelta(hours=1)).replace(minute=0)
                continue
            if cur.minute not in self.minutes:
                cur = cur + timedelta(minutes=1)
                continue
            return cur.timestamp()
        raise ValueError(f"Could not find next occurrence within 2 years for cron {self.expr!r}")


@dataclass(frozen=True)
class ParsedSchedule:
    kind: str  # "cron" | "once" | "interval"
    timezone: str
    task: str
    cron_expr: str = ""
    interval_seconds: int = 0
    run_at: float | None = None
    next_run_at: float = 0.0
    summary: str = ""


def _adjust_hour(hour: int, ampm: str | None, period: str | None) -> int:
    if not 0 <= hour <= 23:
        raise ValueError(f"Hour out of range: {hour}")
    if ampm:
        marker = ampm.casefold()
        if marker == "am":
            return 0 if hour == 12 else hour
        if marker == "pm":
            return 12 if hour == 12 else (hour + 12 if hour < 12 else hour)
    if period:
        p = period.casefold()
        if p in {"morning", "সকাল", "ভোর"}:
            return 0 if hour == 12 else hour
        if p in {"noon", "দুপুর"}:
            if 1 <= hour <= 4:
                return hour + 12
            return hour
        if p in {"afternoon", "evening", "বিকাল", "বিকেল", "সন্ধ্যা"}:
            if 1 <= hour <= 9:
                return hour + 12
            return hour
        if p in {"night", "রাত"}:
            if hour == 12:
                return 0
            if 6 <= hour <= 11:
                return hour + 12
            return hour
    return hour


def _extract_time_of_day(text: str, *, default_hour: int = 9, default_minute: int = 0) -> tuple[int, int, str]:
    """Extract optional time-of-day (English or Bangla) and return ``(hour, minute, remainder)``."""
    working = text

    # Explicit period + time in Bangla: e.g., "সকাল ৭টা ৩০", "বিকাল ৩টায়", "রাত 10:15", "সকাল 7"
    bn_pat = re.compile(
        r"(?:(?P<period>ভোর|সকাল|দুপুর|বিকাল|বিকেল|সন্ধ্যা|রাত)\s*)?"
        r"(?P<hour>\d{1,2})"
        r"(?:\s*(?:[:.])\s*(?P<min_colon>\d{2})|\s*(?:টার\s*সময়|টায়|টা|বাজে)(?:\s*(?P<min_bn>\d{1,2})\s*(?:মিনিটে?)?)?)"
        r"(?:\s*(?P<ampm>am|pm))?",
        re.I,
    )
    en_at_pat = re.compile(
        r"\b(?:at|@)\s*(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?\b"
        r"(?:\s+(?:in\s+the\s+)?(?P<period>morning|afternoon|evening|night))?",
        re.I,
    )
    en_period_pat = re.compile(
        r"\b(?:in\s+the\s+)?(?P<period>morning|noon|afternoon|evening|night)\b",
        re.I,
    )
    bn_period_only_pat = re.compile(r"(?P<period>ভোর|সকাল|দুপুর|বিকাল|বিকেল|সন্ধ্যা|রাত)(?:\s*বেলা)?")

    m = en_at_pat.search(working)
    if m:
        hour = int(m.group("hour"))
        minute = int(m.group("minute") or 0)
        if not 0 <= minute <= 59:
            raise ValueError(f"Minute out of range: {minute}")
        hour = _adjust_hour(hour, m.group("ampm"), m.group("period"))
        remainder = (working[: m.start()] + " " + working[m.end() :]).strip()
        return hour, minute, remainder

    m = bn_pat.search(working)
    if m and (m.group("period") or m.group("min_colon") is not None or "টা" in m.group(0) or "বাজে" in m.group(0) or m.group("ampm")):
        hour = int(m.group("hour"))
        minute_raw = m.group("min_colon") or m.group("min_bn") or "0"
        minute = int(minute_raw)
        if not 0 <= minute <= 59:
            raise ValueError(f"Minute out of range: {minute}")
        hour = _adjust_hour(hour, m.group("ampm"), m.group("period"))
        remainder = (working[: m.start()] + " " + working[m.end() :]).strip()
        return hour, minute, remainder

    m = en_period_pat.search(working)
    if m:
        p = m.group("period").casefold()
        remainder = (working[: m.start()] + " " + working[m.end() :]).strip()
        return _TIME_PERIOD_DEFAULT_HOUR[p], 0, remainder

    m = bn_period_only_pat.search(working)
    if m:
        p = m.group("period")
        remainder = (working[: m.start()] + " " + working[m.end() :]).strip()
        return _TIME_PERIOD_DEFAULT_HOUR[p], 0, remainder

    return default_hour, default_minute, working


def _clean_task_phrase(raw: str, *, fallback_task: str = "") -> str:
    text = (fallback_task or raw or "").strip()
    if not fallback_task:
        text = re.sub(r"^[\s,:\-–—]+", "", text)
        text = re.sub(r"^(?:to|that|then|and|please|দয়া\s*করে)\s+", "", text, flags=re.I)
        # Preserve meaningful phrasing like "run the market report" or "remind me"; strip bare trailing Bangla triggers only when prefix existed
        text = re.sub(r"[\s,:\-–—]+$", "", text)
    return " ".join(text.split())


def parse_schedule_expression(
    expression: str,
    *,
    task: str = "",
    timezone: str = "UTC",
    now: float | None = None,
) -> ParsedSchedule:
    """Parse cron expressions as well as English and Bangla recurring/delayed phrases."""
    tz_name = timezone or "UTC"
    tz = _zone(tz_name)
    now_ts = time.time() if now is None else float(now)
    now_dt = datetime.fromtimestamp(now_ts, tz=tz)

    raw_orig = (expression or "").strip()
    if not raw_orig or len(raw_orig) > MAX_EXPR_CHARS:
        raise ValueError(f"Schedule expression must be 1–{MAX_EXPR_CHARS} characters")
    norm = normalize_bangla_digits(" ".join(raw_orig.split()))

    # 1. Pure cron or "<5-field cron / @macro> <task>"
    macro_m = re.match(r"^(@(?:hourly|daily|midnight|weekly|monthly))(?:\s+(.+))?$", norm, re.I)
    if macro_m:
        cron_spec = CronSpec.parse(macro_m.group(1))
        resolved_task = _clean_task_phrase(macro_m.group(2) or "", fallback_task=task)
        if not resolved_task:
            raise ValueError("Schedule task is required")
        nxt = cron_spec.next_after(now_ts, tz_name)
        return ParsedSchedule(
            kind="cron",
            timezone=tz_name,
            task=resolved_task,
            cron_expr=cron_spec.expr,
            next_run_at=nxt,
            summary=f"cron '{cron_spec.expr}' ({tz_name})",
        )

    cron_tokens = norm.split(" ", 5)
    if len(cron_tokens) >= 5 and all(re.fullmatch(r"[0-9A-Za-z*,/-]+", t) for t in cron_tokens[:5]):
        maybe_cron = " ".join(cron_tokens[:5])
        try:
            cron_spec = CronSpec.parse(maybe_cron)
            trailing = cron_tokens[5] if len(cron_tokens) == 6 else ""
            resolved_task = _clean_task_phrase(trailing, fallback_task=task)
            if not resolved_task:
                raise ValueError("Schedule task is required")
            nxt = cron_spec.next_after(now_ts, tz_name)
            return ParsedSchedule(
                kind="cron",
                timezone=tz_name,
                task=resolved_task,
                cron_expr=cron_spec.expr,
                next_run_at=nxt,
                summary=f"cron '{cron_spec.expr}' ({tz_name})",
            )
        except ValueError:
            pass

    # 2. Interval: "every N minutes/hours/days ..." or Bangla "প্রতি N মিনিট/ঘণ্টা পর ..."
    m = re.match(
        r"^(?:every\s+(?:(\d+)\s+)?(minute|minutes|min|hour|hours|hr|hrs|day|days)\b|"
        r"প্রতি\s+(?:(\d+)\s+)?(মিনিট|মিনিটে|মিনিট\s*পর|ঘণ্টা|ঘন্টা|ঘণ্টায়|দিন)\s*(?:পর)?)\s*(.*)$",
        norm,
        re.I,
    )
    if m:
        en_n, en_unit, bn_n, bn_unit, rest = m.groups()
        count_s = en_n or bn_n or "1"
        count = int(count_s)
        unit = (en_unit or bn_unit or "").casefold()
        if count <= 0:
            raise ValueError("Interval count must be positive")
        if unit.startswith(("minute", "min")) or "মিনিট" in unit:
            interval = count * 60
        elif unit.startswith(("hour", "hr")) or "ঘণ্ট" in unit or "ঘন্ট" in unit:
            interval = count * 3600
        else:
            # Daily without time -> 24h interval or cron at current hour? If rest has "at ...", handle in daily branch!
            interval = 0
        if interval > 0 and not re.search(r"(?:\bat\b|\d+\s*(?:টা|বাজে|:))", rest, re.I):
            if interval < 60 or interval > 30 * 86400:
                raise ValueError("Recurring interval must be between 60 seconds and 30 days")
            resolved_task = _clean_task_phrase(rest, fallback_task=task)
            if not resolved_task:
                raise ValueError("Schedule task is required")
            nxt = now_ts + interval
            return ParsedSchedule(
                kind="interval",
                timezone=tz_name,
                task=resolved_task,
                interval_seconds=interval,
                next_run_at=nxt,
                summary=f"every {interval}s ({tz_name})",
            )

    # 3. Relative delay: "in N minutes/hours/days ..." or Bangla "N মিনিট/ঘণ্টা/দিন পর ..."
    m_rel = re.match(
        r"^(?:in\s+(\d+)\s+(minute|minutes|min|hour|hours|hr|hrs|day|days)\b|"
        r"(\d+)\s*(মিনিট|ঘণ্টা|ঘন্টা|দিন)\s*(?:পর|পরে))\s*(.*)$",
        norm,
        re.I,
    )
    if m_rel:
        en_n, en_unit, bn_n, bn_unit, rest = m_rel.groups()
        count = int(en_n or bn_n)
        if count <= 0:
            raise ValueError("Delay count must be positive")
        unit = (en_unit or bn_unit or "").casefold()
        if unit.startswith(("minute", "min")) or "মিনিট" in unit:
            delta = timedelta(minutes=count)
        elif unit.startswith(("hour", "hr")) or "ঘণ্ট" in unit or "ঘন্ট" in unit:
            delta = timedelta(hours=count)
        else:
            delta = timedelta(days=count)
        resolved_task = _clean_task_phrase(rest, fallback_task=task)
        if not resolved_task:
            raise ValueError("Schedule task is required")
        target = now_dt + delta
        return ParsedSchedule(
            kind="once",
            timezone=tz_name,
            task=resolved_task,
            run_at=target.timestamp(),
            next_run_at=target.timestamp(),
            summary=f"once at {target.isoformat()} ({tz_name})",
        )

    # 4. Daily recurring: "every day ...", "daily ...", "every morning/evening/night ...", "প্রতিদিন ...", "রোজ ..."
    m_daily = re.match(
        r"^(?:every\s+day|daily|every\s+(?=morning|afternoon|evening|night)|প্রতি\s*দিন|রোজ)\b\s*(.*)$",
        norm,
        re.I,
    )
    if m_daily:
        rest = m_daily.group(1)
        hour, minute, remainder = _extract_time_of_day(rest, default_hour=9, default_minute=0)
        resolved_task = _clean_task_phrase(remainder, fallback_task=task)
        if not resolved_task:
            raise ValueError("Schedule task is required")
        expr = f"{minute} {hour} * * *"
        cron_spec = CronSpec.parse(expr)
        nxt = cron_spec.next_after(now_ts, tz_name)
        return ParsedSchedule(
            kind="cron",
            timezone=tz_name,
            task=resolved_task,
            cron_expr=expr,
            next_run_at=nxt,
            summary=f"daily at {hour:02d}:{minute:02d} ({tz_name})",
        )

    # 5. Weekly recurring: "every Monday ...", "প্রতি শুক্রবার ..."
    wd_alt = "|".join(sorted((re.escape(k) for k in _WEEKDAY_MAP), key=len, reverse=True))
    m_weekly = re.match(
        rf"^(?:every|প্রতি(?:\s*সপ্তাহের)?)\s+(?P<wd>{wd_alt})(?:বার)?\s*(?P<rest>.*)$",
        norm,
        re.I,
    )
    if m_weekly:
        wd_tok = m_weekly.group("wd").casefold()
        py_wd = _WEEKDAY_MAP[wd_tok]
        cron_wd = _py_weekday_to_cron(py_wd)
        hour, minute, remainder = _extract_time_of_day(m_weekly.group("rest"), default_hour=9, default_minute=0)
        resolved_task = _clean_task_phrase(remainder, fallback_task=task)
        if not resolved_task:
            raise ValueError("Schedule task is required")
        expr = f"{minute} {hour} * * {cron_wd}"
        cron_spec = CronSpec.parse(expr)
        nxt = cron_spec.next_after(now_ts, tz_name)
        return ParsedSchedule(
            kind="cron",
            timezone=tz_name,
            task=resolved_task,
            cron_expr=expr,
            next_run_at=nxt,
            summary=f"weekly ({expr}, {tz_name})",
        )

    # 6. Delayed one-shot weekday: "next Friday remind me", "আগামী শুক্রবার বিকাল ৩টায় ..."
    m_next_wd = re.match(
        rf"^(?:(?:on|this|next)\s+|আগামী\s+|পরের\s+)(?P<wd>{wd_alt})(?:বার)?\s*(?P<rest>.*)$",
        norm,
        re.I,
    )
    if m_next_wd:
        wd_tok = m_next_wd.group("wd").casefold()
        target_wd = _WEEKDAY_MAP[wd_tok]
        hour, minute, remainder = _extract_time_of_day(m_next_wd.group("rest"), default_hour=9, default_minute=0)
        resolved_task = _clean_task_phrase(remainder, fallback_task=task)
        if not resolved_task:
            raise ValueError("Schedule task is required")
        delta_days = (target_wd - now_dt.weekday()) % 7
        if delta_days == 0:
            candidate = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if "next" in raw_orig.casefold() or "আগামী" in raw_orig or "পরের" in raw_orig or candidate <= now_dt:
                delta_days = 7
        target_dt = (now_dt + timedelta(days=delta_days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return ParsedSchedule(
            kind="once",
            timezone=tz_name,
            task=resolved_task,
            run_at=target_dt.timestamp(),
            next_run_at=target_dt.timestamp(),
            summary=f"once at {target_dt.isoformat()} ({tz_name})",
        )

    # 7. Today / tomorrow: "tomorrow at 9am ...", "আগামীকাল সকাল ৭টায় ...", "today at 17:30 ...", "আজ বিকাল ৫টায় ..."
    m_day = re.match(
        r"^(?P<day>today|tomorrow|আগামীকাল|কাল|আজ)(?:কে|ের)?\s*(?P<rest>.*)$",
        norm,
        re.I,
    )
    if m_day:
        day_tok = m_day.group("day").casefold()
        is_tomorrow = day_tok in {"tomorrow", "আগামীকাল", "কাল"}
        hour, minute, remainder = _extract_time_of_day(m_day.group("rest"), default_hour=9, default_minute=0)
        resolved_task = _clean_task_phrase(remainder, fallback_task=task)
        if not resolved_task:
            raise ValueError("Schedule task is required")
        base_day = now_dt + timedelta(days=1 if is_tomorrow else 0)
        target_dt = base_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not is_tomorrow and target_dt <= now_dt:
            raise ValueError("Scheduled time today has already passed")
        return ParsedSchedule(
            kind="once",
            timezone=tz_name,
            task=resolved_task,
            run_at=target_dt.timestamp(),
            next_run_at=target_dt.timestamp(),
            summary=f"once at {target_dt.isoformat()} ({tz_name})",
        )

    # 8. Explicit ISO date: "on 2026-11-20 at 09:30 ..." or "2026-11-20 09:30 ..."
    m_iso = re.match(
        r"^(?:on\s+)?(?P<date>\d{4}-\d{2}-\d{2})(?:\s+(?:at\s+)?(?P<time>\d{1,2}:\d{2})(?:\s*(?P<ampm>am|pm))?)?\s*(?P<rest>.*)$",
        norm,
        re.I,
    )
    if m_iso:
        year, month, day = (int(p) for p in m_iso.group("date").split("-"))
        if m_iso.group("time"):
            h_s, min_s = m_iso.group("time").split(":")
            hour = _adjust_hour(int(h_s), m_iso.group("ampm"), None)
            minute = int(min_s)
        else:
            hour, minute, rest_after = _extract_time_of_day(m_iso.group("rest"), default_hour=9, default_minute=0)
            m_iso_rest = rest_after
        if m_iso.group("time"):
            m_iso_rest = m_iso.group("rest")
        target_dt = datetime(year, month, day, hour, minute, tzinfo=tz)
        resolved_task = _clean_task_phrase(m_iso_rest, fallback_task=task)
        if not resolved_task:
            raise ValueError("Schedule task is required")
        return ParsedSchedule(
            kind="once",
            timezone=tz_name,
            task=resolved_task,
            run_at=target_dt.timestamp(),
            next_run_at=target_dt.timestamp(),
            summary=f"once at {target_dt.isoformat()} ({tz_name})",
        )

    raise ValueError(f"Could not parse schedule expression: {expression!r}")


@dataclass
class ScheduleEntry:
    id: str
    expression: str
    task: str
    kind: str  # cron | once | interval
    timezone: str
    target: str = "queue"  # queue | chief
    cron_expr: str = ""
    interval_seconds: int = 0
    run_at: float | None = None
    next_run_at: float = 0.0
    last_run_at: float | None = None
    last_fired_slot: int | None = None
    last_dispatched_id: str = ""
    run_count: int = 0
    enabled: bool = True
    priority: int = 5
    mission: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "expression": self.expression,
            "task": self.task,
            "kind": self.kind,
            "timezone": self.timezone,
            "target": self.target,
            "cron_expr": self.cron_expr,
            "interval_seconds": self.interval_seconds,
            "run_at": self.run_at,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "last_fired_slot": self.last_fired_slot,
            "last_dispatched_id": self.last_dispatched_id,
            "run_count": self.run_count,
            "enabled": self.enabled,
            "priority": self.priority,
            "mission": self.mission,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduleEntry:
        return cls(
            id=str(data["id"]),
            expression=str(data.get("expression", "")),
            task=str(data.get("task", "")),
            kind=str(data.get("kind", "once")),
            timezone=str(data.get("timezone", "UTC")),
            target=str(data.get("target", "queue")),
            cron_expr=str(data.get("cron_expr", "")),
            interval_seconds=int(data.get("interval_seconds", 0) or 0),
            run_at=data.get("run_at"),
            next_run_at=float(data.get("next_run_at", 0.0) or 0.0),
            last_run_at=data.get("last_run_at"),
            last_fired_slot=data.get("last_fired_slot"),
            last_dispatched_id=str(data.get("last_dispatched_id", "")),
            run_count=int(data.get("run_count", 0) or 0),
            enabled=bool(data.get("enabled", True)),
            priority=int(data.get("priority", 5)),
            mission=data.get("mission"),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
        )


def compute_next_occurrence(entry: ScheduleEntry, *, after_ts: float) -> float | None:
    if entry.kind == "once":
        return None
    if entry.kind == "interval":
        step = max(60, entry.interval_seconds)
        base = entry.next_run_at if entry.next_run_at > 0 else after_ts
        nxt = base + step
        while nxt <= after_ts:
            nxt += step
        return nxt
    if entry.kind == "cron":
        return CronSpec.parse(entry.cron_expr).next_after(after_ts, entry.timezone)
    return None


class Scheduler:
    """Persistent timezone-aware scheduler storing workspace schedules in ``$NEXUS_HOME``."""

    def __init__(self, workspace: Path | str, *, studio: Any = None, clock: Any = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.studio = studio
        self.clock = clock or time.time
        ws_hash = hashlib.sha256(str(self.workspace).encode("utf-8")).hexdigest()[:32]
        self.dir = nexus_home().resolve() / "scheduler" / ws_hash
        self.path = self.dir / "schedules.json"
        self.lock_path = self.dir / "schedules.lock"

    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise ValueError("Unsafe scheduler storage path")

    def _read_all(self) -> list[ScheduleEntry]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("schedules"), list):
            return []
        return [ScheduleEntry.from_dict(item) for item in raw["schedules"] if isinstance(item, dict)]

    def _write_all(self, entries: list[ScheduleEntry]) -> None:
        self._ensure_dir()
        payload = {
            "schema": 1,
            "updated_at": self.clock(),
            "schedules": [e.to_dict() for e in entries],
        }
        tmp = self.dir / f".schedules.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def list(self) -> list[ScheduleEntry]:
        self._ensure_dir()
        with FileLock(str(self.lock_path), timeout=5):
            entries = self._read_all()
        return sorted(entries, key=lambda e: (not e.enabled, e.next_run_at or float("inf"), e.created_at))

    def get(self, schedule_id: str) -> ScheduleEntry:
        for entry in self.list():
            if entry.id == schedule_id:
                return entry
        raise ValueError(f"Unknown schedule '{schedule_id}'")

    def add(
        self,
        expression: str,
        *,
        task: str = "",
        timezone: str | None = None,
        target: str | None = None,
        priority: int = 5,
        mission: str | None = None,
    ) -> ScheduleEntry:
        policy = load_autonomy_dict(self.workspace)
        tz_name = resolve_timezone(self.workspace, explicit=timezone)
        now_ts = float(self.clock())
        parsed = parse_schedule_expression(expression, task=task, timezone=tz_name, now=now_ts)
        if not parsed.task or len(parsed.task) > MAX_TASK_CHARS:
            raise ValueError(f"Scheduled task must be 1–{MAX_TASK_CHARS} characters")
        scan_text(parsed.task, workspace=self.workspace)
        if str(redact(parsed.task)) != parsed.task:
            raise ValueError("Scheduled task must not contain secrets")
        eff_target = target or str((policy.get("scheduler") or {}).get("default_target") or "queue")
        if eff_target not in {"queue", "chief"}:
            raise ValueError("Schedule target must be 'queue' or 'chief'")
        if type(priority) is not int or not 0 <= priority <= 9:
            raise ValueError("Priority must be 0–9")

        max_schedules = int((policy.get("scheduler") or {}).get("max_schedules", 64))
        self._ensure_dir()
        with FileLock(str(self.lock_path), timeout=5):
            entries = self._read_all()
            if len(entries) >= min(MAX_SCHEDULES_LIMIT, max_schedules):
                raise ValueError(f"Schedule limit reached ({max_schedules})")
            entry = ScheduleEntry(
                id=f"sched-{uuid.uuid4().hex[:10]}",
                expression=expression.strip(),
                task=parsed.task,
                kind=parsed.kind,
                timezone=parsed.timezone,
                target=eff_target,
                cron_expr=parsed.cron_expr,
                interval_seconds=parsed.interval_seconds,
                run_at=parsed.run_at,
                next_run_at=parsed.next_run_at,
                priority=priority,
                mission=mission,
                created_at=now_ts,
                updated_at=now_ts,
            )
            entries.append(entry)
            self._write_all(entries)
        return entry

    def set_enabled(self, schedule_id: str, enabled: bool) -> ScheduleEntry:
        self._ensure_dir()
        with FileLock(str(self.lock_path), timeout=5):
            entries = self._read_all()
            for entry in entries:
                if entry.id == schedule_id:
                    entry.enabled = bool(enabled)
                    entry.updated_at = float(self.clock())
                    self._write_all(entries)
                    return entry
        raise ValueError(f"Unknown schedule '{schedule_id}'")

    def remove(self, schedule_id: str) -> bool:
        self._ensure_dir()
        with FileLock(str(self.lock_path), timeout=5):
            entries = self._read_all()
            kept = [e for e in entries if e.id != schedule_id]
            if len(kept) == len(entries):
                return False
            self._write_all(kept)
            return True

    def _get_studio(self):
        if self.studio is not None:
            return self.studio
        from .server import ServerState

        return ServerState(self.workspace, quiet=True).workbench.studio

    def _dispatch_entry(self, entry: ScheduleEntry) -> str:
        if entry.target == "chief":
            from .chief import ChiefOfStaff, save_mission

            chief = ChiefOfStaff(self.workspace, studio=self.studio)
            mission = chief.plan(entry.task, source="schedule")
            if ChiefOfStaff.can_auto_start(mission, chief.chief_config(), chief.autonomy_policy()):
                chief.start_background(mission.id, auto_approve=True)
            else:
                mission.status = "awaiting_approval"
                save_mission(self.workspace, mission)
            return mission.id

        from .jarvis.queue import Queue

        queue = Queue(self._get_studio())
        job = queue.enqueue(
            entry.task,
            priority=entry.priority,
            source="event:schedule",
            mission=entry.mission,
        )
        return str(job["id"])

    def tick(self, *, now: float | None = None, ignore_policy_gate: bool = False) -> list[dict[str, Any]]:
        """Evaluate due schedules and enqueue/plan due items safely.

        Never executes unless ``autonomy.yaml`` ``scheduler.enabled`` is ``true``
        and ``level != 'L0'`` (unless explicit test/CLI run-due overrides gate).
        """
        policy = load_autonomy_dict(self.workspace)
        if policy["level"] == "L0":
            return []
        sched_cfg = policy.get("scheduler") or {}
        if not ignore_policy_gate and not sched_cfg.get("enabled", False):
            return []

        from .computer_control import EmergencyStop

        if EmergencyStop().stopped():
            return []

        now_ts = float(self.clock()) if now is None else float(now)
        dispatched: list[dict[str, Any]] = []
        self._ensure_dir()
        with FileLock(str(self.lock_path), timeout=5):
            entries = self._read_all()
            changed = False
            for entry in entries:
                if not entry.enabled:
                    continue
                due_at = float(entry.next_run_at or 0.0)
                if due_at <= 0 or due_at > now_ts:
                    continue
                slot = int(due_at)
                if entry.last_fired_slot == slot:
                    # Idempotent: slot already fired
                    nxt = compute_next_occurrence(entry, after_ts=now_ts)
                    if nxt is None:
                        entry.enabled = False
                    else:
                        entry.next_run_at = nxt
                    changed = True
                    continue

                try:
                    dispatched_id = self._dispatch_entry(entry)
                    entry.last_fired_slot = slot
                    entry.last_run_at = now_ts
                    entry.last_dispatched_id = dispatched_id
                    entry.run_count += 1
                    nxt = compute_next_occurrence(entry, after_ts=now_ts)
                    if nxt is None:
                        entry.enabled = False
                        entry.next_run_at = 0.0
                    else:
                        entry.next_run_at = nxt
                    entry.updated_at = now_ts
                    changed = True
                    dispatched.append({
                        "schedule_id": entry.id,
                        "target": entry.target,
                        "dispatched_id": dispatched_id,
                        "task": entry.task,
                        "fired_at": now_ts,
                    })
                except Exception as exc:
                    dispatched.append({
                        "schedule_id": entry.id,
                        "target": entry.target,
                        "error": f"{type(exc).__name__}: {exc}",
                        "fired_at": now_ts,
                    })
                    # Advance recurring slot to avoid spin loop on transient cooldown/budget rejection
                    nxt = compute_next_occurrence(entry, after_ts=now_ts)
                    if nxt is not None:
                        entry.next_run_at = nxt
                        entry.updated_at = now_ts
                        changed = True
            if changed:
                self._write_all(entries)
        return dispatched


def scheduler_summary(workspace: Path | str) -> dict[str, Any]:
    """Return bounded read-only scheduler summary for HUD/monitoring."""
    root = Path(workspace).resolve()
    try:
        policy = load_autonomy_dict(root)
        sched_cfg = policy.get("scheduler") or {}
        entries = Scheduler(root).list()
        enabled_entries = [e for e in entries if e.enabled]
        next_runs = [e.next_run_at for e in enabled_entries if e.next_run_at > 0]
        return {
            "enabled": bool(sched_cfg.get("enabled", False)),
            "timezone": resolve_timezone(root),
            "total": len(entries),
            "active": len(enabled_entries),
            "next_run_at": min(next_runs) if next_runs else None,
        }
    except Exception as exc:
        return {
            "enabled": False,
            "timezone": "UTC",
            "total": 0,
            "active": 0,
            "next_run_at": None,
            "error": str(exc),
        }


__all__ = [
    "CronSpec",
    "ParsedSchedule",
    "ScheduleEntry",
    "Scheduler",
    "compute_next_occurrence",
    "normalize_bangla_digits",
    "parse_schedule_expression",
    "scheduler_summary",
]
