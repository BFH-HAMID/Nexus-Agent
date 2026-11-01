"""Mission Control: one summary of "what is the agent doing" plus its live feed.

Two public surfaces live here, both wired up in :mod:`nexus.server`:

``GET /api/mission/summary``
    One small projection of the existing local records (Studio/Jarvis jobs,
    pending approvals, daemon state, token usage, configured agents) plus a
    psutil sample of this machine. Every section carries ``state`` (``ok`` or
    ``unavailable`` with a ``reason``) and every number carries ``observed_at``,
    so the dashboard can print "Unavailable" / "not checked" instead of a
    fabricated placeholder.

``GET /api/mission/stream``
    A server-sent event feed with exactly five event names:

    ``heartbeat``     server time, uptime, subscriber count and the slow checks
    ``task.update``   active/queued/failed counts and the recent activity list
    ``approval.new``  a pending approval appeared (count + ids)
    ``metrics.tick``  a fresh cpu / ram / disk sample
    ``alert.new``     a source went bad (ollama, GitHub, disk, daemon)

    EventSource cannot send headers, so the stream is authenticated exactly
    like the rest of the GUI: the signed session cookie, ``?token=`` or the
    ``X-Nexus-Token`` header all work.

The feed only runs while somebody is connected: the first subscriber starts a
daemon thread, the last one stops it. Sampling cadence is fixed on the server
(5 s metrics, 15 s heartbeat, 60 s slow checks) and can be shortened with
``NEXUS_MISSION_TICK`` / ``NEXUS_MISSION_HEARTBEAT`` / ``NEXUS_MISSION_SLOW``
for tests, never from a request.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from typing import Any

SCHEMA = 1
EVENTS = ("heartbeat", "task.update", "approval.new", "metrics.tick", "alert.new")
MAX_SUBSCRIBERS = 32
SUBSCRIBER_QUEUE = 64
RECENT_ACTIVITY = 8
FAILED_WINDOW_S = 86_400


def _interval(name: str, default: float, low: float = 0.5, high: float = 300.0) -> float:
    """Read a clamped interval from the environment (tests shorten the ticks)."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return min(high, max(low, value))


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value == value and abs(value) != float("inf") else None


def _section(**values: Any) -> dict[str, Any]:
    return {"state": "ok", "reason": None, **values}


def _unavailable(reason: str, **values: Any) -> dict[str, Any]:
    return {"state": "unavailable", "reason": reason, **values}


class Subscriber:
    """One connected browser. Bounded so a stalled client cannot grow memory."""

    __slots__ = ("events", "lock", "dropped", "ident")

    def __init__(self, ident: int) -> None:
        self.ident = ident
        self.events: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE)
        self.lock = threading.Lock()
        self.dropped = 0

    def push(self, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            try:
                self.events.put_nowait((name, payload))
            except queue.Full:
                # Drop the oldest frame: a live dashboard prefers "now" to history.
                try:
                    self.events.get_nowait()
                except queue.Empty:  # pragma: no cover - lost a race with a reader
                    pass
                self.dropped += 1
                try:
                    self.events.put_nowait((name, payload))
                except queue.Full:  # pragma: no cover - reader vanished mid-push
                    pass

    def next(self, timeout: float):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None


class Mission:
    """Snapshot builder plus the live feed shared by every dashboard tab."""

    def __init__(self, state: Any, *, tick: float | None = None, heartbeat: float | None = None,
                 slow: float | None = None) -> None:
        self.state = state
        self.tick = _interval("NEXUS_MISSION_TICK", tick or 5.0)
        self.heartbeat_every = _interval("NEXUS_MISSION_HEARTBEAT", heartbeat or 15.0, low=self.tick)
        self.slow_every = _interval("NEXUS_MISSION_SLOW", slow or 60.0, low=self.tick)
        self._lock = threading.RLock()
        self._subscribers: dict[int, Subscriber] = {}
        self._next_ident = 1
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cache: dict[str, Any] | None = None
        self._cache_at = 0.0
        self._summary_ttl = max(1.0, self.tick / 2)
        self._health: dict[str, dict[str, Any]] = {}
        self._health_checked_at = 0.0
        self._health_running = False
        self._last_tasks: tuple | None = None
        self._last_approvals: tuple | None = None
        self._last_alerts: dict[str, dict[str, Any]] = {}
        self._audit_lock = threading.Lock()
        self._audit_at: dict[str, float] = {}
        self._log: deque = deque(maxlen=40)
        self._seq = 0
        self.started_at = time.time()

    # ── subscribers ────────────────────────────────────────────────────── #

    def subscribe(self, *, instant: bool = True) -> Subscriber | None:
        """Register a listener and make sure the producer thread is running."""
        with self._lock:
            if len(self._subscribers) >= MAX_SUBSCRIBERS:
                return None
            ident = self._next_ident
            self._next_ident += 1
            subscriber = Subscriber(ident)
            self._subscribers[ident] = subscriber
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name=f"nexus-mission-{id(self):x}", daemon=True)
                self._thread.start()
            self._wake.set()
        if instant:
            summary = self.summary(force=True)
            subscriber.push("heartbeat", self._heartbeat(summary))
            subscriber.push("task.update", self._task_update(summary))
            subscriber.push("approval.new", self._approval_update(summary, first=True))
            subscriber.push("metrics.tick", self._metrics_event(summary))
        return subscriber

    def unsubscribe(self, subscriber: Subscriber | None) -> None:
        if subscriber is None:
            return
        with self._lock:
            self._subscribers.pop(subscriber.ident, None)
            if not self._subscribers:
                self._stop.set()
                self._wake.set()

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def ensure_health(self) -> None:
        """Refresh the slow checks in the background when nobody is streaming.

        A page that only polls ``/api/mission/summary`` (the documented fallback
        when a proxy blocks EventSource) would otherwise show "not checked"
        forever. The check runs once per ``slow_every`` and never blocks the
        request that triggered it.
        """
        if self.subscribers:
            return
        with self._lock:
            if self._health_running or time.time() - self._health_checked_at < self.slow_every:
                return
            self._health_running = True

        def run() -> None:
            try:
                self._slow_checks()
            finally:
                with self._lock:
                    self._health_running = False

        threading.Thread(target=run, name="nexus-mission-check", daemon=True).start()

    def publish(self, name: str, payload: dict[str, Any]) -> int:
        """Fan one frame out; returns how many listeners received it."""
        if name not in EVENTS:
            raise ValueError(f"Unknown mission event: {name}")
        with self._lock:
            self._seq += 1
            frame = dict(payload, seq=self._seq, event=name)
            targets = list(self._subscribers.values())
            self._log.append(frame)
        for subscriber in targets:
            subscriber.push(name, frame)
        return len(targets)

    # ── summary ────────────────────────────────────────────────────────── #

    def summary(self, *, force: bool = False) -> dict[str, Any]:
        """Cached projection of the existing records plus one local sample."""
        now = time.time()
        with self._lock:
            if not force and self._cache is not None and now - self._cache_at < self._summary_ttl:
                return self._cache
        started = time.perf_counter()
        snapshot: dict[str, Any] = {}
        snapshot_error = None
        try:
            snapshot = self.state.gui.operations().snapshot()
        except Exception as exc:  # a broken store must not take the page down
            snapshot_error = f"Local records unavailable ({type(exc).__name__})."
        observed = _num(snapshot.get("observed_at")) or now
        summary = {
            "schema": SCHEMA,
            "observed_at": observed,
            "source": "Existing Studio/Jarvis records and local psutil samples; nothing is inferred.",
            "daemon": self._daemon(snapshot, snapshot_error, observed),
            "tasks": self._tasks(snapshot, snapshot_error, observed),
            "approvals": self._approvals(snapshot, snapshot_error, observed),
            "resources": self._resources(observed),
            "usage": self._usage(snapshot, snapshot_error, observed),
            "agents": self._agents(observed),
            "activity": self._activity(snapshot, observed),
            "health": self._health_section(),
            "alerts": self._alerts(),
            "stream": {"subscribers": self.subscribers, "schema": SCHEMA, "started_at": self.started_at},
        }
        summary["build_ms"] = round((time.perf_counter() - started) * 1000, 1)
        with self._lock:
            self._cache = summary
            self._cache_at = now
        return summary

    def _daemon(self, snapshot: dict[str, Any], error: str | None, observed: float) -> dict[str, Any]:
        daemon = snapshot.get("daemon") if isinstance(snapshot, dict) else None
        if not isinstance(daemon, dict) or error:
            return _unavailable(error or "Daemon state not reported by the local store.", observed_at=observed)
        # ``state`` is the section status shared by every block; the daemon's
        # own value is ``status`` so the two never shadow each other.
        return _section(status=str(daemon.get("state") or "unknown"),
                        lock_held=bool(daemon.get("lock_held")),
                        execution_enabled=daemon.get("execution_enabled"),
                        dispatch_configured=daemon.get("dispatch_configured"), observed_at=observed)

    def _tasks(self, snapshot: dict[str, Any], error: str | None, observed: float) -> dict[str, Any]:
        if error:
            return _unavailable(error, observed_at=observed, active=None, queued=None, failed_24h=None,
                                total=None, recent=[])
        jobs = snapshot.get("jobs") if isinstance(snapshot, dict) else None
        if not isinstance(jobs, list):
            return _unavailable("Job list not reported by the local store.", observed_at=observed,
                                active=None, queued=None, failed_24h=None, total=None, recent=[])
        cutoff = time.time() - FAILED_WINDOW_S
        active = sum(1 for row in jobs if row.get("status") == "running")
        queued = sum(1 for row in jobs if row.get("status") == "queued")
        failed = sum(1 for row in jobs if row.get("status") in {"failed", "error"}
                     and (_num(row.get("created")) or 0) >= cutoff)
        recent = [{key: row.get(key) for key in ("id", "task", "status", "created", "active_role")}
                  for row in jobs[:RECENT_ACTIVITY]]
        return _section(active=active, queued=queued, failed_24h=failed,
                        total=int(_num(snapshot.get("jobs_total")) or len(jobs)), recent=recent,
                        observed_at=observed)

    def _approvals(self, snapshot: dict[str, Any], error: str | None, observed: float) -> dict[str, Any]:
        if error:
            return _unavailable(error, pending=None, items=[], observed_at=observed)
        items = snapshot.get("pending_actions") if isinstance(snapshot, dict) else None
        if not isinstance(items, list):
            return _unavailable("Approval list not reported by the local store.", pending=None, items=[],
                                observed_at=observed)
        pending = int(_num(snapshot.get("pending_actions_total")) or len(items))
        rows = [{key: row.get(key) for key in ("id", "job_id", "tool", "expires")} for row in items[:20]]
        return _section(pending=pending, items=rows, observed_at=observed)

    def _resources(self, observed: float) -> dict[str, Any]:
        sample: dict[str, Any] = {"cpu_percent": None, "ram_percent": None, "disk_free_bytes": None,
                                  "disk_total_bytes": None, "gpu": None, "gpu_reason": None}
        reason = None
        try:
            import psutil
            sample["cpu_percent"] = _num(psutil.cpu_percent(interval=None))
            sample["ram_percent"] = _num(psutil.virtual_memory().percent)
            disk = psutil.disk_usage(str(self.state.workspace))
            sample["disk_free_bytes"] = int(disk.free)
            sample["disk_total_bytes"] = int(disk.total)
        except Exception as exc:
            reason = f"Local metrics unavailable ({type(exc).__name__})."
        with self._lock:
            gpu = dict(self._health.get("gpu") or {})
        if gpu.get("state") == "ok":
            sample["gpu"] = gpu.get("text")
        else:
            sample["gpu_reason"] = gpu.get("text") or "not checked"
        if reason:
            return _unavailable(reason, observed_at=observed, **sample)
        return _section(observed_at=observed, **sample)

    def _usage(self, snapshot: dict[str, Any], error: str | None, observed: float) -> dict[str, Any]:
        usage = snapshot.get("usage") if isinstance(snapshot, dict) else None
        if error or not isinstance(usage, dict):
            return _unavailable(error or "Usage ledger not reported by the local store.",
                                tokens_in=None, tokens_out=None, cost_usd=None,
                                cost_reason="no ledger reading", observed_at=observed)
        day = usage.get("day") if isinstance(usage.get("day"), dict) else {}
        tokens_in = _num(day.get("actual_input_tokens"))
        tokens_out = _num(day.get("actual_output_tokens"))
        cost = _num(usage.get("actual_cost_usd"))
        return _section(tokens_in=int(tokens_in) if tokens_in is not None else None,
                        tokens_out=int(tokens_out) if tokens_out is not None else None,
                        cost_usd=cost,
                        cost_reason=None if cost is not None else "no verified price for the models used",
                        observed_at=observed)

    def _agents(self, observed: float) -> dict[str, Any]:
        try:
            agents = self.state.config.agents
            total = len(agents)
            with_keys = sum(1 for agent in agents.values() if bool(getattr(agent, "resolved_api_key", "")))
        except Exception as exc:
            return _unavailable(f"Agent configuration unavailable ({type(exc).__name__}).",
                                total=None, with_keys=None, reachability_checked=False, observed_at=observed)
        return _section(total=total, with_keys=with_keys, reachability_checked=False, observed_at=observed)

    def _activity(self, snapshot: dict[str, Any], observed: float) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if isinstance(snapshot, dict):
            for job in snapshot.get("jobs") or []:
                rows.append({"id": job.get("id"), "task": job.get("task") or "",
                             "status": job.get("status"), "created": job.get("created"),
                             "role": job.get("active_role"), "kind": "job"})
        try:
            with self.state.lock:
                sessions = [session.snapshot() for session in self.state.sessions.values()]
        except Exception:
            sessions = []
        for task in sessions:
            rows.append({"id": task.get("task_id"), "task": task.get("task") or "",
                         "status": task.get("status"), "created": task.get("created"),
                         "role": task.get("role"), "agent": task.get("agent"), "kind": "session"})
        rows = [row for row in rows if row.get("id")]
        rows.sort(key=lambda row: _num(row.get("created")) or 0, reverse=True)
        seen: set = set()
        unique = []
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            unique.append(row)
        return unique[:RECENT_ACTIVITY]

    def _health_section(self) -> dict[str, Any]:
        with self._lock:
            cached = {name: dict(value) for name, value in self._health.items()}
        streaming = self.subscribers > 0
        health: dict[str, Any] = {
            "server": {"state": "ok", "text": "reachable", "checked_at": time.time()},
            "stream": {"state": "ok" if streaming else "off",
                       "text": "streaming" if streaming else "idle · checks resume when Mission Control opens",
                       "checked_at": time.time()},
        }
        for name in ("ollama", "github", "gpu", "mcp", "telegram"):
            value = cached.get(name)
            health[name] = value if value is not None else {"state": "unknown", "text": "not checked",
                                                            "checked_at": None}
        return health

    def _alerts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._last_alerts.values()]

    # ── event payloads ─────────────────────────────────────────────────── #

    def _heartbeat(self, summary: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "server_time": time.time(),
            "uptime_s": round(time.time() - getattr(self.state, "started", time.time()), 1),
            "subscribers": self.subscribers,
            "observed_at": summary.get("observed_at"),
            "health": summary.get("health"),
            "usage": summary.get("usage"),
            "agents": summary.get("agents"),
            "daemon": summary.get("daemon"),
            "alerts": summary.get("alerts"),
        }

    def _task_update(self, summary: dict[str, Any]) -> dict[str, Any]:
        tasks = summary.get("tasks") or {}
        return {"schema": SCHEMA, "observed_at": summary.get("observed_at"),
                "state": tasks.get("state"), "reason": tasks.get("reason"),
                "active": tasks.get("active"), "queued": tasks.get("queued"),
                "failed_24h": tasks.get("failed_24h"), "total": tasks.get("total"),
                "recent": tasks.get("recent") or [], "activity": summary.get("activity") or []}

    def _approval_update(self, summary: dict[str, Any], *, first: bool = False) -> dict[str, Any]:
        approvals = summary.get("approvals") or {}
        return {"schema": SCHEMA, "observed_at": summary.get("observed_at"),
                "state": approvals.get("state"), "reason": approvals.get("reason"),
                "pending": approvals.get("pending"), "items": approvals.get("items") or [],
                "new_ids": [], "first": first}

    def _metrics_event(self, summary: dict[str, Any]) -> dict[str, Any]:
        resources = summary.get("resources") or {}
        keys = ("cpu_percent", "ram_percent", "disk_free_bytes", "disk_total_bytes", "gpu", "gpu_reason")
        payload = {key: resources.get(key) for key in keys}
        payload.update({"schema": SCHEMA, "observed_at": resources.get("observed_at") or time.time(),
                        "state": resources.get("state"), "reason": resources.get("reason")})
        return payload

    # ── producer thread ────────────────────────────────────────────────── #

    def _run(self) -> None:
        next_tick = time.monotonic()
        next_heartbeat = 0.0
        next_slow = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_slow:
                next_slow = now + self.slow_every
                self._slow_checks()
            if now >= next_tick:
                next_tick = now + self.tick
                try:
                    self._fast_frame()
                except Exception:  # a sampling bug must not kill the feed
                    pass
            if now >= next_heartbeat:
                next_heartbeat = now + self.heartbeat_every
                try:
                    self.publish("heartbeat", self._heartbeat(self.summary()))
                except Exception:
                    pass
            timeout = max(0.05, min(next_tick, next_heartbeat, next_slow) - time.monotonic())
            self._wake.wait(timeout=timeout)
            self._wake.clear()

    def _fast_frame(self) -> None:
        summary = self.summary(force=True)
        self.publish("metrics.tick", self._metrics_event(summary))
        tasks = summary.get("tasks") or {}
        fingerprint = tuple((str(row.get("id")), str(row.get("status"))) for row in (tasks.get("recent") or []))
        if self._last_tasks is None or fingerprint != self._last_tasks:
            self._last_tasks = fingerprint
            self.publish("task.update", self._task_update(summary))
        approvals = summary.get("approvals") or {}
        ids = tuple(str(row.get("id")) for row in (approvals.get("items") or []))
        if self._last_approvals is None:
            self._last_approvals = ids
        elif ids != self._last_approvals:
            new_ids = [ident for ident in ids if ident not in self._last_approvals]
            self._last_approvals = ids
            payload = self._approval_update(summary)
            payload["new_ids"] = new_ids
            self.publish("approval.new", payload)
        self._raise_alerts(summary)

    def _slow_checks(self) -> None:
        from .integrations import github_status, ollama_models

        results: dict[str, dict[str, Any]] = {}
        checked_at = time.time()
        try:
            ollama = ollama_models()
            models = ollama.get("models") or []
            connected = bool(ollama.get("connected"))
            results["ollama"] = {"state": "ok" if connected else "off",
                                 "text": f"{len(models)} models" if models else (
                                     "unreachable" if not connected else "no models"),
                                 "checked_at": checked_at}
        except Exception as exc:
            results["ollama"] = {"state": "unknown", "text": f"not checked ({type(exc).__name__})",
                                 "checked_at": checked_at}
        try:
            github = github_status(self.state.workspace)
            available = bool(github.get("connected"))
            results["github"] = {"state": "ok" if available else "off",
                                 "text": (github.get("login") or "connected") if available else str(
                                     github.get("message") or "not connected")[:80],
                                 "checked_at": checked_at}
        except Exception as exc:
            results["github"] = {"state": "unknown", "text": f"not checked ({type(exc).__name__})",
                                 "checked_at": checked_at}
        try:
            desktop = self.state.workbench.get("desktop", {})
            policies = (desktop or {}).get("data", desktop or {}).get("policies") or {}
            for name in ("mcp", "telegram"):
                row = policies.get(name)
                if not isinstance(row, dict) or row.get("error"):
                    results[name] = {"state": "unknown", "text": "not checked", "checked_at": checked_at}
                else:
                    enabled = bool((row.get("settings") or {}).get("enabled"))
                    results[name] = {"state": "ok" if enabled else "off",
                                     "text": "enabled" if enabled else "off", "checked_at": checked_at}
        except Exception as exc:
            for name in ("mcp", "telegram"):
                results[name] = {"state": "unknown", "text": f"not checked ({type(exc).__name__})",
                                 "checked_at": checked_at}
        results["gpu"] = self._gpu_check(checked_at)
        with self._lock:
            previous = {name: dict(value) for name, value in self._health.items()}
            self._health.update(results)
            self._health_checked_at = checked_at
        self._transition_alerts(previous, results)
        self._wake.set()

    def _gpu_check(self, checked_at: float) -> dict[str, Any]:
        if not shutil.which("nvidia-smi"):
            return {"state": "off", "text": "nvidia-smi not installed", "checked_at": checked_at}
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4, check=False)
            first = (result.stdout or "").strip().splitlines()[0] if result.stdout else ""
            if not first:
                return {"state": "unknown", "text": "nvidia-smi returned no rows", "checked_at": checked_at}
            return {"state": "ok", "text": first[:90], "checked_at": checked_at}
        except (OSError, subprocess.SubprocessError, IndexError) as exc:
            return {"state": "unknown", "text": f"not checked ({type(exc).__name__})", "checked_at": checked_at}

    def _transition_alerts(self, previous: dict[str, Any], current: dict[str, Any]) -> None:
        labels = {"ollama": "Ollama", "github": "GitHub", "mcp": "MCP", "telegram": "Telegram"}
        for name, label in labels.items():
            before = (previous.get(name) or {}).get("state")
            after = (current.get(name) or {}).get("state")
            if after in {"off", "unknown"} and before == "ok":
                self._raise_alert(f"{name}-{after}", "warn", f"{label} changed state",
                                  (current.get(name) or {}).get("text") or after, name)
            elif after == "ok" and before in {"off", "unknown"} and before is not None:
                self._raise_alert(f"{name}-ok", "info", f"{label} is reachable again",
                                  (current.get(name) or {}).get("text") or "ok", name)
        # A machine that never had nvidia-smi is not an incident; losing one is.
        gpu_state = (current.get("gpu") or {}).get("state")
        gpu_before = (previous.get("gpu") or {}).get("state")
        if gpu_before == "ok" and gpu_state != "ok":
            self._raise_alert("gpu-" + str(gpu_state), "info", "GPU monitoring stopped",
                              (current.get("gpu") or {}).get("text") or "", "gpu")

    def _raise_alert(self, ident: str, severity: str, title: str, detail: str, source: str) -> None:
        with self._lock:
            if ident in self._last_alerts:
                return
            row = {"id": ident, "severity": severity, "title": title, "detail": detail,
                   "source": source, "observed_at": time.time()}
            self._last_alerts[ident] = row
            while len(self._last_alerts) > 12:
                self._last_alerts.pop(next(iter(self._last_alerts)))
        self.publish("alert.new", row)

    def _raise_alerts(self, summary: dict[str, Any]) -> None:
        resources = summary.get("resources") or {}
        free = _num(resources.get("disk_free_bytes"))
        if free is not None and free < 5 * 1024 ** 3:
            self._raise_alert("disk-low", "high", "Low disk space",
                              f"{free / 1024 ** 3:.1f} GB free for the workspace", "disk")
        tasks = summary.get("tasks") or {}
        daemon = summary.get("daemon") or {}
        active = tasks.get("active") or 0
        state_name = str(daemon.get("status") or "unknown")
        if active and state_name in {"stopped", "killed"}:
            self._raise_alert("daemon-down", "high", "Daemon stopped while tasks were active",
                              f"{active} active task(s); approve a restart in Operations", "daemon")

    def audit(self, client: str, *, action: str = "mission.summary", every: float = 300.0) -> bool:
        """Sample one audit line per client per window (polling must not flood)."""
        now = time.time()
        with self._audit_lock:
            last = self._audit_at.get(client, 0.0)
            if now - last < every:
                return False
            self._audit_at[client] = now
        from . import webapi
        webapi.audit_api(self.state.workspace, action, actor="user", target="dashboard",
                         result="ok", risk="low", detail={"client": client})
        return True

    def close(self) -> None:
        with self._lock:
            self._stop.set()
            self._wake.set()
            subscribers = list(self._subscribers.values())
            self._subscribers.clear()
        for subscriber in subscribers:
            subscriber.push("heartbeat", {"schema": SCHEMA, "server_time": time.time(), "shutdown": True})

    # ── test helpers ───────────────────────────────────────────────────── #

    def inject_slow(self, results: dict[str, dict[str, Any]]) -> None:
        """Used by tests to simulate a source turning healthy/unhealthy."""
        with self._lock:
            previous = {name: dict(self._health.get(name) or {}) for name in results}
            self._health.update(results)
        self._transition_alerts(previous, results)


def encode(name: str, payload: dict[str, Any], seq: int | None = None) -> bytes:
    """One SSE frame; the event name is the contract the browser listens on."""
    prefix = f"id: {seq}\n" if seq is not None else ""
    return f"{prefix}event: {name}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n".encode()


def keepalive() -> bytes:
    return b": keep-alive\n\n"
