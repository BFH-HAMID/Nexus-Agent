"""Opt-in authenticated encryption for the complete dashboard SQLite file.

The key is held only in a supported OS credential store. Missing crypto, an
unavailable/unlocked key store, or corrupt ciphertext fails closed. No password
or key is accepted through the web API and no plaintext key-file fallback exists.
"""
from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
from contextlib import contextmanager

MAGIC = b'NEXUS-DB-AESGCM-1\n'


def os_key(path, *, create=False):
    try:
        import keyring
    except ImportError as exc:
        raise ValueError('Install nexus-agent[secure-state] and configure an OS key store') from exc
    backend = keyring.get_keyring()
    if type(backend).__module__ not in {"keyring.backends.SecretService", "keyring.backends.macOS", "keyring.backends.Windows"}:
        raise ValueError('A real OS Secret Service/macOS Keychain/Windows Credential Vault backend is required; no plaintext fallback')
    account = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    try:
        value = keyring.get_password('nexus-dashboard-aesgcm', account)
        if value is None and create:
            value = base64.b64encode(os.urandom(32)).decode()
            keyring.set_password('nexus-dashboard-aesgcm', account, value)
        if value is None:
            raise ValueError('OS key missing or locked; encrypted dashboard state cannot be opened')
        raw = base64.b64decode(value, validate=True)
        if len(raw) != 32:
            raise ValueError('Invalid OS-stored key')
        return raw
    except Exception as exc:
        raise ValueError('OS key store unavailable/locked; no plaintext fallback') from exc


def cipher(key):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise ValueError('Install nexus-agent[secure-state] for authenticated AES-GCM encryption') from exc
    return AESGCM(key)


def seal(data, key):
    nonce = os.urandom(12)
    return MAGIC + nonce + cipher(key).encrypt(nonce, data, MAGIC)


def unseal(data, key):
    if not data.startswith(MAGIC):
        raise ValueError('Not a Nexus encrypted state file')
    try:
        return cipher(key).decrypt(data[len(MAGIC):len(MAGIC)+12], data[len(MAGIC)+12:], MAGIC)
    except Exception as exc:
        raise ValueError('Encrypted state authentication failed; no plaintext fallback') from exc


def atomic_write(path, data):
    import tempfile
    fd, name = tempfile.mkstemp(prefix='.nexus-sealed-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def enabled(path):
    if not path.exists():
        return False
    with path.open('rb') as file:
        return file.read(len(MAGIC)) == MAGIC


@contextmanager
def connection(path, *, locked=False):
    # FileLock serializes independent processes as well as request threads.
    try:
        from filelock import FileLock
    except ImportError as exc:
        raise ValueError('Install nexus-agent[secure-state] to open encrypted state') from exc
    from contextlib import nullcontext
    with (nullcontext() if locked else FileLock(str(path) + '.lock', timeout=15)):
        key = os_key(path)
        conn = sqlite3.connect(':memory:')
        try:
            conn.deserialize(unseal(path.read_bytes(), key))
            yield conn
            conn.commit()
            atomic_write(path, seal(conn.serialize(), key))
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def enable(path):
    from filelock import FileLock
    with FileLock(str(path) + '.lock', timeout=15):
        if enabled(path):
            return
        key = os_key(path, create=True)
        if not hasattr(sqlite3.Connection, 'serialize'):
            raise ValueError('This Python/SQLite build lacks serialize; secure-state requires Python 3.11+ with SQLite serialization')
        # Must be enabled with this server idle and no other Nexus server using the
        # shared DB. The exclusive SQLite transaction prevents concurrent writers
        # while the file is sealed; subsequent connections detect the encrypted header.
        conn = sqlite3.connect(path)
        try:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            conn.execute('PRAGMA journal_mode=DELETE')
            conn.execute('BEGIN EXCLUSIVE')
            data = conn.serialize()
            encrypted = seal(data, key)
            conn.rollback()
        finally:
            conn.close()
        atomic_write(path, encrypted)


class DashboardSessions:
    """Keep new encrypted-dashboard transcripts out of the legacy plaintext CLI DB."""
    def __init__(self, studio):
        self.studio = studio

    def record(self, result, task, **kwargs):
        record = self.studio.put('transcript', {'task': task[:50000], 'text': result.text[:150000],
            'summary': result.summary[:2000], 'role': result.role, 'created': __import__('time').time()})
        return record['id']

    def recent(self, limit=10):
        return sorted(self.studio.items('transcript'), key=lambda r: r['created'], reverse=True)[:limit]

    def search(self, query, *, limit=5):
        return [r for r in self.recent(300) if query.lower() in str(r).lower()][:limit]


class DashboardMemory:
    """Read legacy memory, but keep newly approved secure-dashboard notes encrypted."""
    def __init__(self, studio, legacy):
        self.studio = studio
        self.legacy = legacy

    def prompt_sections(self):
        import time
        notes = [r['approved_text'] for r in self.studio.items('memory-draft') if r.get('status') == 'approve'
                 and r.get('scope') == 'encrypted-dashboard' and r.get('active_expires', 0) > time.time()]
        return self.legacy.prompt_sections() + ('\n# Reviewed dashboard memory\n' + '\n'.join(notes) if notes else '')

    def entries(self, section='memory'):
        return self.legacy.entries(section)
