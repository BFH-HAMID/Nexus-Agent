"""Bounded typed-file recovery in Nexus state, never Git reset/clean.

Not a filesystem sandbox. Pending/failed mutations require manual inspection;
only an applied record with the exact current after-image can be undone.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

LIMIT = 500_000
QUOTA = 20_000_000
UNSET = object()


class Snapshots:
    def __init__(self, workspace):
        from .config import nexus_home
        self.workspace = Path(workspace).resolve()
        home = nexus_home().resolve()
        self.root = home / 'snapshots' / hashlib.sha256(str(self.workspace).encode()).hexdigest()
        if self.root.resolve() != self.root or not self.root.is_relative_to(home):
            raise ValueError('Unsafe snapshot state path')

    @contextmanager
    def locked(self):
        if self.root.resolve() != self.root:
            raise ValueError('Unsafe snapshot state path')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.root / 'snapshots.lock'
        if lock.is_symlink():
            raise ValueError('Unsafe snapshot lock')
        with FileLock(str(lock), timeout=5):
            yield

    def target(self, value):
        from .permissions import Permissions
        path = Permissions(self.workspace).path(value)
        if Path(os.path.abspath(self.workspace / value)) != path:
            raise ValueError('Snapshot target was replaced by a symlink; inspect manually')
        if Permissions(self.workspace).operator_control(path):
            raise ValueError('Cannot restore operator permission policy')
        return path

    @staticmethod
    def read(path):
        if not path.exists():
            return None, None
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > LIMIT or stat.S_IMODE(info.st_mode) & ~0o777:
            raise ValueError('Snapshots require a single-link regular text file, at most 500 KB')
        with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)), 'rb') as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev):
                raise ValueError('Snapshot source changed during read')
            data = handle.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError('Snapshot text budget exceeded or binary content')
        Snapshots.validate_content(path, data)
        return data, stat.S_IMODE(info.st_mode)

    @staticmethod
    def validate_content(path, data, *, workspace=None):
        from .github_safety import scan_text
        if Path(path).suffix.lower() in {'.docx', '.xlsx', '.pptx'}:
            from .office import validate
            validate(path, data, workspace=workspace)
        else:
            if b'\x00' in data:
                raise ValueError('Binary snapshots are refused')
            scan_text(data.decode('utf-8'), workspace=workspace)

    @staticmethod
    def identity(path, *, parent=False):
        if not path.exists():
            return None
        info = path.stat()
        return [info.st_dev, info.st_ino] if parent else [info.st_dev, info.st_ino, info.st_mtime_ns]

    def verify_current(self, record):
        path = self.target(record['path'])
        actual, mode = self.read(path)
        if (self.image(actual, mode) != record['after'] or self.identity(path) != record['identity']
                or self.identity(path.parent, parent=True) != record['parent_identity']):
            raise ValueError('File or parent changed since agent action; refusing later user edits')
        return path

    @staticmethod
    def image(data, mode):
        return None if data is None else {'sha256': hashlib.sha256(data).hexdigest(), 'mode': mode}

    def save(self, record):
        path = self.root / (record['id'] + '.json')
        if path.is_symlink():
            raise ValueError('Unsafe snapshot record')
        payload = json.dumps(record, sort_keys=True).encode()
        files = list(self.root.glob('*.json'))
        if len(files) >= 1000 and not path.exists():
            raise ValueError('Snapshot record quota full; archive manually before continuing')
        if any(p.is_symlink() or not p.is_file() for p in files):
            raise ValueError('Unsafe snapshot archive')
        total = sum(p.stat().st_size for p in files) - (path.stat().st_size if path.exists() else 0)
        if total + len(payload) > QUOTA:
            raise ValueError('Snapshot storage quota full; archive manually before continuing')
        temporary = self.root / (uuid.uuid4().hex + '.tmp')
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def change(self, path, operation, planned, *, expected=UNSET):
        from .github_safety import scan_text
        path = self.target(str(path))
        if planned is not None:
            if len(planned) > LIMIT:
                raise ValueError('Planned file exceeds text snapshot budget')
            self.validate_content(path, planned, workspace=self.workspace)
            if path.suffix.lower() not in {'.docx', '.xlsx', '.pptx'}:
                scan_text(planned.decode('utf-8'), workspace=self.workspace)
        with self.locked():
            before, mode = self.read(path)
            if expected is not UNSET and before != expected:
                raise ValueError('File changed during edit preparation; refusing overwrite')
            if before is not None:
                self.validate_content(path, before, workspace=self.workspace)
            previous = None
            files = list(self.root.glob('*.json'))
            if len(files) >= 1000:
                raise ValueError('Snapshot record quota full; archive manually')
            history = [self.load(p.stem) for p in files]
            candidates = sorted((r for r in history if r['path'] == path.relative_to(self.workspace).as_posix()
                                 and r['status'] == 'applied'), key=lambda r: r['created'], reverse=True)
            if candidates:
                try:
                    self.verify_current(candidates[0])
                    previous = candidates[0]['id']
                except ValueError:
                    pass  # User edits break lineage; never rebase over them.
            record = {'schema': 1, 'id': uuid.uuid4().hex, 'workspace': str(self.workspace),
                      'path': path.relative_to(self.workspace).as_posix(), 'operation': operation,
                      'created': time.time(), 'previous': previous, 'status': 'pending', 'before': self.image(before, mode),
                      'body': None if before is None else base64.b64encode(before).decode(), 'after': None,
                      'identity': None, 'parent_identity': None}
            self.save(record)  # Persist pre-image before mutation, not after it.
            try:
                yield record['id']
                actual, actual_mode = self.read(path)
                if actual != planned:
                    raise ValueError('File changed unexpectedly; recovery requires manual inspection')
                record.update(status='applied', after=self.image(actual, actual_mode), identity=self.identity(path),
                              parent_identity=self.identity(path.parent, parent=True))
                self.save(record)
            except BaseException:
                # Never call a partial mutation safely undoable. Keep its pre-image.
                record['status'] = 'uncertain'
                self.save(record)
                raise

    def load(self, identifier):
        if not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise ValueError('Invalid snapshot identifier')
        path = self.root / (identifier + '.json')
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 800_000:
            raise ValueError('Invalid snapshot record file')
        record = json.loads(path.read_text())
        required = {'schema', 'id', 'workspace', 'path', 'operation', 'created', 'status', 'before', 'body',
                    'after', 'identity', 'parent_identity', 'previous'}
        if (not isinstance(record, dict) or not required.issubset(record) or set(record) - required - {'restored'}):
            raise ValueError('Invalid snapshot schema')
        if (record.get('schema') != 1 or record.get('id') != identifier
                or record.get('workspace') != str(self.workspace)):
            raise ValueError('Snapshot identity mismatch')
        if (record.get('status') not in {'pending', 'applied', 'uncertain', 'restoring', 'undone'}
                or record.get('operation') not in {'write_file', 'edit_file', 'delete_file', 'office_file'}
                or not isinstance(record.get('path'), str)
                or type(record.get('created')) not in (int, float) or not math.isfinite(record['created'])
                or (record['previous'] is not None and (not isinstance(record['previous'], str)
                    or not re.fullmatch(r'[a-f0-9]{32}', record['previous'])))):
            raise ValueError('Invalid snapshot metadata')
        for key in ('before', 'after'):
            image = record.get(key)
            if image is not None and (not isinstance(image, dict) or set(image) != {'sha256', 'mode'}
                    or not isinstance(image['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', image['sha256'])
                    or type(image['mode']) is not int or not 0 <= image['mode'] <= 0o777):
                raise ValueError('Invalid snapshot image')
        if (record.get('before') is None) != (record.get('body') is None):
            raise ValueError('Invalid snapshot body')
        if record.get('body') is not None and not isinstance(record['body'], str):
            raise ValueError('Invalid snapshot body')
        for key, length in (('identity', 3), ('parent_identity', 2)):
            value = record[key]
            if value is not None and (not isinstance(value, list) or len(value) != length
                    or any(type(n) is not int or n < 0 for n in value)):
                raise ValueError('Invalid snapshot filesystem identity')
        self.target(record['path'])
        return record

    def listing(self):
        if not self.root.exists():
            return []
        with self.locked():
            files = list(self.root.glob('*.json'))
            if len(files) > 1000:
                raise ValueError('Snapshot listing budget exceeded')
            rows = [self.summary(self.load(p.stem)) for p in files]
        return sorted(rows, key=lambda row: row['created'], reverse=True)

    @staticmethod
    def summary(record):
        return {k: record[k] for k in ('id', 'path', 'operation', 'status', 'created', 'before', 'after', 'identity', 'parent_identity', 'previous')}

    def preview(self, identifier):
        with self.locked():
            record = self.load(identifier)
            if record['status'] != 'applied':
                raise ValueError('Only applied, not undone/pending/uncertain snapshots can be restored')
            self.verify_current(record)
            return {**self.summary(record), 'confirmation': f"UNDO {identifier} {record['path']}"}

    def undo(self, preview, gate):
        from .tools import ToolFailure
        from .types import Risk, ToolSpec
        spec = ToolSpec(name='undo_file', description='Restore one reviewed typed-file snapshot',
                        input_schema={'type': 'object', 'properties': {'path': {'type': 'string'}}}, risk=Risk.DANGEROUS)
        decision = gate.check(spec, {'path': preview['path'], 'snapshot': preview['id'], 'after': preview['after']})
        if not decision.approved:
            raise ToolFailure('Undo denied by approval/permissions')
        with self.locked():
            record = self.load(preview['id'])
            if self.summary(record) != {k: preview[k] for k in self.summary(record)} or record['status'] != 'applied':
                raise ValueError('Snapshot changed during review; preview again')
            path = self.verify_current(record)
            previous = self.load(record['previous']) if record.get('previous') else None
            if previous and (previous['path'] != record['path'] or previous['status'] != 'applied'
                             or previous['after'] != record['before']):
                raise ValueError('Snapshot undo lineage changed; inspect manually')
            data = None if record['body'] is None else base64.b64decode(record['body'], validate=True)
            if self.image(data, record['before']['mode'] if record['before'] else None) != record['before']:
                raise ValueError('Corrupt snapshot pre-image')
            if data is not None:
                from .github_safety import scan_text
                self.validate_content(path, data, workspace=self.workspace)
                if path.suffix.lower() not in {'.docx', '.xlsx', '.pptx'}:
                    scan_text(data.decode('utf-8'), workspace=self.workspace)
                path.parent.mkdir(parents=True, exist_ok=True)
            record['status'] = 'restoring'
            self.save(record)  # Interrupted restores cannot be replayed automatically.
            if data is None:
                path.unlink()
            else:
                temporary = path.parent / ('.nexus-restore-' + uuid.uuid4().hex)
                try:
                    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
                        out.write(data)
                        out.flush()
                        os.fsync(out.fileno())
                    temporary.chmod(record['before']['mode'])
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            if previous:
                previous.update(identity=self.identity(path), parent_identity=self.identity(path.parent, parent=True))
                self.save(previous)
            record.update(status='undone', restored=time.time())
            self.save(record)
            return self.summary(record)


@contextmanager
def file_snapshot(ctx, path, operation, planned, *, expected=UNSET):
    """Only local handlers use this: remote GitHub tools have their own revisions."""
    ctx.state.pop('last_snapshot', None)
    if ctx.gate.permissions is None:
        yield
        return
    with Snapshots(ctx.workspace).change(path, operation, planned, expected=expected) as identifier:
        ctx.state['last_snapshot'] = identifier
        yield
