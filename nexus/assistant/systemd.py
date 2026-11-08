"""Reviewed Linux user-service installation; no implicit startup mutation."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .common import AssistantError, atomic_bytes

UNIT_NAME = "nexus-assistant.service"


def unit_path() -> Path:
    override = os.environ.get("NEXUS_ASSISTANT_UNIT")
    if override:
        return Path(override).expanduser().resolve()
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "systemd" / "user" / UNIT_NAME


def render_unit(workspace: Path | str) -> str:
    root = Path(workspace).resolve()
    nexus_home = Path(os.environ.get("NEXUS_HOME", Path.home() / ".config" / "nexus")).expanduser().resolve()
    # ExecStart is systemd's argv-like command, not a shell. Escape only the
    # whitespace/percent forms systemd specifies and refuse control characters.
    def quote(value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise AssistantError("Service paths cannot contain control characters")
        return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"""[Unit]
Description=Nexus Personal Assistant (authenticated local workspace)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory={quote(str(root))}
Environment=NEXUS_ASSISTANT_SCHEDULER=1
Environment=NEXUS_HOME={quote(str(nexus_home))}
ExecStart={quote(sys.executable)} -m nexus serve --workspace {quote(str(root))} --host 127.0.0.1 --port 8787
Restart=always
RestartSec=5
TimeoutStopSec=20
KillSignal=SIGTERM
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths={quote(str(root / '.nexus'))} {quote(str(nexus_home))}

[Install]
WantedBy=default.target
"""


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    executable = shutil.which("systemctl")
    if not executable:
        raise AssistantError("systemctl is unavailable; use the explicit CLI scheduler instead")
    result = subprocess.run([executable, "--user", *args], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=20, check=False)
    if result.returncode:
        raise AssistantError(f"systemctl --user {args[0]} refused the assistant service")
    return result


def install(workspace: Path | str) -> dict[str, Any]:
    path = unit_path()
    root = Path(workspace).resolve()
    nexus_home = Path(os.environ.get("NEXUS_HOME", Path.home() / ".config" / "nexus")).expanduser().resolve()
    (root / ".nexus").mkdir(parents=True, exist_ok=True, mode=0o700)
    nexus_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    previous = path.read_bytes() if path.is_file() and not path.is_symlink() else None
    atomic_bytes(path, render_unit(root).encode())
    try:
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", UNIT_NAME)
    except Exception:
        # Restore a prior working unit; never turn a failed edit into deletion.
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            atomic_bytes(path, previous)
        raise
    return {"installed": True, "unit": str(path), "restart": "always", "host": "127.0.0.1:8787",
            "note": "Remote phone access still requires a reviewed HTTPS proxy and NEXUS_SERVE_TOKEN."}


def remove() -> dict[str, Any]:
    path = unit_path()
    _systemctl("disable", "--now", UNIT_NAME)
    path.unlink(missing_ok=True)
    _systemctl("daemon-reload")
    return {"installed": False, "unit": str(path)}


def status() -> dict[str, Any]:
    path = unit_path()
    result = {"unit": str(path), "unit_file": path.is_file(), "systemctl": bool(shutil.which("systemctl"))}
    if result["systemctl"]:
        try:
            active = _systemctl("is-active", UNIT_NAME)
            result["active"] = active.stdout.decode(errors="replace").strip()
        except AssistantError:
            result["active"] = "inactive-or-unavailable"
    return result
