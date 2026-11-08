"""Real self-monitoring telemetry: queue depth, running missions, cost today, error rate."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_day_bounds(now_ts: float) -> tuple[float, float]:
    dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return start, start + 86400.0


def collect_self_monitoring(
    workspace: Path | str,
    *,
    studio: Any = None,
    sessions: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Collect grounded operational self-monitoring metrics without fabricating data."""
    root = Path(workspace).resolve()
    now_ts = time.time() if now is None else float(now)
    day_start, day_end = _utc_day_bounds(now_ts)

    # 1. Jarvis queue depth & job outcomes
    jarvis_jobs: list[dict[str, Any]] = []
    if studio is None:
        try:
            from .server import ServerState

            studio = ServerState(root, quiet=True).workbench.studio
        except Exception:
            studio = None
    if studio is not None:
        try:
            jarvis_jobs = list(studio.items("jarvis-job"))
        except Exception:
            jarvis_jobs = []

    jarvis_pending = sum(
        1 for row in jarvis_jobs if row.get("status") in {"awaiting_approval", "approved"}
    )
    jarvis_running = sum(1 for row in jarvis_jobs if row.get("status") == "running")

    # 2. Chief-of-Staff missions
    from .chief import _get_runtime, list_missions

    missions = list_missions(root, limit=200)
    chief_pending = sum(1 for m in missions if m.status in {"planned", "awaiting_approval"})
    running_mission_rows: list[dict[str, Any]] = []
    chief_running = 0
    chief_interrupted = 0
    for m in missions:
        if m.status == "running":
            live_rt = _get_runtime(root, m.id)
            active_steps = [s.id for s in m.steps if s.status == "running"]
            chief_running += 1
            running_mission_rows.append({
                "id": m.id,
                "goal": m.goal[:120],
                "live_thread": bool(live_rt is not None),
                "active_steps": active_steps,
                "cost_usd": round(float(m.total_cost_usd), 6),
                "resume_count": int(m.resume_count),
            })
        elif m.status == "interrupted":
            chief_interrupted += 1

    # 3. Cost today (recorded costcenter + chief missions created/updated today)
    costcenter_today_usd = 0.0
    costcenter_available = False
    try:
        from . import costcenter

        db = costcenter.db_path(root)
        if db.exists():
            summary = costcenter.overview(root)
            costcenter_today_usd = float((summary.get("totals") or {}).get("today_usd") or 0.0)
            costcenter_available = True
    except Exception:
        costcenter_today_usd = 0.0

    chief_today_usd = 0.0
    for m in missions:
        ref_ts = float(m.finished_at or m.updated_at or m.created_at or 0.0)
        if day_start <= ref_ts < day_end:
            chief_today_usd += float(m.total_cost_usd or 0.0)
    chief_today_usd = round(chief_today_usd, 6)

    # Prefer costcenter when it already imported session calls; otherwise report Chief ledger cleanly
    total_cost_today = round(max(costcenter_today_usd, chief_today_usd), 6)

    # 4. Error rate across recent completed missions, Jarvis jobs, and sessions
    window_start = now_ts - 86400.0
    mission_succeeded = 0
    mission_failed = 0
    watchdog_stops = 0
    for m in missions:
        ref_ts = float(m.finished_at or m.updated_at or m.created_at or 0.0)
        if m.watchdog_reason and ref_ts >= window_start:
            watchdog_stops += 1
        if m.status == "done" and ref_ts >= window_start:
            mission_succeeded += 1
        elif m.status in {"failed", "escalated", "aborted_cost_cap", "error"} and ref_ts >= window_start:
            mission_failed += 1

    jarvis_succeeded = 0
    jarvis_failed = 0
    for row in jarvis_jobs:
        created = float(row.get("created") or 0.0)
        if created < window_start:
            continue
        status = row.get("status")
        if status in {"done", "completed"}:
            jarvis_succeeded += 1
        elif status in {"error", "failed", "interrupted"}:
            jarvis_failed += 1

    session_succeeded = 0
    session_failed = 0
    if sessions is None:
        try:
            from .sessions import SessionStore

            sessions = SessionStore()
        except Exception:
            sessions = None
    if sessions is not None:
        try:
            for s in sessions.recent(root, limit=100):
                created = float(s.get("created_at") or 0.0)
                if created < window_start:
                    continue
                if bool(s.get("ok")):
                    session_succeeded += 1
                else:
                    session_failed += 1
        except Exception:
            pass

    total_failed = mission_failed + jarvis_failed + session_failed
    total_succeeded = mission_succeeded + jarvis_succeeded + session_succeeded
    total_finished = total_failed + total_succeeded
    rate = round(total_failed / total_finished, 4) if total_finished > 0 else 0.0

    # 5. Scheduler summary
    from .scheduler import scheduler_summary

    sched_info = scheduler_summary(root)

    return {
        "schema": 1,
        "observed_at": now_ts,
        "queue_depth": {
            "total": jarvis_pending + chief_pending,
            "jarvis_pending": jarvis_pending,
            "chief_pending": chief_pending,
            "scheduled_active": int(sched_info.get("active", 0)),
        },
        "running_missions": {
            "count": chief_running,
            "jarvis_running": jarvis_running,
            "interrupted": chief_interrupted,
            "items": running_mission_rows,
        },
        "cost_today": {
            "usd": total_cost_today,
            "chief_usd": chief_today_usd,
            "costcenter_usd": round(costcenter_today_usd, 6),
            "costcenter_recorded": costcenter_available,
            "basis": "recorded operator/session ledger; not a provider invoice",
        },
        "error_rate": {
            "rate": rate,
            "failed": total_failed,
            "succeeded": total_succeeded,
            "total_finished": total_finished,
            "watchdog_stops": watchdog_stops,
            "window_seconds": 86400,
            "breakdown": {
                "missions": {"failed": mission_failed, "succeeded": mission_succeeded},
                "jarvis_jobs": {"failed": jarvis_failed, "succeeded": jarvis_succeeded},
                "sessions": {"failed": session_failed, "succeeded": session_succeeded},
            },
        },
        "scheduler": sched_info,
    }


__all__ = ["collect_self_monitoring"]
