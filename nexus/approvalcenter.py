"""Approval Center: one reviewed inbox for every decision this dashboard can answer.

The workspace already asks for consent in three real places:

* a live task pauses inside the agent's approval gate (`/api/approve/<task>`);
* a background Jarvis job waits for a one-use action approval
  (`nexus/jarvis/approvals.py`);
* a queued task waits for admission (`nexus/jarvis/queue.py`).

Each one had its own screen and its own consent phrasing, and none of them
presented the three together or kept a durable record of the decision. This
module is a read-and-decide projection over those existing stores: it never
starts a run, never grants a permission and never invents a pending item. Every
grant still goes through the same gate that raised it, with the same exact
token; this file only asks for it in one place.

Rules that shape everything here:

* **No second control plane.** Decisions call the existing gate, the existing
  Jarvis approval store and the existing queue review. Nothing is re-implemented.
* **Nothing is invented.** An item is listed only while its real record still
  exists, is inside its real deadline and still matches its owning job/run.
  Expired, consumed and withdrawn requests are reported as such, never revived.
* **Deny is free, grant is typed.** Refusing needs one click; approving needs the
  exact token that request published (`APPROVE ACTION <id>`, `ALLOW ACTION …`,
  `APPROVE QUEUED TASK <id>`). A latched emergency stop refuses every grant.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = 1
SOURCES = ("live", "action", "queue")
DECISIONS = {"live": ("approve", "deny"), "action": ("allow", "deny"),
             "queue": ("approve", "reject", "cancel")}
MAX_ITEMS = 100
MAX_TEXT = 4000
MAX_ARGS = 20_000
MAX_COMPUTER_ARGS = 200_000  # preparation already caps inputs + target metadata below this
MAX_HISTORY = 100
_audit_lock = threading.Lock()
_audit_at: dict[str, float] = {}


class Stale(ValueError):
    """The request is no longer answerable; refresh instead of retrying."""


def audit_read(state, client: str, action: str, *, every: float = 300.0) -> bool:
    """Sample reads into the same audit trail the rest of the dashboard uses."""
    from . import webapi

    now = time.time()
    key = f"{client}|{action}"
    with _audit_lock:
        if every > 0 and now - _audit_at.get(key, 0.0) < every:
            return False
        _audit_at[key] = now
    try:
        webapi.audit_api(Path(state.workspace), action, actor="user", target="dashboard", result="ok",
                         risk="low", detail={"client": client})
    except (OSError, ValueError):
        return False
    return True


def _hud(state):
    """The same HUD instance the Operations page uses (one Studio, one queue)."""
    return state.gui.operations()


def _clean(state, data: Any) -> Any:
    from .redaction import mask, secrets

    workspace = Path(state.workspace)
    values: list[str] = [str(getattr(state, "token", "") or "")] if getattr(state, "token", "") else []
    try:
        values.extend(secrets(workspace))
    except (OSError, ValueError):
        pass
    return json.loads(mask(json.dumps(data, allow_nan=False, default=str), values))


def _bounded(value: Any, limit: int, label: str, *, allow_empty: bool = True) -> str:
    if value is None and allow_empty:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    text = value.strip()
    if not text and not allow_empty:
        raise ValueError(f"{label} is required")
    if len(text) > limit:
        raise ValueError(f"{label} must be under {limit} characters")
    return text


def _seconds_left(expires: Any) -> int | None:
    if not isinstance(expires, (int, float)) or not expires:
        return None
    return max(0, int(float(expires) - time.time()))


def _live_items(state) -> list[dict[str, Any]]:
    from .computer_actions import PREPARED_TOOLS
    rows: list[dict[str, Any]] = []
    sessions = list(getattr(state, "sessions", {}).values())
    with getattr(state, "lock", _NullLock()):
        sessions = list(getattr(state, "sessions", {}).values())
        for session in sessions:
            pending = getattr(session, "pending", None)
            if not pending or getattr(session, "cancel", None) is not None and session.cancel.is_set():
                continue
            approval_id = str(pending.get("approval_id") or "")
            if not approval_id:
                continue
            requested = pending.get("requested") or getattr(session, "created", time.time())
            timeout = pending.get("timeout")
            expires = (requested + float(timeout)) if isinstance(timeout, (int, float)) else None
            rows.append({
                "id": f"live:{session.task_id}:{approval_id}",
                "source": "live",
                "reference": session.task_id,
                "approval_id": approval_id,
                "tool": str(pending.get("tool") or "")[:120],
                "risk": str(pending.get("risk") or "")[:40],
                "title": str(getattr(session, "task", "") or "")[:240],
                "detail": str(getattr(session, "role", "") or "")[:80],
                "requested": requested,
                "expires": expires,
                "seconds_left": _seconds_left(expires),
                "args": pending.get("args") or {},
                "preview": str(pending.get("preview") or "")[:MAX_TEXT],
                "proposal": pending.get("proposal") or {},
                "token": f"APPROVE ACTION {approval_id}",
                "decisions": list(DECISIONS["live"]),
                "scopes": ["once"] if pending.get("tool") in PREPARED_TOOLS or getattr(session, "computer_request", None) is not None else ["once", "session"],
            })
    return rows


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _action_items(state) -> list[dict[str, Any]]:
    from .jarvis.approvals import Approvals

    hud = _hud(state)
    studio = hud.studio
    now = time.time()
    rows: list[dict[str, Any]] = []
    jobs = {row["id"]: row for row in studio.items("jarvis-job")}
    for row in Approvals(studio).items():
        if row.get("state") != "pending" or float(row.get("expires") or 0) <= now:
            continue
        job = jobs.get(row.get("job_id"))
        if not job or job.get("status") != "running" or job.get("lease") != row.get("lease"):
            continue
        rows.append({
            "id": f"action:{row['id']}",
            "source": "action",
            "reference": row["id"],
            "job_id": row.get("job_id"),
            "tool": str(row.get("tool") or "")[:120],
            "risk": str(row.get("risk") or "")[:40],
            "title": str(job.get("task") or "")[:240],
            "detail": f"job {row.get('job_id')} · {job.get('active_role') or 'unrouted'}",
            "requested": row.get("created") or row.get("updated_at"),
            "expires": row.get("expires"),
            "seconds_left": _seconds_left(row.get("expires")),
            "args": row.get("arguments") or {},
            "preview": str(row.get("preview") or "")[:MAX_TEXT],
            "proposal": row.get("proposal") or {},
            "token": str(row.get("confirmation") or ""),
            "decisions": list(DECISIONS["action"]),
            "scopes": [],
        })
    return rows


def _queue_items(state) -> list[dict[str, Any]]:
    from .jarvis.queue import Queue

    queue = Queue(_hud(state).studio)
    rows: list[dict[str, Any]] = []
    for row in queue._jobs():                        # noqa: SLF001 - the queue owner's own reader
        if row.get("status") != "awaiting_approval":
            continue
        if float(row.get("expires") or 0) <= time.time():
            continue
        rows.append({
            "id": f"queue:{row['id']}",
            "source": "queue",
            "reference": row["id"],
            "job_id": row.get("id"),
            "tool": "queued task",
            "risk": "admission",
            "title": str(row.get("task") or "")[:240],
            "detail": f"priority {row.get('priority')} · {row.get('source') or 'operator enqueue'}",
            "requested": row.get("created"),
            "expires": row.get("expires"),
            "seconds_left": _seconds_left(row.get("expires")),
            "args": {"priority": row.get("priority"), "estimated_cost": row.get("estimated_cost"),
                     "depth": row.get("depth")},
            "preview": "",
            "proposal": {},
            "token": f"APPROVE QUEUED TASK {row['id']}",
            "decisions": list(DECISIONS["queue"]),
            "scopes": [],
        })
    return rows


def _truncate(item: dict[str, Any]) -> dict[str, Any]:
    from .computer_actions import PREPARED_TOOLS
    args = item.get("args")
    computer = item.get("tool") in PREPARED_TOOLS
    try:
        if len(json.dumps(args, default=str, ensure_ascii=False).encode('utf-8')) > (MAX_COMPUTER_ARGS if computer else MAX_ARGS):
            item["args"] = {"truncated": True, "note": "Arguments exceeded the display budget; open the job for the full record."}
            if computer:
                item['decisions'] = ['deny']  # never approve a clipped native request here
    except (TypeError, ValueError):
        item["args"] = {"truncated": True}
    item["preview"] = str(item.get("preview") or "")[:MAX_TEXT]
    return item


def counts(state) -> dict[str, int]:
    """How many decisions are waiting, by source. Expensive sources are counted once."""
    try:
        live = len(_live_items(state))
    except (OSError, ValueError, KeyError):
        live = -1
    try:
        actions = len(_action_items(state))
    except (OSError, ValueError, KeyError):
        actions = -1
    try:
        queue = len(_queue_items(state))
    except (OSError, ValueError, KeyError):
        queue = -1
    total = sum(value for value in (live, actions, queue) if value > 0)
    return {"live": live, "action": actions, "queue": queue, "total": total}


def _sources(state) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for label, builder in (("live", _live_items), ("action", _action_items), ("queue", _queue_items)):
        try:
            rows.extend(builder(state))
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"{label}: {str(exc)[:200] or 'store unavailable'}")
    rows = [_truncate(row) for row in rows]
    rows.sort(key=lambda row: (row.get("expires") or float("inf"), row.get("requested") or 0))
    return rows[:MAX_ITEMS], errors


def inbox(state) -> dict[str, Any]:
    """Every real request waiting for a decision, oldest deadline first."""
    rows, errors = _sources(state)
    tally = {name: sum(1 for row in rows if row["source"] == name) for name in SOURCES}
    return _clean(state, {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "items": rows,
        "counts": {**tally, "total": len(rows)},
        "limits": {"items": MAX_ITEMS, "text": MAX_TEXT},
        "errors": errors,
        # Stable ids so the page can translate them; the text is the English fallback.
        "notes": [
            {"id": "record", "text": "A request appears here only while its own record still exists and is inside its deadline."},
            {"id": "tokens", "text": "Deny needs one click. A grant needs the exact token this request published."},
            {"id": "queue", "text": "Approving a queued task admits the task, not its tools; each mutation asks again."},
        ],
    })


def policy(state) -> dict[str, Any]:
    """The real gate policy behind the inbox, including what is not enforced here."""
    from .computer_control import EmergencyStop
    from .jarvis.inbox import Inbox
    from .jarvis.queue import Queue
    from .permissions import FORCED_REVIEW, Permissions

    workspace = Path(state.workspace)
    hud = _hud(state)
    try:
        loaded = Permissions(workspace).load()
        permissions: dict[str, Any] = {
            "readable": True,
            "default": loaded["default"],
            "tools": {"count": len(loaded["tools"]),
                      "rows": [{"tool": name, "mode": mode} for name, mode in sorted(loaded["tools"].items())[:40]]},
            "computer": {key: loaded["computer"].get(key) for key in
                         ("browser", "editor", "office", "shell_backend")},
        }
    except (OSError, ValueError) as exc:
        permissions = {"readable": False, "error": str(exc)[:300]}
    try:
        queue_policy = Queue(hud.studio).policy()
        autonomy: dict[str, Any] = {
            "readable": True,
            "level": queue_policy["level"],
            "runtime_enabled": bool(queue_policy["runtime"].get("enabled")),
            "local_model": queue_policy["runtime"].get("local_model"),
            "max_queue": queue_policy["max_queue"],
            "ttl_seconds": queue_policy["ttl_seconds"],
        }
    except (OSError, ValueError) as exc:
        autonomy = {"readable": False, "error": str(exc)[:300]}
    try:
        delivery = Inbox(hud.studio).delivery_policy()
    except (OSError, ValueError):
        delivery = {}
    return _clean(state, {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "demo": bool(getattr(state, "demo", False)),
        "permissions": permissions,
        "autonomy": autonomy,
        "latch": {"stopped": EmergencyStop().stopped()},
        "queue_paused": bool(getattr(state, "queue_paused", False)),
        "delivery": {"quiet": bool(delivery.get("quiet")), "dnd_until": delivery.get("dnd_until") or 0,
                     "transport": delivery.get("transport", "local")},
        "counts": counts(state),
        "always_review": sorted(FORCED_REVIEW),
        "not_checked": [
            {"id": "scope", "text": "A session-scoped decision covers this run only; it is not saved as policy."},
            {"id": "audit", "text": "The audit trail records the decisions and API calls that actually happened, not what a model intended."},
            {"id": "safety", "text": "This page cannot prove an action was safe; it shows what was asked and who answered."},
            {"id": "expiry", "text": "Expired or consumed requests are not revived here — the agent must ask again."},
        ],
    })


def history(state, limit: int = 40) -> dict[str, Any]:
    """Decisions that were actually recorded, newest first."""
    from . import webapi

    limit = max(1, min(int(limit), MAX_HISTORY))
    rows: list[dict[str, Any]] = []
    for event in webapi.read_audit(Path(state.workspace), limit=500):
        if event.get("action") != "approval.decide":
            continue
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        rows.append({
            "at": event.get("at"), "source": detail.get("source") or "center",
            "reference": detail.get("reference"), "decision": detail.get("decision"),
            "scope": detail.get("scope") or "", "tool": detail.get("tool") or "",
            "risk": detail.get("risk") or event.get("risk") or "", "recorded": "audit",
        })
    try:
        from .jarvis.approvals import Approvals

        for row in Approvals(_hud(state).studio).items():
            state_name = row.get("state")
            if state_name not in {"allowed", "denied", "consumed", "expired"}:
                continue
            at = row.get("consumed") or row.get("decided") or row.get("expires") or row.get("updated_at")
            rows.append({
                "at": at, "source": "action", "reference": row.get("id"),
                "decision": state_name, "scope": "once", "tool": str(row.get("tool") or "")[:120],
                "risk": str(row.get("risk") or "")[:40],
                "recorded": "approval store" if state_name != "consumed" else "consumed by the job",
            })
    except (OSError, ValueError, KeyError):
        pass
    rows = [row for row in rows if isinstance(row.get("at"), (int, float))]
    rows.sort(key=lambda row: row["at"], reverse=True)
    return _clean(state, {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "items": rows[:limit],
        "total": len(rows),
        "limits": {"requested": limit, "kept": MAX_HISTORY},
        "sources": [
            {"name": "audit", "path": "nexus_home/permissions/<workspace>/audit.jsonl",
             "note": "Every decision answered through this page."},
            {"name": "action", "path": "Studio store: jarvis-approval",
             "note": "Background action decisions, including ones consumed or expired."},
        ],
        "notes": ["Only recorded decisions appear here; nothing is reconstructed from memory."],
    })


def _stale(message: str) -> Stale:
    return Stale(message)


def decide(state, payload: dict[str, Any]) -> dict[str, Any]:
    """Answer one request through the gate that raised it."""
    from . import webapi
    from .computer_control import EmergencyStop

    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required")
    if getattr(state, "demo", False):
        raise ValueError("Live mode is required to answer approvals.")
    source = payload.get("source")
    if source not in SOURCES:
        raise ValueError("source must be live, action or queue")
    reference = _bounded(payload.get("id"), 200, "id", allow_empty=False)
    decision = payload.get("decision")
    if decision not in DECISIONS[source]:
        raise ValueError(f"decision for {source} must be one of {', '.join(DECISIONS[source])}")
    confirmation = _bounded(payload.get("confirmation"), 300, "confirmation")
    reason = _bounded(payload.get("reason"), 300, "reason")
    scope = payload.get("scope") or "once"
    if scope not in {"once", "session"}:
        raise ValueError("scope must be once or session")
    client = _bounded(payload.get("_client"), 120, "client")
    tool = ""
    grant = decision in {"approve", "allow"}

    if source == "live":
        approval_id = _bounded(payload.get("approval_id"), 80, "approval_id", allow_empty=False)
        session = state.get(reference)
        if session is None or not getattr(session, "pending", None):
            raise _stale("This request is no longer waiting; refresh the inbox.")
        tool = str(session.pending.get("tool") or "")
        if session.pending.get("approval_id") != approval_id:
            raise _stale("A newer request replaced this one; refresh the inbox.")
        if grant and confirmation != f"APPROVE ACTION {approval_id}":
            raise ValueError(f"Type APPROVE ACTION {approval_id} to approve this action.")
        status, body = state.decide_approval(reference, grant, scope, approval_id,
                                             reason=reason or "answered in the Approval Center")
        if status == 409:
            raise _stale(str(body.get("error") or "The request is no longer pending."))
        if status >= 400:
            raise ValueError(str(body.get("error") or "The approval was refused."))
    elif source == "action":
        from .jarvis.approvals import Approvals

        hud = _hud(state)
        try:
            row = hud.studio.find("jarvis-approval", reference)
        except ValueError:
            raise _stale("This background approval no longer exists.") from None
        tool = str(row.get("tool") or "")
        if row.get("state") != "pending":
            raise _stale(f"This approval is {row.get('state')}; a new decision needs a new request.")
        if grant and EmergencyStop().stopped():
            raise ValueError("Emergency stop is latched; every grant is refused until it is cleared.")
        ApprovalDecisions = {"allow": True, "deny": False}
        hud.action("decide", {"id": reference, "allow": ApprovalDecisions[decision],
                              "confirmation": confirmation})
        del Approvals  # the HUD owns the store; this import documents the owner
    else:
        hud = _hud(state)
        try:
            job = hud.studio.find("jarvis-job", reference)
        except ValueError:
            raise Stale("This queued task no longer exists.") from None
        tool = "queued task"
        permitted = {"approve": {"awaiting_approval", "approved"},
                     "reject": {"awaiting_approval", "approved"},
                     "cancel": {"awaiting_approval", "approved", "running"}}[decision]
        if job.get("status") not in permitted:
            verb = {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}[decision]
            raise Stale(f"This task is {job.get('status')}; it cannot be {verb} now.")
        hud.action("review", {"id": reference, "action": decision, "confirmation": confirmation})

    webapi.audit_api(Path(state.workspace), "approval.decide", actor="user", target=reference,
                     result=decision, risk="high",
                     detail={"source": source, "reference": reference, "decision": decision,
                             "scope": scope if source == "live" else "", "tool": tool[:120],
                             "client": client[:120] or "local"})
    remaining = counts(state)
    return _clean(state, {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "decided": {"source": source, "reference": reference, "decision": decision,
                    "scope": scope if source == "live" else "", "tool": tool[:120]},
        "counts": remaining,
        "note": ("Answered through the same gate that raised the request."
                 if source == "live" else
                 "Recorded by the existing background approval store; the job decides what happens next."
                 if source == "action" else
                 "Recorded as a queue review; queued-task approval never approves that task's tools."),
    })
