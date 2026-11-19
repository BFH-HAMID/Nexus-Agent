"""``/docs`` in the REPL: read-only views. Filing is done with ``nexus docs-auto file`` or the approved tool."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import policy

OFF = "Document automation is OFF (operator opt-in: docs_auto.yaml enabled: true)."


def slash(workspace: Path | str, argument: str, ctx: dict[str, Any] | None = None) -> str:
    """``/docs`` | ``/docs scan`` | ``/docs find WORDS``."""
    if not policy.enabled(workspace):
        return OFF
    from . import status
    from .core import DocsAuto, brief, printable

    ctx = ctx or {}
    docs = DocsAuto(workspace, ctx.get("config"), ctx.get("providers"), keyring_module=ctx.get("keyring_module"))
    verb, _, rest = argument.strip().partition(" ")
    if verb in ("", "status"):
        info = status(workspace)
        return (f"inbox {info['inbox']}/ -> archive {info['archive']}/; {info.get('waiting', 0)} waiting, "
                f"{info.get('filed', 0)} filed" + (f"; missing: {', '.join(info['missing'])}" if info.get("missing") else ""))
    if verb == "scan":
        found = docs.scan()
        lines = [brief(row) for row in found["documents"]] or ["nothing new in the inbox"]
        return "\n".join(lines + [printable(f"skipped {row['name']}: {row['reason']}") for row in found["skipped"]]
                         + ["to file: nexus docs-auto file ID"])
    if verb == "find" and rest.strip():
        return "\n".join(brief(row) for row in docs.find(rest)) or "no match"
    return "Usage: /docs | /docs scan | /docs find WORDS"
