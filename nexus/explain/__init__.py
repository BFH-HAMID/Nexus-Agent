"""Task 21 - explainability and audit export (opt-in, ``explain.yaml``).

Records *why* each approval decision was made in the hash-chained audit trail
and answers "why did you do that?" from those records only.
See ``docs/explain.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import policy


def status(workspace: Path | str) -> dict[str, Any]:
    info = policy.POLICY.describe(workspace)
    if not info.get("enabled"):
        return info
    cfg = policy.load(workspace)
    info.update(record=cfg["record"], voice=cfg["voice"], uses_model=False)
    try:
        from .core import _chain, read_chain
        from .record import ACTION

        chain = _chain(workspace)
        info.update(chain_ok=chain["ok"], audit_events=chain["events"],
                    explained_decisions=sum(1 for row in read_chain(workspace) if row.get("what") == ACTION))
        if not chain["ok"]:
            info["error"] = "audit chain does not verify; run: nexus audit verify"
    except (ValueError, OSError) as exc:
        info["error"] = str(exc)[:240]
    return info
