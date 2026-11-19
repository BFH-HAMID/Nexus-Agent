"""Operator policies for gesture control (``gesture.yaml`` and ``gestures.yaml``)."""
from __future__ import annotations

from typing import Any

from .. import policyfile

NAME = "gesture.yaml"
BINDINGS_NAME = "gestures.yaml"
POSES = ("open_palm", "fist", "thumbs_up", "thumbs_down", "peace", "point_up")
ACTIONS = ("none", "emergency_stop", "deny", "approve", "stop_speaking", "focus_on", "focus_off")

DEFAULTS = {
    "schema": 1,
    "enabled": False,
    "camera": {"device": 0, "width": 640, "height": 480, "fps": 10, "hand_model": ""},
    "min_confidence": 0.7,
    "hold_ms": 600,
    "cooldown_ms": 1500,
    "max_per_minute": 12,
    "arming": {"required": True, "gesture": "open_palm", "window_seconds": 5},
    "approve": {"enabled": False, "max_risk": "MEDIUM"},
}
BINDINGS_DEFAULTS = {"schema": 1, "enabled": False, "bindings": {pose: "none" for pose in POSES}}


class GesturePolicyError(policyfile.PolicyError):
    """gesture.yaml / gestures.yaml is invalid or gesture control is OFF."""


POLICY = policyfile.Policy(
    NAME, DEFAULTS, label="Gesture control", error=GesturePolicyError,
    ranges={"camera.device": (0, 16), "camera.width": (160, 1920), "camera.height": (120, 1080), "camera.fps": (1, 30),
            "min_confidence": (0.5, 1.0), "hold_ms": (200, 5000), "cooldown_ms": (0, 60_000),
            "max_per_minute": (1, 60), "arming.window_seconds": (1, 30)},
    choices={"arming.gesture": POSES, "approve.max_risk": ("SAFE", "MEDIUM")},
)
BINDINGS = policyfile.Policy(
    BINDINGS_NAME, BINDINGS_DEFAULTS, label="Gesture bindings", error=GesturePolicyError,
    choices={f"bindings.{pose}": ACTIONS for pose in POSES},
)
load = POLICY.load
require = POLICY.require
enabled = POLICY.enabled
digest = policyfile.digest


def bindings(workspace, cfg: dict[str, Any] | None = None) -> dict[str, str]:
    """Active pose -> action map (``{}`` while gestures.yaml is OFF). Raises on an invalid combination."""
    cfg = cfg or load(workspace)
    table = BINDINGS.load(workspace)
    if not table["enabled"]:
        return {}
    active = {pose: action for pose, action in table["bindings"].items() if action != "none"}
    if cfg["arming"]["required"] and cfg["arming"]["gesture"] in active:
        raise GesturePolicyError(f"gestures.yaml: '{cfg['arming']['gesture']}' is the arming pose in gesture.yaml "
                                 "and cannot also be bound to an action")
    return active
