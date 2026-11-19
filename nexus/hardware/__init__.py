"""Task 24 - hardware control (opt-in, ``hardware.yaml``).

An exact allowlist of devices and literal commands over serial, MQTT, GPIO or
a mock transport; dry-run by default.  See ``docs/hardware.md``.
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
        from .transports import missing

        cfg = policy.load(workspace)
        devices = cfg["devices"]
        info.update(
            dry_run=cfg["dry_run"], devices=len(devices), safe_on_stop=cfg["safe_on_stop"],
            commands={kind: sum(1 for device in devices.values() for body in device["commands"].values()
                                if body["class"] == kind) for kind in policy.CLASSES},
            without_safe_command=sorted(name for name, device in devices.items() if not device["safe"] and any(
                body["class"] != "read" for body in device["commands"].values())),
            missing=missing({device["transport"] for device in devices.values()}),
        )
    except (ValueError, OSError) as exc:
        info["error"] = str(exc)[:240]
    return info
