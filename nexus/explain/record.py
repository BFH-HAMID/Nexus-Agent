"""Record *why* the approval gate decided what it decided.

``install`` chains one observer onto ``ApprovalGate.decision_observer``.  It
calls the observer that was there before (trace recorders keep working), then
appends an ``explain.decision`` row to the hash-chained audit trail.  The row is
derived from the decision object only: nothing here can change a decision, and
a failing audit write never turns into an exception inside the gate.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import policy

MARK = "_nexus_explain_installed"
ACTION = "explain.decision"
_AUTO_REASONS = {
    "read-only": "It only reads, and policy lets read-only calls run without asking.",
    "--yes": "The session runs in auto mode (--yes) and this call did not need a fresh approval.",
    "pre-approved": "The tool was pre-approved (always_allow, or allowed for this session earlier).",
}


def factors(spec: Any, args: dict[str, Any], decision: Any, gate: Any) -> list[str]:
    """Plain-language reasons, each one a restatement of a field on the decision or the gate."""
    risk_class = str(getattr(decision, "risk_class", "") or "")
    lines = [f"The tool '{spec.name}' is declared '{spec.risk.value}'"
             + (f" and was classified {risk_class} for these arguments." if risk_class else ".")]
    reason = str(getattr(decision, "reason", "") or "")
    scope = str(getattr(decision, "scope", "") or "")
    if not decision.approved:
        lines.append(f"It was refused: {reason or 'the approval channel said no'}.")
    elif scope == "auto":
        lines.append(_AUTO_REASONS.get(reason) or (
            "permissions.yaml allows this tool automatically (AUTO_ALLOW)." if reason.startswith("permissions.yaml: AUTO_ALLOW")
            else f"Policy approved it automatically ({reason or 'no reason recorded'})."))
    else:
        lines.append(f"It was approved through the approval prompt (scope: {scope or 'once'})"
                     + (f"; note: {reason}." if reason else "."))
    if getattr(gate, "tainted", False):
        sources = ", ".join(str(item) for item in list(getattr(gate, "taint_sources", []))[:3])
        lines.append("Untrusted content had been read in this session"
                     + (f" ({sources})" if sources else "") + ", so high-impact actions needed a fresh approval.")
    if getattr(decision, "second_factor_verified", False):
        lines.append("A second factor was verified.")
    if getattr(decision, "notify_after", False) and decision.approved:
        lines.append("Policy asked for a notification after the action.")
    if getattr(gate, "mode", "") == "plan":
        lines.append("The session was in plan mode, which is read-only.")
    return [line[:400] for line in lines]


def _wanted(level: str, decision: Any) -> bool:
    if level == "all":
        return True
    if not decision.approved:
        return level in {"non_safe", "denied"}
    return level == "non_safe" and str(getattr(decision, "risk_class", "") or "").upper() != "SAFE"


def record(workspace: Path | str, gate: Any, spec: Any, args: dict[str, Any], decision: Any) -> dict[str, Any] | None:
    """Append one ``explain.decision`` row. Never raises; returns the row or ``None``."""
    try:
        cfg = policy.load(workspace)
        if not cfg["enabled"] or not _wanted(cfg["record"], decision):
            return None
        from .. import audit
        from ..computer_actions import PREPARED_TOOLS
        from ..redaction import redact_text
        from ..tools import preview_args

        preview = "" if spec.name in PREPARED_TOOLS else redact_text(preview_args(spec, args, limit=160), workspace)
        return audit.record(
            workspace, ACTION, who="gate", target=spec.name, result="approved" if decision.approved else "denied",
            risk=spec.risk.value,
            detail={"tool": spec.name, "risk_class": str(getattr(decision, "risk_class", "") or ""),
                    "scope": str(getattr(decision, "scope", "") or ""), "reason": str(decision.reason or "")[:300],
                    "mode": str(getattr(gate, "mode", "") or ""), "tainted": bool(getattr(gate, "tainted", False)),
                    "second_factor": bool(getattr(decision, "second_factor_verified", False)),
                    "preview": preview[:200], "factors": factors(spec, args, decision, gate)},
            extra={"args_sha256": audit.args_digest(args)})
    except Exception:  # noqa: BLE001 - explaining must never break or alter a gate decision
        return None


def install(gate: Any, workspace: Path | str) -> bool:
    """Chain the explain observer onto ``gate`` once. Returns True when it was installed now."""
    if gate is None or getattr(gate, MARK, False) or not policy.enabled(workspace):
        return False
    previous = getattr(gate, "decision_observer", None)
    root = Path(workspace).resolve()

    def explain_observer(spec, args, decision):
        try:
            if previous is not None:
                previous(spec, args, decision)
        finally:
            record(root, gate, spec, args, decision)

    gate.decision_observer = explain_observer
    setattr(gate, MARK, True)
    return True
