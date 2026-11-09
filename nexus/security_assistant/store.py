"""Private, bounded incident journal and immutable normalized evidence copies.

No automatic deletion/rotation. File modes and hashes detect accidents/tampering;
this is not WORM storage against the host owner. Original inputs are never written.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

MAX_STATE = 8_000_000
MAX_EVIDENCE = 64_000
SEVERITIES = {'info': 0, 'low': 1, 'medium': 2, 'high': 3, 'critical': 4}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encoded(value)).hexdigest()


def clean(root, value):
    from ..redaction import mask, secrets
    from ..studio import redact
    known = secrets(root)
    def visit(node):
        # Mask BEFORE JSON escaping, including quotes, backslashes and newlines
        # in known credentials. Mask map keys as well as leaf values.
        if isinstance(node, str):
            return mask(node, known)
        if isinstance(node, dict):
            return {visit(str(key)): visit(item) for key, item in node.items()}
        if isinstance(node, (list, tuple)):
            return [visit(item) for item in node]
        return node
    return visit(redact(value))


def path_safe(path):
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError('Security storage/input paths must not traverse symlinks')


def read_private(path, limit=MAX_STATE, *, readonly=False):
    path_safe(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit
                or os.name == 'posix' and (info.st_mode & 0o077 or readonly and info.st_mode & 0o222)):
            raise ValueError('Security state requires bounded private single-link files; evidence must be read-only')
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    if len(data) > limit or (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Security state changed or exceeded its budget')
    return data


def initial():
    return {'schema': 1, 'sources': {}, 'incidents': {}, 'timeline': [], 'last_tick': None, 'coverage': []}


class Store:
    def __init__(self, workspace):
        from ..config import nexus_home
        self.workspace = Path(workspace).resolve()
        self.root = nexus_home().absolute() / 'defensive-security' / sha(str(self.workspace).encode())
        self.path = self.root / 'state.json'
        self.evidence = self.root / 'evidence'

    def read(self):
        path_safe(self.path)
        if not self.path.exists():
            return initial()
        try:
            data = json.loads(read_private(self.path))
        except (ValueError, RecursionError):
            raise ValueError('Invalid security journal; operator inspection required') from None
        if (not isinstance(data, dict) or set(data) != set(initial()) or data['schema'] != 1
                or not isinstance(data['timeline'], list) or len(data['timeline']) > 5000
                or not isinstance(data['incidents'], dict) or len(data['incidents']) > 5000
                or not isinstance(data['sources'], dict) or len(data['sources']) > 64
                or not isinstance(data['coverage'], list) or len(data['coverage']) > 64):
            raise ValueError('Invalid security journal schema/bounds')
        previous = ''
        for index, row in enumerate(data['timeline']):
            if (not isinstance(row, dict) or set(row) != {'seq', 'sha256', 'previous', 'incident', 'at'}
                    or row['seq'] != index + 1 or row['previous'] != previous
                    or not isinstance(row['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', row['sha256'])):
                raise ValueError('Invalid security timeline chain')
            previous = row['sha256']
        return data

    @contextmanager
    def locked(self):
        path_safe(self.root)
        for directory in (self.root.parent, self.root, self.evidence):
            path_safe(directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == 'posix' and directory.stat().st_mode & 0o077:
                raise ValueError('Private security storage directories required')
        lock = self.root / 'state.lock'
        path_safe(lock)
        if lock.exists():
            read_private(lock, 4096)
        with FileLock(str(lock), timeout=5, mode=0o600):
            yield

    def write(self, state):
        data = encoded(state)
        if len(data) > MAX_STATE:
            raise ValueError('Security state budget full; archive manually, no evidence deleted')
        path_safe(self.path)
        if self.path.exists():
            read_private(self.path)
        fd, temporary = tempfile.mkstemp(prefix='.state-', dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _capacity(self, cfg, size, *, slots=1):
        total, count = 0, 0
        with os.scandir(self.evidence) as files:
            for index, item in enumerate(files):
                if index >= cfg['max_records'] or not re.fullmatch(r'[a-f0-9]{64}\.json', item.name):
                    raise ValueError('Evidence capacity full or unexpected artifact; archive manually')
                info = item.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('Unsafe security evidence artifact')
                total += info.st_size
                count += 1
        if count + slots > cfg['max_records']:
            raise ValueError('Evidence file quota full (including orphan artifacts); archive manually')
        if total + size > cfg['max_evidence_bytes']:
            raise ValueError('Evidence byte quota full; archive manually, no automatic deletion')

    def artifact(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{64}', identifier):
            raise ValueError('Invalid evidence SHA-256')
        data = read_private(self.evidence / (identifier + '.json'), MAX_EVIDENCE, readonly=True)
        if sha(data) != identifier:
            raise ValueError('Evidence hash mismatch; operator inspection required')
        return json.loads(data)

    def append(self, state, cfg, finding, now):
        """Caller holds lock. Journal first; never use these records as approval grants."""
        finding = clean(self.workspace, finding)
        if finding['severity'] not in SEVERITIES:
            raise ValueError('Unknown security severity')
        key = sha([finding['kind'], finding['subject']])[:24]
        signature = sha(finding)
        old = state['incidents'].get(key)
        if old and old['signature'] == signature:
            return None  # Unchanged CVE/certificate/baseline is not a fresh observation.
        if len(state['timeline']) >= cfg['max_records']:
            raise ValueError('Incident timeline full; archive manually, no evidence deleted')
        notify = (old is None or now - old['last_alert'] >= cfg['cooldown_seconds']
                  or SEVERITIES[finding['severity']] > SEVERITIES[old['severity']])
        event = {'schema': 1, 'seq': len(state['timeline']) + 1, 'at': now, 'incident': key,
                 'previous': state['timeline'][-1]['sha256'] if state['timeline'] else '',
                 'untrusted': True, 'evidence_type': 'normalized metadata; no original file bodies', **finding}
        data = encoded(event)
        if len(data) > MAX_EVIDENCE:
            raise ValueError('Normalized security evidence exceeded its budget')
        identifier = sha(data)
        path = self.evidence / (identifier + '.json')
        if path.exists() or path.is_symlink():
            if read_private(path, MAX_EVIDENCE, readonly=True) != data:
                raise ValueError('Conflicting immutable security evidence')
        else:
            self._capacity(cfg, len(data))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o400)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        state['timeline'].append({k: event[k] for k in ('seq', 'at', 'incident', 'previous')} | {'sha256': identifier})
        incident = {'id': key, 'kind': finding['kind'], 'subject': finding['subject'], 'severity': finding['severity'],
                    'summary': finding['summary'], 'first_seen': old['first_seen'] if old else now, 'last_seen': now,
                    'occurrences': old['occurrences'] + 1 if old else 1, 'signature': signature, 'evidence': identifier,
                    'last_alert': now if notify else old['last_alert'], 'suggestion': finding.get('suggestion', 'Review the evidence; no response was executed.')}
        state['incidents'][key] = incident
        return copy.deepcopy(incident) if notify and finding.get('alert', True) else None

    def export(self, state):
        events = []
        for pointer in state['timeline']:
            row = self.artifact(pointer['sha256'])
            if any(row.get(k) != pointer[k] for k in ('seq', 'at', 'incident', 'previous')):
                raise ValueError('Security evidence no longer matches its timeline')
            events.append({**row, 'sha256': pointer['sha256']})
        return clean(self.workspace, {'schema': 1, 'untrusted': True, 'response_executed': False,
                    'note': 'Observed normalized evidence, not proof of compromise. Hashes are not external notarization.',
                    'last_tick': state['last_tick'], 'coverage': state['coverage'],
                    'incidents': list(state['incidents'].values()), 'timeline': events})
