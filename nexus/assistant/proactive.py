"""Morning briefs, durable reminders, monitors and DND-aware local notices."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .common import AssistantError, UntrustedDocument, assistant_dir, atomic_json, clean_text, read_json


class ReminderStore:
    def __init__(self, workspace: Path | str):
        self.workspace = Path(workspace).resolve()
        self.root = assistant_dir(workspace)
        self.path = self.root / "reminders.db"

    def _connect(self, *, create: bool = True) -> sqlite3.Connection | None:
        if not create and not self.path.exists():
            return None
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise AssistantError("Reminder database must not be a symlink")
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        if create:
            with connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS reminder(id TEXT PRIMARY KEY,text TEXT NOT NULL,rule TEXT NOT NULL,"
                    "next_run REAL NOT NULL,enabled INTEGER NOT NULL,created REAL NOT NULL,last_fired REAL)")
        return connection

    @staticmethod
    def _zone(name: str):
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise AssistantError("Unknown reminder timezone") from exc

    @staticmethod
    def _clock(value: str) -> tuple[int, int]:
        try:
            hour, minute = (int(part) for part in value.split(":"))
        except (ValueError, TypeError) as exc:
            raise AssistantError("Recurring time must be HH:MM") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise AssistantError("Recurring time must be HH:MM")
        return hour, minute

    def add(self, text: str, *, run_at: float | None = None, every_minutes: int | None = None,
            daily: str = "", weekly: str = "", timezone: str = "Asia/Dhaka") -> dict[str, Any]:
        text = clean_text(text, 1000)
        selected = sum(value is not None for value in (run_at, every_minutes)) + bool(daily) + bool(weekly)
        if selected != 1:
            raise AssistantError("Choose exactly one: run_at, every_minutes, daily, or weekly")
        zone = self._zone(timezone)
        now = time.time()
        if run_at is not None:
            if isinstance(run_at, bool) or not isinstance(run_at, (int, float)) or not 0 < run_at <= now + 366 * 86400:
                raise AssistantError("One-time reminder timestamp is outside the supported window")
            rule = {"kind": "once"}
            next_run = float(run_at)
        elif every_minutes is not None:
            if type(every_minutes) is not int or not 1 <= every_minutes <= 10_080:
                raise AssistantError("Recurring interval must be 1–10080 minutes")
            rule = {"kind": "interval", "minutes": every_minutes}
            next_run = now + every_minutes * 60
        else:
            value = daily or weekly
            parts = value.split(":")
            if weekly:
                if len(parts) != 3 or parts[0].upper() not in {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}:
                    raise AssistantError("Weekly reminder must be MON:HH:MM (any weekday name)")
                weekday = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"].index(parts[0].upper())
                clock = ":".join(parts[1:])
            else:
                weekday, clock = None, value
            hour, minute = self._clock(clock)
            rule = {"kind": "weekly" if weekly else "daily", "hour": hour, "minute": minute,
                    "weekday": weekday, "timezone": timezone}
            next_run = self._next(rule, now, zone)
        ident = uuid.uuid4().hex[:16]
        connection = self._connect()
        try:
            with connection:
                if connection.execute("SELECT COUNT(*) FROM reminder").fetchone()[0] >= 1000:
                    raise AssistantError("At most 1000 reminders are kept")
                connection.execute("INSERT INTO reminder(id,text,rule,next_run,enabled,created) VALUES(?,?,?,?,1,?)",
                                   (ident, text, json.dumps(rule, sort_keys=True), next_run, now))
        finally:
            connection.close()
        return {"id": ident, "text": text, "rule": rule, "next_run": next_run, "enabled": True, "created": now}

    @staticmethod
    def _next(rule: dict[str, Any], after: float, zone=None) -> float:
        kind = rule["kind"]
        if kind == "interval":
            return after + int(rule["minutes"]) * 60
        zone = zone or ReminderStore._zone(rule["timezone"])
        current = datetime.fromtimestamp(after, zone).replace(second=0, microsecond=0)
        candidate = current.replace(hour=int(rule["hour"]), minute=int(rule["minute"]))
        if rule.get("weekday") is not None:
            candidate += timedelta(days=(int(rule["weekday"]) - candidate.weekday()) % 7)
        if candidate.timestamp() <= after:
            candidate += timedelta(days=7 if rule.get("weekday") is not None else 1)
        return candidate.timestamp()

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "text": row["text"], "rule": json.loads(row["rule"]),
                "next_run": row["next_run"], "enabled": bool(row["enabled"]),
                "created": row["created"], "last_fired": row["last_fired"]}

    def list(self) -> list[dict[str, Any]]:
        connection = self._connect(create=False)
        if connection is None:
            return []
        try:
            return [self._row(row) for row in connection.execute(
                "SELECT * FROM reminder ORDER BY enabled DESC,next_run LIMIT 1000")]
        finally:
            connection.close()

    def cancel(self, ident: str) -> bool:
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute("UPDATE reminder SET enabled=0 WHERE id=?", (ident,))
                return bool(cursor.rowcount)
        finally:
            connection.close()

    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else float(now)
        connection = self._connect(create=False)
        if connection is None:
            return []
        try:
            return [self._row(row) for row in connection.execute(
                "SELECT * FROM reminder WHERE enabled=1 AND next_run<=? ORDER BY next_run LIMIT 100", (now,))]
        finally:
            connection.close()

    def mark_fired(self, ident: str, now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            with connection:
                row = connection.execute("SELECT * FROM reminder WHERE id=? AND enabled=1", (ident,)).fetchone()
                if row is None:
                    return
                rule = json.loads(row["rule"])
                if rule["kind"] == "once":
                    connection.execute("UPDATE reminder SET enabled=0,last_fired=? WHERE id=?", (now, ident))
                else:
                    next_run = self._next(rule, max(now, float(row["next_run"])))
                    connection.execute("UPDATE reminder SET next_run=?,last_fired=? WHERE id=?",
                                       (next_run, now, ident))
        finally:
            connection.close()


class MonitorStore:
    """Durable monitor definitions and bounded last results (no response bodies)."""
    def __init__(self, workspace: Path | str):
        self.path = assistant_dir(workspace) / "monitors.json"
        self.lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        return read_json(self.path, {"monitors": []})

    def list(self) -> list[dict[str, Any]]:
        return list(self._read().get("monitors", []))

    def add(self, kind: str, options: dict[str, Any], every_minutes: float = 5) -> dict[str, Any]:
        if kind not in {"server", "website", "market", "github_ci"}:
            raise AssistantError("Unknown monitor kind")
        minutes = float(every_minutes)
        if not 1 <= minutes <= 1440:
            raise AssistantError("Monitor interval must be 1..1440 minutes")
        with self.lock:
            data = self._read()
            rows = data.setdefault("monitors", [])
            if len(rows) >= 100:
                raise AssistantError("At most 100 monitors are allowed")
            row = {"id": uuid.uuid4().hex[:12], "kind": kind, "options": options,
                   "every_minutes": minutes, "next_run": 0.0, "last_result": None}
            rows.append(row)
            atomic_json(self.path, data)
            return row

    def remove(self, ident: str) -> bool:
        with self.lock:
            data = self._read()
            rows = data.get("monitors", [])
            remaining = [row for row in rows if row["id"] != ident]
            if len(remaining) == len(rows):
                return False
            data["monitors"] = remaining
            atomic_json(self.path, data)
            return True

    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        return [row for row in self.list() if float(row.get("next_run", 0)) <= current]

    def mark(self, ident: str, result: dict[str, Any], now: float | None = None) -> None:
        current = time.time() if now is None else now
        with self.lock:
            data = self._read()
            for row in data.get("monitors", []):
                if row["id"] == ident:
                    row["last_result"] = result
                    row["next_run"] = current + float(row["every_minutes"]) * 60
            atomic_json(self.path, data)


class BriefSchedule:
    """One local-time morning brief schedule; sources are still independently enabled."""
    def __init__(self, workspace: Path | str):
        self.path = assistant_dir(workspace) / "brief-schedule.json"

    def get(self) -> dict[str, Any]:
        return read_json(self.path, {"enabled": False, "time": "08:00", "timezone": "UTC", "last_date": ""})

    def save(self, *, enabled: bool, at: str = "08:00", timezone: str = "UTC") -> dict[str, Any]:
        try:
            datetime.strptime(at, "%H:%M")
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise AssistantError("Valid HH:MM and IANA timezone required") from exc
        data = {**self.get(), "enabled": bool(enabled), "time": at, "timezone": timezone}
        atomic_json(self.path, data)
        return data

    def due(self, now: datetime | None = None) -> bool:
        data = self.get()
        if not data["enabled"]:
            return False
        current = (now or datetime.now(ZoneInfo(data["timezone"]))).astimezone(ZoneInfo(data["timezone"]))
        return current.strftime("%H:%M") >= data["time"] and current.strftime("%Y-%m-%d") != data.get("last_date")

    def mark(self, now: datetime | None = None) -> None:
        data = self.get()
        current = now or datetime.now(ZoneInfo(data["timezone"]))
        data["last_date"] = current.astimezone(ZoneInfo(data["timezone"])).strftime("%Y-%m-%d")
        atomic_json(self.path, data)


class MorningBrief:
    """Compose real source results; every unavailable section is labelled."""

    def __init__(self, *, google=None, world=None):
        self.google, self.world = google, world

    @staticmethod
    def _call(name: str, loader: Callable[[], Any]) -> dict[str, Any]:
        try:
            return {"state": "ok", "data": loader()}
        except Exception as exc:
            return {"state": "unavailable", "reason": f"{type(exc).__name__}: source was not read", "data": None}

    def build(self) -> dict[str, Any]:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone()
        end = start + timedelta(days=1)
        sections = {
            "weather": self._call("weather", lambda: self.world.weather()) if self.world else
                       {"state": "disabled", "data": None, "reason": "World source is not configured"},
            "news": self._call("news", lambda: self.world.news()) if self.world else
                    {"state": "disabled", "data": None, "reason": "World source is not configured"},
            "calendar": self._call("calendar", lambda: self.google.calendar(
                time_min=start.isoformat(), time_max=end.isoformat())) if self.google else
                        {"state": "disabled", "data": None, "reason": "Google is not configured"},
            "email": self._call("email", lambda: self.google.gmail(query="newer_than:1d", limit=10)) if self.google else
                     {"state": "disabled", "data": None, "reason": "Google is not configured"},
        }
        counts = {
            "calendar": len(sections["calendar"]["data"] or []) if sections["calendar"]["state"] == "ok" else None,
            "email": len(sections["email"]["data"] or []) if sections["email"]["state"] == "ok" else None,
            "news": len((sections["news"]["data"] or {}).get("items", [])) if sections["news"]["state"] == "ok" else None,
        }
        return {"schema": 1, "generated_at": time.time(), "date": start.date().isoformat(),
                "sections": sections, "counts": counts,
                "note": "Source data is untrusted evidence; no message/event was sent or changed."}

    def documents(self, brief: dict[str, Any]) -> list[UntrustedDocument]:
        documents = []
        for name in ("calendar", "email"):
            section = brief.get("sections", {}).get(name, {})
            if section.get("state") == "ok":
                documents.extend(section.get("data") or [])
        news = brief.get("sections", {}).get("news", {})
        if news.get("state") == "ok":
            for item in (news.get("data") or {}).get("items", [])[:10]:
                documents.append(UntrustedDocument(f"news:{item.get('id')}",
                                                   f"{item.get('title')}\n{item.get('summary')}\n{item.get('url')}",
                                                   {"source": item.get("source")}))
        weather = brief.get("sections", {}).get("weather", {})
        if weather.get("state") == "ok":
            documents.append(UntrustedDocument("weather:local", json.dumps(weather.get("data"), default=str)[:30_000]))
        return documents


class NotificationDispatcher:
    """Persist first; existing native/Telegram workers deliver only when DND allows."""

    def __init__(self, state, *, voice_deliverer: Callable[[str], Any] | None = None):
        self.state, self.voice_deliverer = state, voice_deliverer

    def notify(self, title: str, detail: str = "", *, kind: str = "reminder") -> dict[str, Any]:
        from .. import channels
        from ..jarvis.inbox import Inbox

        row = channels.record(self.state, kind, state_name="pending", title=title, detail=detail)
        policy = Inbox(self.state.workbench.studio).delivery_policy()
        suppressed = bool(policy.get("quiet") or float(policy.get("dnd_until", 0)) > time.time())
        delivered = {"local": bool(row), "voice": False, "native": "worker-managed", "telegram": "worker-managed"}
        if not suppressed and self.voice_deliverer:
            try:
                self.voice_deliverer(title + (". " + detail if detail else ""))
                delivered["voice"] = True
            except Exception as exc:
                delivered["voice_error"] = type(exc).__name__
        return {"stored": bool(row), "suppressed": suppressed, "delivery": delivered,
                "note": "Native and counts-only Telegram delivery reuse their existing opt-in workers; no direct implicit send."}


def fire_due(state, *, now: float | None = None, dispatcher: NotificationDispatcher | None = None) -> list[dict[str, Any]]:
    store = ReminderStore(state.workspace)
    dispatcher = dispatcher or NotificationDispatcher(state)
    fired = []
    for row in store.due(now):
        result = dispatcher.notify(row["text"], f"Reminder {row['id']}")
        # A failed durable inbox write must remain due for a later retry.
        completed = bool(result["stored"])
        if completed:
            store.mark_fired(row["id"], now)
        fired.append({"id": row["id"], "completed": completed, "notification": result})
    return fired


def check_monitor(kind: str, config: dict[str, Any], *, workspace: Path | str = ".") -> dict[str, Any]:
    """One bounded check. Conditions, not prose, determine whether to alert."""
    if kind == "website":
        url = clean_text(config.get("url"), 2048)
        if not url.startswith(("https://", "http://")):
            raise AssistantError("Website monitor needs an explicit HTTP(S) URL")
        try:
            response = httpx.get(url, timeout=10, follow_redirects=False, trust_env=False)
            code = response.status_code
        except httpx.HTTPError as exc:
            return {"ok": False, "alert": True, "reason": type(exc).__name__}
        expected = int(config.get("status", 200))
        return {"ok": code == expected, "alert": code != expected, "status": code, "expected": expected}
    if kind == "server":
        import psutil

        metric = config.get("metric")
        threshold = float(config.get("threshold"))
        value = (psutil.cpu_percent(interval=None) if metric == "cpu" else
                 psutil.virtual_memory().percent if metric == "memory" else
                 100 * psutil.disk_usage(str(workspace)).free / psutil.disk_usage(str(workspace)).total
                 if metric == "disk_free" else None)
        if value is None or not 0 <= threshold <= 100:
            raise AssistantError("Server metric must be cpu/memory/disk_free with a 0–100 threshold")
        alert = value <= threshold if metric == "disk_free" else value >= threshold
        return {"ok": not alert, "alert": alert, "metric": metric, "value": value, "threshold": threshold}
    if kind == "market":
        url = clean_text(config.get("url"), 2048)
        if not url.startswith("https://"):
            raise AssistantError("Market monitor requires an explicit HTTPS JSON endpoint")
        data = _json_request(url)
        value = data
        for part in str(config.get("field", "")).split("."):
            if not part:
                raise AssistantError("Market field is required")
            value = value[int(part)] if isinstance(value, list) and part.isdigit() else value.get(part) if isinstance(value, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AssistantError("Market endpoint did not produce a numeric field")
        threshold = float(config.get("threshold"))
        direction = config.get("direction", "above")
        alert = value >= threshold if direction == "above" else value <= threshold if direction == "below" else None
        if alert is None:
            raise AssistantError("Market direction must be above or below")
        return {"ok": not alert, "alert": alert, "value": value, "threshold": threshold, "direction": direction}
    if kind == "github_ci":
        from ..jarvis.sources import github

        state, alert = github(Path(workspace), clean_text(config.get("repo"), 200),
                              str(config.get("ref") or "HEAD"))
        return {"ok": not alert, "alert": bool(alert), "sha": state.get("sha"), "checks": len(state.get("checks", []))}
    raise AssistantError("Monitor kind must be website, server, market, or github_ci")


def _json_request(url: str) -> Any:
    try:
        response = httpx.get(url, timeout=10, follow_redirects=False, trust_env=False)
        response.raise_for_status()
        if len(response.content) > 1_000_000:
            raise AssistantError("Monitor response budget exceeded")
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AssistantError(f"Monitor endpoint unavailable ({type(exc).__name__})") from exc


def run_monitors(state, monitors: list[dict[str, Any]], *, dispatcher: NotificationDispatcher | None = None) -> list[dict[str, Any]]:
    if len(monitors) > 50:
        raise AssistantError("At most 50 monitors per pass are allowed")
    dispatcher = dispatcher or NotificationDispatcher(state)
    path = assistant_dir(state.workspace) / "monitor-state.json"
    previous = read_json(path, {})
    results = []
    for monitor in monitors:
        name = clean_text(monitor.get("name"), 100)
        kind = str(monitor.get("kind") or "")
        config = monitor.get("config") or {}
        try:
            outcome = check_monitor(kind, config, workspace=state.workspace)
        except Exception as exc:
            outcome = {"ok": False, "alert": True, "reason": type(exc).__name__}
        fingerprint = json.dumps(outcome, sort_keys=True, default=str)
        changed = previous.get(name) != fingerprint
        notified = None
        if outcome["alert"] and changed:
            notified = dispatcher.notify(f"Monitor: {name}", json.dumps(outcome, default=str)[:250], kind="monitor")
        # Do not suppress a later retry when the durable notice itself failed.
        if outcome["alert"] and changed and notified is not None and not notified.get("stored"):
            previous.pop(name, None)
        else:
            previous[name] = fingerprint
        results.append({"name": name, "kind": kind, "result": outcome, "changed": changed, "notification": notified})
    atomic_json(path, previous)
    return results


def run_saved_monitors(state, *, now: float | None = None,
                       dispatcher: NotificationDispatcher | None = None) -> list[dict[str, Any]]:
    """Run durable due checks; existing monitor state suppresses repeated alerts."""
    store = MonitorStore(state.workspace)
    rows = store.due(now)
    specs = [{"name": row["id"], "kind": row["kind"], "config": row.get("options", {})} for row in rows]
    outcomes = run_monitors(state, specs, dispatcher=dispatcher) if specs else []
    by_name = {row["name"]: row for row in outcomes}
    for row in rows:
        result = by_name.get(row["id"], {"ok": False, "alert": True, "reason": "MissingResult"})
        store.mark(row["id"], result, now)
    return outcomes


class ProactiveWorker:
    """Explicit foreground/background scheduler; never started at import time."""

    def __init__(self, state, *, monitors: Callable[[], list[dict[str, Any]]] | None = None,
                 brief_builder: Callable[[], dict[str, Any]] | None = None,
                 voice_deliverer: Callable[[str], Any] | None = None, interval: float = 30):
        self.state, self.monitors, self.brief_builder = state, monitors, brief_builder
        self.interval = min(300.0, max(1.0, float(interval)))
        self.voice_deliverer = voice_deliverer
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def pass_once(self) -> dict[str, Any]:
        dispatcher = NotificationDispatcher(self.state, voice_deliverer=self.voice_deliverer)
        brief_result = None
        schedule = BriefSchedule(self.state.workspace)
        if self.brief_builder and schedule.due():
            brief = self.brief_builder()
            brief_result = dispatcher.notify("Morning brief", json.dumps(brief, ensure_ascii=False, default=str)[:8000],
                                             kind="brief")
            if brief_result["stored"]:
                schedule.mark()
        return {"reminders": fire_due(self.state, dispatcher=dispatcher),
                "monitors": run_saved_monitors(self.state, dispatcher=dispatcher),
                "extra_monitors": run_monitors(self.state, self.monitors(), dispatcher=dispatcher)
                if self.monitors else [], "brief": brief_result}

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()

        def run():
            while not self.stop_event.is_set():
                try:
                    self.pass_once()
                except Exception:
                    pass  # individual sources retain their unavailable state; retry next tick
                self.stop_event.wait(self.interval)

        self.thread = threading.Thread(target=run, name="nexus-assistant-proactive", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
