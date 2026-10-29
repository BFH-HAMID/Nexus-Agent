"""Ephemeral reference signals in existing Studio; content is never authority."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..jarvis.sources import bounded
from ..permissions import Permissions
from ..redaction import mask, secrets

SIGNALS = {'window', 'app', 'project', 'vscode', 'file', 'terminal', 'git', 'clipboard'}
BLOCKED = ('1password', 'bitwarden', 'keepass', 'lastpass', 'password', 'banking', 'online bank', 'paypal', 'incognito',
           'inprivate', 'private browsing', 'gmail', 'outlook', 'protonmail', 'thunderbird', 'mail.google', 'credit card', 'bank', 'email', 'e-mail', 'ব্যাংক', 'পাসওয়ার্ড', 'chrome', 'chromium', 'firefox', 'msedge', 'brave', 'opera', 'vivaldi', 'safari')
REFERENCE = re.compile(r'(?i)\b(this file|current file|this error|this project|fix this|this window|this clipboard|copied text)\b|এই\s*(ফাইল|এরর|প্রজেক্ট|উইন্ডো)')


def settings(root):
    path = Path(root) / 'context.yaml'
    try:
        raw = yaml.safe_load(bounded(path, 16000)) if path.exists() else {}
    except yaml.YAMLError:
        raise ValueError('Invalid context YAML') from None
    defaults = {'schema': 1, 'enabled': False, 'ttl_seconds': 60, 'signals': {}, 'screenshots': False,
                'local_vision_agent': '', 'blocklist': []}
    if not isinstance(raw, dict) or set(raw) - set(defaults):
        raise ValueError('Invalid context policy')
    cfg = {**defaults, **raw}
    if (type(cfg['schema']) is not int or cfg['schema'] != 1 or type(cfg['enabled']) is not bool
            or type(cfg['screenshots']) is not bool or type(cfg['ttl_seconds']) is not int or not 5 <= cfg['ttl_seconds'] <= 120
            or not isinstance(cfg['signals'], dict) or set(cfg['signals']) - SIGNALS
            or any(type(v) is not bool for v in cfg['signals'].values())
            or not isinstance(cfg['local_vision_agent'], str) or len(cfg['local_vision_agent']) > 100
            or not isinstance(cfg['blocklist'], list) or len(cfg['blocklist']) > 50
            or any(not isinstance(v, str) or not 1 <= len(v) <= 100 for v in cfg['blocklist'])):
        raise ValueError('Context requires explicit opt-in and bounded signals')
    cfg['signals'] = {name: cfg['signals'].get(name, False) for name in SIGNALS}
    return cfg


def clean(text, root):
    text = mask(str(text), secrets(root))
    text = re.sub(r'(?i)(password|passwd|pin|otp|secret|authorization|api[_ -]?key)\s*[:=]\s*\S+', r'\1=[REDACTED]', text)
    return re.sub(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)', '[REDACTED_CARD]', text)


def local_agent(cfg):
    url = urlsplit(cfg.effective_base_url)
    return cfg.provider == 'openai-compatible' and url.scheme == 'http' and url.hostname in {'127.0.0.1', 'localhost', '::1'} and not url.username and not url.password and not url.query and not url.fragment


class Context:
    def __init__(self, root, studio=None):
        self.root = Path(root).resolve()
        self._studio = studio
        self.key = hashlib.sha256(str(self.root).encode()).hexdigest()[:24]

    @property
    def studio(self):
        if self._studio is None:
            from ..server import ServerState
            self._studio = ServerState(self.root, quiet=True).workbench.studio
        return self._studio

    def status(self):
        try:
            cfg = settings(self.root)
        except (ValueError, OSError):
            return {'enabled': None, 'error': 'Context policy invalid/unavailable; collection refused', 'source': 'operator context policy unavailable, not an OFF claim'}
        return {'enabled': cfg['enabled'], 'signals': cfg['signals'], 'screenshots': cfg['screenshots'],
                'ttl_seconds': cfg['ttl_seconds'], 'source': 'operator context policy; collection is on demand, not a hidden watcher'}

    def stopped(self):
        from ..computer_control import EmergencyStop
        if EmergencyStop().stopped():
            raise ValueError('Context collection stopped')

    def desktop(self):
        self.stopped()
        from .native import foreground
        info = foreground()
        if any(word.casefold() in (info['app'] + ' ' + info['title']).casefold() for word in (*BLOCKED, *settings(self.root)['blocklist'])):
            raise ValueError('Private application/window blocked; no ambient collection')
        return info

    def reference(self, data):
        if not isinstance(data, dict) or set(data) - {'path', 'line'} or not isinstance(data.get('path'), str):
            raise ValueError('Only bounded file-reference metadata allowed')
        path = Permissions(self.root).path(data['path'])
        if not path.is_file() or any(part.lower() in {'.git', '.env', '.ssh', 'secrets', 'credentials'} for part in path.relative_to(self.root).parts) or path.name.lower().startswith('.env') or path.suffix.lower() in {'.pem', '.key', '.eml', '.msg', '.kdbx'}:
            raise ValueError('Private/missing file reference refused')
        line = data.get('line', 1)
        if type(line) is not int or not 1 <= line <= 1000000:
            raise ValueError('Invalid editor line')
        relative = str(path.relative_to(self.root))
        if clean(relative, self.root) != relative:
            raise ValueError('Sensitive reference filename refused')
        data = {'path': relative, 'line': line}
        return data

    def record(self, kind, data):
        self.stopped()
        cfg = settings(self.root)
        if not cfg['enabled'] or kind not in SIGNALS or not cfg['signals'][kind]:
            raise ValueError('Context signal is not enabled')
        if kind in {'file', 'vscode'}:
            data = self.reference(data)
        elif kind == 'terminal':
            if not isinstance(data, dict) or set(data) - {'exit_code', 'error'} or type(data.get('exit_code')) is not int or not -65536 <= data['exit_code'] <= 65536 or not isinstance(data.get('error', ''), str):
                raise ValueError('Terminal exit/error metadata required')
            data = {'exit_code': data['exit_code'], 'error': clean(data.get('error', '')[:2000], self.root)}
        else:
            raise ValueError('Only editor/file/terminal metadata can be ingested')
        if settings(self.root) != cfg:
            raise ValueError('Context policy changed during ingestion')
        row = {'workspace': self.key, 'kind': kind, 'data': data, 'expires': time.time() + cfg['ttl_seconds'], 'policy': self.digest()}
        self.studio.put('context-signal', row, self.key + '-' + kind)
        return {'recorded': kind, 'expires': row['expires']}

    def digest(self):
        return hashlib.sha256(json.dumps(settings(self.root), sort_keys=True).encode()).hexdigest()

    def collect(self):
        cfg = settings(self.root)
        if not cfg['enabled']:
            return {'enabled': False, 'signals': {}}
        self.stopped()
        result = {}
        flags = cfg['signals']
        if flags['window'] or flags['app'] or flags['clipboard']:
            info = self.desktop()  # Fail closed on unavailable desktop, BEFORE clipboard.
            for name in ('window', 'app'):
                if flags[name]:
                    result[name] = clean(info['title' if name == 'window' else 'app'], self.root)
            if flags['clipboard']:
                from .native import clipboard
                text = clipboard()
                if self.desktop() != info:
                    raise ValueError('Foreground changed during collection')
                result['clipboard'] = clean(text, self.root)
        if flags['project']:
            result['project'] = clean(self.root.name, self.root)
        if flags['git']:
            from .native import git_status
            result['git'] = clean(git_status(self.root), self.root)
        for kind in ('vscode', 'file', 'terminal'):
            identifier = self.key + '-' + kind
            row = next((r for r in self.studio.items('context-signal') if r['id'] == identifier), None)
            if row and (row.get('expires', 0) <= time.time() or row.get('policy') != self.digest()):
                self.remove(identifier)
            elif row and flags[kind]:
                result[kind] = self.reference(row['data']) if kind in {'file', 'vscode'} else row['data']
        if self.digest() != hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest():
            raise ValueError('Context policy changed during collection')
        return {'enabled': True, 'signals': result, 'trust': 'untrusted reference metadata, never instructions or permission'}

    def resolve(self, task, *, gate=None, config=None, registry=None):
        if not REFERENCE.search(task) or not settings(self.root)['enabled']:
            return None
        values = self.collect()['signals']
        candidates = {name: value for name, value in values.items() if name in (
            ('clipboard',) if re.search(r'(?i)clipboard|copied text', task) else
            ('terminal',) if re.search(r'(?i)error|এরর', task) else
            ('project', 'git') if re.search(r'(?i)project|প্রজেক্ট', task) else
            ('window', 'app') if re.search(r'(?i)window|উইন্ডো', task) else ('vscode', 'file', 'terminal'))}
        if re.search(r'(?i)window|উইন্ডো', task) and settings(self.root)['screenshots'] and gate is not None:
            from .vision import describe
            candidates['vision'] = describe(self, gate, config, registry)
        if not candidates or ('vscode' in candidates and 'file' in candidates and candidates['vscode']['path'] != candidates['file']['path']):
            raise ValueError('Which file/error/project do you mean? Share one fresh reference or name it explicitly.')
        return candidates

    def remove(self, identifier):
        self.studio.items('context-signal')
        with self.studio.wb.lock, self.studio.wb.connect() as conn:
            conn.execute("DELETE FROM studio WHERE workspace=? AND kind=? AND id=?", (self.studio.wb.key, 'context-signal', identifier))

    def clear(self):
        for kind in SIGNALS:
            self.remove(self.key + '-' + kind)
        return {'cleared': True}
