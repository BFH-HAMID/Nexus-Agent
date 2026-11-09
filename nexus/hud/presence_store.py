"""Small private cross-process state. No audio, tool arguments or approval grants.

A file lock and atomic replace serialize CLI voice and HTTP writers. Bounds are
intentional: this is handoff state, not an unbounded transcript/archive.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

MAX_BYTES = 2_000_000


def directory(workspace):
    from ..config import nexus_home
    home = nexus_home().absolute()
    root = home / 'presence' / hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()
    if root.resolve() != root or home.is_symlink():
        raise ValueError('Private presence state must not use symlinks')
    return root


def safe_file(path, *, max_bytes=MAX_BYTES):
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError('Unsafe presence state path')
    if path.exists():
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes
                or os.name == 'posix' and info.st_mode & 0o077):
            raise ValueError('Presence state requires bounded private single-link files')


class Store:
    def __init__(self, root, name, default):
        if name not in {'conversation', 'devices', 'service'}:
            raise ValueError('Unknown presence store')
        self.root = directory(root)
        self.path = self.root / (name + '.json')
        self.lock_path = self.root / (name + '.lock')
        self.default = default

    def read(self):
        safe_file(self.path)
        if not self.path.exists():
            return copy.deepcopy(self.default())
        fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
                raise ValueError('Unsafe presence state')
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Presence state budget exceeded')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('Invalid presence state; operator review required')
        return value

    @contextmanager
    def locked(self):
        if self.root.resolve() != self.root:
            raise ValueError('Unsafe presence directory')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == 'posix' and self.root.stat().st_mode & 0o077:
            raise ValueError('Private presence directory required')
        safe_file(self.lock_path)
        with FileLock(str(self.lock_path), timeout=5, mode=0o600):
            yield

    def write(self, data):
        """Caller holds locked(); never accepts request-selected filenames."""
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
        if len(raw) > MAX_BYTES:
            raise ValueError('Presence state budget exceeded')
        safe_file(self.path)
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix='.presence-')
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def update(self, change):
        with self.locked():
            value = self.read()
            result = change(value)
            value['revision'] = int(value.get('revision', 0)) + 1
            self.write(value)
            return copy.deepcopy(result if result is not None else value)
