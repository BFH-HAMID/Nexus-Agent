"""Metadata-only capture indicators and a durable, fail-closed hard-off latch."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

from .. import audit
from ..jarvis.sources import bounded
from ..studio_crypto import atomic_write
from .policy import VisionError

_LOCK = threading.RLock()
_CANCEL: dict[str, set[threading.Event]] = {}


def directory(workspace, *, create=False):
    from ..config import nexus_home
    home = nexus_home().resolve()
    root = home / 'vision' / hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()[:32]
    if root.resolve() != root:
        raise VisionError('Unsafe vision state directory')
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _read(path, default):
    if not path.exists() and not path.is_symlink():
        return default
    try:
        value = json.loads(bounded(path, 4096))
    except (ValueError, OSError):
        raise VisionError('Vision control/indicator state unavailable; capture refused') from None
    if not isinstance(value, dict):
        raise VisionError('Invalid vision control/indicator state')
    return value


def _write(path, value):
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise VisionError('Unsafe vision state file')
    atomic_write(path, json.dumps(value, allow_nan=False).encode())


def control(workspace):
    row = _read(directory(workspace) / 'control.json', {'stopped': False, 'epoch': ''})
    if type(row.get('stopped')) is not bool or not isinstance(row.get('epoch'), str):
        raise VisionError('Invalid vision hard-off latch')
    return row


def check_running(workspace, epoch=None):
    from ..computer_control import EmergencyStop
    if EmergencyStop().stopped():
        raise VisionError('Emergency stop latched; vision is stopped')
    row = control(workspace)
    if row['stopped'] or (epoch is not None and row['epoch'] != epoch):
        raise VisionError('Vision hard off latched or operation revoked')


def hard_off(workspace):
    """Works even with corrupt YAML. Never enables devices or resets permissions."""
    root = directory(workspace, create=True)
    lock = root / 'control.lock'
    if lock.is_symlink():
        raise VisionError('Unsafe vision control lock')
    with FileLock(str(lock), timeout=2):
        _write(root / 'control.json', {'stopped': True, 'epoch': uuid.uuid4().hex})
    with _LOCK:
        for event in _CANCEL.get(str(root), ()):
            event.set()
    # Stopping must take effect even if a full audit prevents its receipt.
    try:
        audit.record(workspace, 'vision.off', who='operator', target='capture', result='latched')
    except (OSError, ValueError):
        return {'stopped': True, 'audit_available': False}
    return {'stopped': True, 'audit_available': True}


def resume(workspace, gate):
    from .policy import approve, load_policy
    cfg = load_policy(workspace)
    if not cfg['enabled']:
        raise VisionError('Enable vision.yaml before resetting the hard-off latch')
    prior = control(workspace)
    approve(workspace, gate, 'vision_resume', {'epoch': prior['epoch'], 'effect': 'reset latch only; no device starts'})
    root = directory(workspace, create=True)
    lock = root / 'control.lock'
    if lock.is_symlink():
        raise VisionError('Unsafe vision control lock')
    with FileLock(str(lock), timeout=2):
        if load_policy(workspace) != cfg or control(workspace) != prior:
            raise VisionError('Vision state changed during review')
        audit.record(workspace, 'vision.resume', who='operator', target='capture', result='reset')
        _write(root / 'control.json', {'stopped': False, 'epoch': uuid.uuid4().hex})
    return {'stopped': False, 'capture_started': False}


def status(workspace):
    from ..computer_control import EmergencyStop
    from .policy import load_policy
    try:
        cfg = load_policy(workspace)
        enabled = cfg['enabled']
    except (ValueError, OSError):
        enabled = None
    try:
        stopped = control(workspace)['stopped'] or EmergencyStop().stopped()
    except (ValueError, OSError):
        stopped = True
    result = {'enabled': enabled, 'stopped': stopped, 'observed_at': time.time(),
              'source': 'capture leases, not an inference from enabled policy'}
    for kind in ('screen', 'camera'):
        try:
            row = _read(directory(workspace) / f'{kind}.json', {})
            active = row.get('active', False)
            if type(active) is not bool or type(row.get('updated', 0)) not in (int, float):
                raise VisionError('Malformed capture indicator')
            fresh = 0 <= time.time() - row.get('updated', 0) <= 10
            result[kind] = {'active': bool(active) if (not active or fresh) else None,
                            'state': 'active' if active and fresh else 'unknown' if active else 'idle',
                            'last_capture': row.get('last_capture'), 'stop_requested': stopped}
        except (ValueError, OSError, TypeError):
            result[kind] = {'active': None, 'state': 'unknown', 'stop_requested': stopped}
    return result


class CaptureLease:
    """One capture owner per kind/workspace, cross-process; no images in state."""

    def __init__(self, workspace, kind, *, epoch=None):
        if kind not in ('screen', 'camera'):
            raise VisionError('Unsupported capture kind')
        self.root, self.kind = Path(workspace).resolve(), kind
        self.id = uuid.uuid4().hex
        self.expected_epoch = epoch
        self.cancel = threading.Event()
        self.last_capture = None
        self._entered = False
        self.uncertain = False

    def check(self):
        if self.cancel.is_set():
            raise VisionError('Vision capture cancelled')
        check_running(self.root, self.epoch)

    def heartbeat(self):
        self.check()
        _write(self.path, {'id': self.id, 'active': True, 'updated': time.time(), 'last_capture': self.last_capture})

    def __enter__(self):
        check_running(self.root, self.expected_epoch)
        root = directory(self.root, create=True)
        lock = root / f'{self.kind}.lock'
        if lock.is_symlink():
            raise VisionError('Unsafe capture lock')
        self.lock = FileLock(str(lock), timeout=0)
        self.lock.acquire()
        try:
            self.path = root / f'{self.kind}.json'
            self.epoch = control(self.root)['epoch']
            if self.expected_epoch is not None and self.expected_epoch != self.epoch:
                raise VisionError('Capture request revoked during approval')
            self.heartbeat()
            with _LOCK:
                _CANCEL.setdefault(str(root), set()).add(self.cancel)
            self._entered = True
            return self
        except BaseException:
            self.lock.release()
            raise

    @contextmanager
    def frame(self):
        self.heartbeat()
        ident = uuid.uuid4().hex
        # Audit before acquiring pixels: failure blocks IO, including failed captures.
        audit.record(self.root, 'vision.capture', who='operator', target=self.kind,
                     result='requested', detail={'capture_id': ident})
        self.check()
        try:
            yield ident
            self.check()
            self.last_capture = time.time()
            audit.record(self.root, 'vision.capture', who='operator', target=self.kind,
                         result='captured', detail={'capture_id': ident})
        except BaseException:
            audit.record(self.root, 'vision.capture', who='operator', target=self.kind,
                         result='failed_or_revoked', detail={'capture_id': ident})
            raise

    def __exit__(self, *unused):
        try:
            if self._entered:
                _write(self.path, {'id': self.id, 'active': self.uncertain, 'updated': 0 if self.uncertain else time.time(),
                                   'last_capture': self.last_capture})
        finally:
            with _LOCK:
                events = _CANCEL.get(str(directory(self.root)), set())
                events.discard(self.cancel)
                if not events:
                    _CANCEL.pop(str(directory(self.root)), None)
            self.lock.release()


def store_frame(workspace, payload, cfg):
    """Opt-in bounded archive; stop at capacity, never silently delete old frames."""
    if not cfg['camera']['store_frames']:
        return False
    from .policy import recheck
    recheck(workspace, cfg)
    root = directory(workspace, create=True) / 'frames'
    if root.resolve() != root:
        raise VisionError('Unsafe frame archive')
    root.mkdir(mode=0o700, exist_ok=True)
    rows = list(root.iterdir())
    if any(p.is_symlink() or not p.is_file() or p.stat().st_nlink != 1 for p in rows):
        raise VisionError('Unsafe frame archive entry')
    if (len(rows) >= cfg['camera']['max_storage_frames']
            or sum(p.stat().st_size for p in rows) + len(payload) > cfg['camera']['max_storage_bytes']):
        raise VisionError('Frame storage quota full; operator archival required')
    name = root / (uuid.uuid4().hex + '.jpg')
    with os.fdopen(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
        out.write(payload)
    return True
