"""``/devpilot`` - read-only views. Runs and applies need the CLI or the approval-gated tools."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import policy


def slash(workspace: Path | str, argument: str, ctx: dict[str, Any] | None = None) -> str:
    from . import core

    if not policy.enabled(workspace):
        return "DevPilot is OFF (operator opt-in: devpilot.yaml enabled: true)."
    words = argument.split()
    if len(words) == 2 and words[0] in {"show", "diff"}:
        return core.render(core.load_run(workspace, words[1])) if words[0] == "show" else core.diff(workspace, words[1], 8000)
    if words:
        return "Usage: /devpilot | /devpilot show RUN | /devpilot diff RUN  (start runs with `nexus devpilot run`)"
    rows = core.runs(workspace, 5)
    found = core.DevPilot(workspace).commands()
    lines = [f"DevPilot is ON. Test command: {found['test']['display'] or '(none)'} [{found['test']['source']}]"]
    lines += [f"{row['id']}  {row['status']:<10} {row['task'][:60]}" for row in rows] or ["No runs yet."]
    return "\n".join(lines)
