"""Coarse, content-free presence signals.

``idle_seconds``   seconds since the last keyboard/mouse input (a number only)
``neighbor_macs``  hardware addresses in this machine's own neighbour table (passive: no ping, no scan)
``device_hash``    salted hash of an address; only hashes are configured or compared

Every probe returns ``None`` when the platform offers no answer, so an
unavailable signal is reported as unavailable instead of being read as "away".
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .. import policyfile

AREA = "presence"
_MAC = re.compile(r"(?<![0-9a-f:-])([0-9a-f]{1,2}(?:[:-][0-9a-f]{1,2}){5})(?![0-9a-f:-])", re.I)
_IGNORED = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}


def _run(argv: list[str], timeout: float = 3.0) -> str | None:
    exe = shutil.which(argv[0])
    if not exe:
        return None
    try:
        done = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def idle_seconds() -> float | None:
    """Seconds since the last local input event, or ``None`` when the platform can not say."""
    try:
        if sys.platform == "win32":  # pragma: no cover - platform specific
            import ctypes

            class LastInput(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            info = LastInput()
            info.cbSize = ctypes.sizeof(info)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return None
            return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)
        if sys.platform == "darwin":  # pragma: no cover - platform specific
            text = _run(["ioreg", "-c", "IOHIDSystem"]) or ""
            match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', text)
            return int(match.group(1)) / 1e9 if match else None
        text = _run(["xprintidle"])
        return max(0.0, int(text.strip()) / 1000.0) if text and text.strip().isdigit() else None
    except (OSError, ValueError, AttributeError):
        return None


def normalize_mac(value: str) -> str:
    parts = re.split(r"[:-]", str(value).strip().lower())
    if len(parts) != 6 or any(not re.fullmatch(r"[0-9a-f]{1,2}", part) for part in parts):
        raise ValueError("Give a hardware address like AA:BB:CC:DD:EE:FF")
    return ":".join(part.zfill(2) for part in parts)


def parse_macs(text: str) -> set[str]:
    found = set()
    for raw in _MAC.findall(text or ""):
        try:
            mac = normalize_mac(raw)
        except ValueError:
            continue
        if mac not in _IGNORED:
            found.add(mac)
    return found


def neighbor_macs() -> set[str] | None:
    """Addresses the OS already knows about. Reads a table; sends nothing on the network."""
    table = Path("/proc/net/arp")
    try:
        if table.is_file():
            return parse_macs(table.read_text(encoding="utf-8", errors="replace")[:200_000])
    except OSError:
        pass
    text = _run(["arp", "-a"])
    return parse_macs(text[:200_000]) if text is not None else None


def _salt(workspace: Path | str) -> bytes:
    path = policyfile.state_dir(workspace, AREA) / "salt"
    try:
        data = policyfile.read_bounded(path, 128)
        if len(data) == 32:
            return data
    except (OSError, ValueError):
        pass
    data = os.urandom(32)
    policyfile.atomic_write(path, data)
    return data


def device_hash(workspace: Path | str, mac: str) -> str:
    """Per-workspace salted hash of a hardware address (useless on any other machine)."""
    return hmac.new(_salt(workspace), normalize_mac(mac).encode("ascii"), hashlib.sha256).hexdigest()[:32]
