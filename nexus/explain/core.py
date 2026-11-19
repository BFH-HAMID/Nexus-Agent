"""Grounded explanations and audit export.

Every sentence an explanation contains is a restatement of a field in a cited
audit row (or, for runs, in the stored run trace).  There is no model call and
no free text generation, so an explanation can be checked line by line against
``nexus audit tail``.  If the hash chain does not verify, or a cited row fails
its own hash, the explanation says so instead of presenting itself as verified.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any

from .. import policyfile
from . import policy
from .record import ACTION

DECISION_KINDS = (ACTION, "permission.decision", "tool.call")
OWN_TOOLS = frozenset({"explain_action"})
WINDOW_SECONDS = 120.0
_CSV_FIELDS = ("seq", "when", "who", "what", "target", "result", "risk", "reason", "args_sha256", "prev", "hash")


class ExplainError(policy.ExplainPolicyError):
    """An explanation or export could not be produced."""


def read_chain(workspace: Path | str) -> list[dict[str, Any]]:
    """All parseable audit rows in file order (the file is capped by the audit writer)."""
    from .. import securitycenter

    try:
        path = securitycenter.audit_file(workspace)
    except ValueError:
        return []
    if not path.exists() or path.is_symlink() or path.stat().st_size > securitycenter.AUDIT_LIMIT_BYTES:
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _kind(row: dict[str, Any]) -> str:
    return str(row.get("what") or row.get("action") or "")


def _row_ok(row: dict[str, Any]) -> bool:
    from .. import securitycenter

    try:
        return bool(row.get("hash")) and securitycenter._hash_of(row) == row.get("hash")
    except (TypeError, ValueError):
        return False


def _chain(workspace: Path | str) -> dict[str, Any]:
    from .. import audit

    report = audit.verify(workspace)
    return {"ok": bool(report.get("ok")), "events": report.get("events", 0), "first_bad": report.get("first_bad"),
            "head": str(report.get("head", ""))[:64]}


def _cite(row: dict[str, Any]) -> dict[str, Any]:
    return {"seq": row.get("seq"), "when": row.get("when", ""), "who": row.get("who") or row.get("actor", ""),
            "what": _kind(row), "target": str(row.get("target", ""))[:120], "result": str(row.get("result", ""))[:60],
            "hash": str(row.get("hash", ""))[:16], "intact": _row_ok(row)}


def _reasons(row: dict[str, Any]) -> list[str]:
    """Reasons recorded in (or directly restating) one audit row."""
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    kind = _kind(row)
    if kind == ACTION and isinstance(detail.get("factors"), list):
        return [str(item)[:400] for item in detail["factors"][:8]]
    if kind == "permission.decision":
        mode = str(row.get("mode") or detail.get("mode") or "")
        reason = str(row.get("reason") or "")
        lines = [f"permissions.yaml mode for '{row.get('tool') or row.get('target')}' was {mode}." if mode else ""]
        lines.append((f"It was approved ({reason})." if reason else "It was approved.") if row.get("approved")
                     else f"It was refused: {reason or 'no reason recorded'}.")
        return [line for line in lines if line]
    reason = str(detail.get("reason") or row.get("reason") or "")
    return [f"Recorded reason: {reason[:300]}."] if reason else []


def _find(rows: list[dict[str, Any]], seq: int | None) -> dict[str, Any] | None:
    if seq is not None:
        return next((row for row in rows if row.get("seq") == seq), None)
    for kinds in (DECISION_KINDS, None):
        for row in reversed(rows):
            kind = _kind(row)
            if str(row.get("target", "")) in OWN_TOOLS or (kind.startswith("explain.") and kind != ACTION):
                continue
            if kinds is None or kind in kinds:
                return row
    return None


def explain_action(workspace: Path | str, seq: int | None = None) -> dict[str, Any]:
    """Explain audit event ``seq``, or the most recent recorded decision when ``seq`` is None."""
    cfg = policy.require(workspace)
    if seq is not None and (type(seq) is not int or seq < 0):
        raise ExplainError("Give the audit sequence number of the action (see: nexus audit tail)")
    rows = read_chain(workspace)
    chain = _chain(workspace)
    row = _find(rows, seq)
    if row is None:
        return {"found": False, "chain": chain, "grounded": True, "evidence": [], "factors": [],
                "summary": ("No audit event has that sequence number." if seq is not None
                            else "No actions have been recorded for this workspace yet."),
                "warning": "" if chain["ok"] else _warning(chain)}
    at = float(row.get("at") or 0.0)
    digest = row.get("args_sha256")
    tool = str(row.get("tool") or row.get("target") or "")
    related = [other for other in rows
               if other is row or (abs(float(other.get("at") or 0.0) - at) <= WINDOW_SECONDS and (
                   (digest and other.get("args_sha256") == digest)
                   or (tool and str(other.get("tool") or other.get("target") or "") == tool
                       and abs(int(other.get("seq") or 0) - int(row.get("seq") or 0)) <= 3)))]
    related = sorted(related, key=lambda item: int(item.get("seq") or 0))[-cfg["max_evidence"]:]
    best = next((item for item in related if _kind(item) == ACTION), None) or next(
        (item for item in related if _kind(item) == "permission.decision"), row)
    detail = best.get("detail") if isinstance(best.get("detail"), dict) else {}
    result = str(best.get("result") or row.get("result") or "")
    reasons: list[str] = []
    for item in [best] + [other for other in related if other is not best]:
        for line in _reasons(item):
            if line not in reasons:
                reasons.append(line)
    evidence = [_cite(item) for item in related]
    intact = all(item["intact"] for item in evidence)
    summary = (f"{row.get('when', 'At an unknown time')}: '{tool or _kind(row)}' was {result or 'recorded'}"
               + (f" ({detail['risk_class']})" if detail.get("risk_class") else "")
               + (f" - {detail['preview']}" if detail.get("preview") else "") + ".")
    return {
        "found": True, "seq": row.get("seq"), "when": row.get("when", ""), "who": row.get("who") or row.get("actor", ""),
        "what": _kind(row), "target": tool, "result": result, "risk_class": str(detail.get("risk_class") or ""),
        "summary": summary, "factors": reasons[:8] or ["No reason was recorded for this event; only the event itself is known."],
        "evidence": evidence, "chain": chain, "grounded": True, "verified": bool(chain["ok"] and intact),
        "warning": "" if chain["ok"] and intact else _warning(chain, intact),
    }


def _warning(chain: dict[str, Any], intact: bool = True) -> str:
    if not intact:
        return "WARNING: a cited audit record fails its own hash; it was altered after it was written."
    bad = chain.get("first_bad") or {}
    return ("WARNING: the audit chain does not verify"
            + (f" (line {bad.get('line')}: {bad.get('reason')})" if bad else "")
            + "; these records may have been altered. Run: nexus audit verify")


def explain_run(workspace: Path | str, ident: str = "last", *, store: Any = None) -> dict[str, Any]:
    """Summarise the approval decisions of one recorded agent run (``nexus trace`` id, or ``last``)."""
    policy.require(workspace)
    from ..sessions import SessionStore

    store = store or SessionStore()
    if ident in {"", "last"}:
        items = store.trace_summaries(workspace)["items"]
        if not items:
            return {"found": False, "summary": "No recorded runs for this workspace yet.", "decisions": [],
                    "chain": _chain(workspace), "grounded": True}
        ident = str(items[0]["id"])
    ident = policyfile.safe_id(ident, label="run id", error=ExplainError)
    try:
        body = store.observation(ident, workspace)
    except ValueError as exc:
        raise ExplainError(str(exc)) from None
    events = body.get("events") if isinstance(body.get("events"), list) else []
    decisions = [{"step": event.get("step"), "tool": str(event["data"].get("tool", ""))[:80],
                  "allowed": bool(event["data"].get("allowed")), "scope": str(event["data"].get("scope", ""))[:20],
                  "reason": str(event["data"].get("reason", ""))[:200], "risk": str(event["data"].get("risk", ""))[:20]}
                 for event in events if event.get("kind") == "approval_decision" and isinstance(event.get("data"), dict)]
    approvals = body.get("approvals") if isinstance(body.get("approvals"), dict) else {}
    outcome = body.get("outcome") if isinstance(body.get("outcome"), dict) else {}
    allowed, denied = int(approvals.get("allowed", 0)), int(approvals.get("denied", 0))
    return {
        "found": True, "run": ident, "state": str(body.get("state", "")), "complete": bool(body.get("complete")),
        "task": str(body.get("trigger", ""))[:300], "allowed": allowed, "denied": denied,
        "tool_calls": sum(1 for event in events if event.get("kind") == "tool_call"),
        "model_calls": len(body.get("models") or []), "reported_ok": outcome.get("reported_ok"),
        "decisions": decisions[:40], "truncated": len(decisions) > 40,
        "summary": (f"Run {ident} ({body.get('state', 'unknown')}): {allowed} action(s) allowed, {denied} refused"
                    + ("" if body.get("complete") else "; the trace is incomplete") + "."),
        "source": "run trace (not hash-chained)", "chain": _chain(workspace), "grounded": True,
    }


def render(result: dict[str, Any]) -> str:
    lines = [result["summary"]]
    if "decisions" in result:
        if result.get("task"):
            lines.append("Task: " + result["task"])
        lines.extend(f" {'✓' if row['allowed'] else '✗'} step {row['step']}: {row['tool']} ({row['risk']}) - "
                     f"{row['reason'] or row['scope'] or 'no reason recorded'}" for row in result["decisions"])
        if result.get("found"):
            lines.append(f"Source: {result['source']}.")
    else:
        lines.extend(" - " + line for line in result.get("factors", []))
        if result.get("evidence"):
            lines.append("Evidence: " + ", ".join(f"#{row['seq']} {row['what']}" for row in result["evidence"]))
    chain = result.get("chain") or {}
    lines.append(result.get("warning") or (f"Audit chain verified ({chain.get('events', 0)} events)." if chain.get("ok")
                                           else _warning(chain)))
    return "\n".join(lines)


def spoken(result: dict[str, Any]) -> str:
    """Short form for voice: what happened, the two strongest reasons, and any integrity warning."""
    parts = [result["summary"]] + list(result.get("factors", []))[1:3]
    if result.get("warning"):
        parts.append("Note: the audit trail failed verification, so treat this with caution.")
    return " ".join(parts)


# -- export ------------------------------------------------------------------------- #
def _cell(value: Any) -> str:
    """CSV cell that a spreadsheet will not run as a formula."""
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t") else text


def export(workspace: Path | str, fmt: str = "jsonl", *, since: float | None = None, until: float | None = None,
           action: str = "", clock=time.time) -> tuple[str, dict[str, Any]]:
    """``(text, manifest)`` for the audit trail. Rows are exactly as stored (already redacted at write time)."""
    cfg = policy.require(workspace)
    if fmt not in {"jsonl", "json", "csv"}:
        raise ExplainError("Export format must be jsonl, json or csv")
    rows = [row for row in read_chain(workspace)
            if (since is None or float(row.get("at") or 0) >= since) and (until is None or float(row.get("at") or 0) <= until)
            and (not action or _kind(row).startswith(action))]
    limit = cfg["export"]["max_events"]
    truncated = len(rows) > limit
    rows = rows[-limit:]
    chain = _chain(workspace)
    manifest: dict[str, Any] = {
        "schema": 1, "format": fmt, "events": len(rows), "truncated": truncated,
        "first_seq": rows[0].get("seq") if rows else None, "last_seq": rows[-1].get("seq") if rows else None,
        "chain_ok": chain["ok"], "chain_head": chain["head"], "chain_first_bad": chain["first_bad"],
        "rows_intact": all(_row_ok(row) for row in rows), "exported_at": round(clock(), 3),
        "filter": {"since": since, "until": until, "action": action},
    }
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(_CSV_FIELDS)
        for row in rows:
            detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
            flat = {**row, "what": _kind(row), "who": row.get("who") or row.get("actor", ""),
                    "reason": row.get("reason") or detail.get("reason", "")}
            writer.writerow([_cell(flat.get(field)) for field in _CSV_FIELDS])
        text = buffer.getvalue()
    else:
        text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    # The digest always covers the event lines, so a JSON export can be checked the same way as JSONL.
    manifest["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if fmt == "json":
        text = json.dumps({"manifest": manifest, "events": rows}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    policyfile.audit(workspace, "explain.export", who="operator", target=fmt, result="exported", risk="low",
                     detail={"events": len(rows), "sha256": manifest["sha256"], "chain_ok": chain["ok"]})
    return text, manifest


def write_export(path: Path | str, text: str, manifest: dict[str, Any]) -> Path:
    """Write an export (0600) plus ``<file>.manifest.json``. Never overwrites and never follows a symlink."""
    import os

    target = Path(path).expanduser()
    sidecar = target.with_name(target.name + ".manifest.json")
    for item in (target, sidecar):
        if item.exists() or item.is_symlink():
            raise ExplainError(f"Refusing to overwrite {item}")
    if not target.parent.is_dir():
        raise ExplainError(f"Directory does not exist: {target.parent}")
    for item, body in ((target, text), (sidecar, json.dumps(manifest, indent=2, sort_keys=True) + "\n")):
        fd = os.open(item, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
    return target
