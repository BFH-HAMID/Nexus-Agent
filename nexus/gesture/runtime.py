"""Attach gesture control to a running voice pipeline (``nexus voice run``)."""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

from . import policy


def attach(workspace: Path | str, pipeline: Any, stop: threading.Event):
    """Start the camera watcher bound to ``pipeline``; ``None`` while gesture control is OFF or unusable."""
    if not policy.enabled(workspace):
        return None
    try:
        from .camera import GestureWatcher, missing
        from .controller import GestureController, Sinks

        absent = missing()
        if absent:
            print(f"gesture: enabled but not started - install {', '.join(absent)}", file=sys.stderr)
            return None

        def tainted() -> bool:
            # Fail closed: without a known approval gate the session counts as tainted,
            # so a gesture can deny but never approve.
            gate = getattr(pipeline, "active_gate", None)
            return True if gate is None else bool(getattr(gate, "tainted", True))

        sinks = Sinks(approvals=getattr(pipeline, "approval_manager", None), tainted=tainted,
                      stop_speaking=getattr(pipeline, "interrupt_speech", None))
        watcher = GestureWatcher(GestureController(workspace, sinks=sinks), stop)
        watcher.start()
        return watcher
    except Exception as exc:  # noqa: BLE001 - voice must start even when gestures can not
        print(f"gesture: not started - {exc}", file=sys.stderr)
        return None
