"""User-scoped emergency latch. No arbitrary PID killing or keyboard recording."""
from __future__ import annotations

import json
import os
import time
import uuid

from filelock import FileLock


class EmergencyStop:
    def __init__(self):
        from .config import nexus_home
        home = nexus_home().resolve()
        self.root = home / 'computer-control'
        self.path = self.root / 'emergency.json'

    def stopped(self):
        if self.root.resolve() != self.root or self.path.is_symlink():
            return True
        if not self.path.exists():
            return False
        try:
            if self.path.stat().st_size > 1000:
                return True
            data = json.loads(self.path.read_text())
            return data.get('stopped') is not False
        except (ValueError, OSError, AttributeError):
            return True

    def set(self, stopped):
        if self.root.resolve() != self.root:
            raise ValueError('Unsafe emergency control directory')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.root / 'control.lock'
        if lock.is_symlink() or self.path.is_symlink():
            raise ValueError('Unsafe emergency control file')
        with FileLock(str(lock), timeout=5):
            temporary = self.root / (uuid.uuid4().hex + '.tmp')
            try:
                with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as out:
                    json.dump({'stopped': bool(stopped), 'changed': time.time()}, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        return {'stopped': self.stopped()}


def hotkey():
    if os.name != 'nt':
        raise ValueError('Global Ctrl+Alt+Pause requires Windows; use agent kill or terminal Ctrl+C here')
    import ctypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    listen(user32)


def listen(user32):
    import ctypes
    from ctypes import wintypes
    # MOD_ALT | MOD_CONTROL | MOD_NOREPEAT, VK_PAUSE. No key logging/hook injection.
    if not user32.RegisterHotKey(None, 1, 0x4003, 0x13):
        raise OSError('Global hotkey unavailable or already registered')
    print('Ctrl+Alt+Pause registered. Keep this listener running; Ctrl+C exits.')
    try:
        message = wintypes.MSG()
        while True:
            if user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                if message.message == 0x0012:  # WM_QUIT
                    break
                if message.message == 0x0312:
                    EmergencyStop().set(True)
                    print('Nexus emergency stop latched; agent resume-tools is required.')
            time.sleep(.05)  # Main-thread signal handling, no busy loop/blocking GetMessage.
    finally:
        user32.UnregisterHotKey(None, 1)
