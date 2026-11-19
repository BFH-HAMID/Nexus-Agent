"""Task 23 - hand-gesture control (opt-in, ``gesture.yaml`` + ``gestures.yaml``).

A local camera, a pure-Python pose classifier and a closed action table.
See ``docs/gesture.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import policy


def status(workspace: Path | str) -> dict[str, Any]:
    info = policy.POLICY.describe(workspace)
    if not info.get("enabled"):
        return info
    try:
        from .camera import NO_MODEL, hand_model, missing, needs_model

        cfg = policy.load(workspace)
        table = policy.bindings(workspace, cfg)
        info.update(missing=missing(), bindings=table, arming=cfg["arming"]["gesture"] if cfg["arming"]["required"] else "",
                    approve_by_gesture=bool(cfg["approve"]["enabled"] and "approve" in table.values()),
                    approve_max_risk=cfg["approve"]["max_risk"], frames_stored=False,
                    hand_model="operator file" if cfg["camera"]["hand_model"] else "built into MediaPipe")
        if not table:
            info["error"] = "no bindings: enable gestures.yaml"
        elif cfg["camera"]["hand_model"]:
            hand_model(workspace, cfg["camera"]["hand_model"])      # raises with the reason
        elif needs_model():
            info["error"] = NO_MODEL
    except (ValueError, OSError) as exc:
        info["error"] = str(exc)[:240]
    return info
