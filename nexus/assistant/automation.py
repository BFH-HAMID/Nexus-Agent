"""Compatibility facades for the shared, YAML-scoped computer tool boundary.

Legacy environment flags/confirmation strings may narrow a request, never enable
an adapter or replace fresh ApprovalGate review. No parallel privileged executor.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from ..computer_actions import invoke, runtime, safe_local
from ..computer_workflows import Recorder
from ..jarvis.sources import bounded
from ..permissions import Permissions
from .common import AssistantError, atomic_json

MAX_BACKUP_FILES = 200
MAX_BACKUP_BYTES = 20_000_000
SKIP = {'.git', '.nexus', '.venv', 'venv', 'node_modules', '__pycache__', 'dist', 'build'}
SECRET_NAMES = {'.env', 'keys.json', 'credentials.json', '.netrc', 'id_rsa', 'id_ed25519'}


def _call(root, name, args, gate):
    try:
        return invoke(root, name, args, gate=gate)
    except (ValueError, OSError) as exc:
        raise AssistantError(str(exc)) from None


class DesktopController:
    """Preserved API; explicit app identity and permissions.yaml are now mandatory."""
    def __init__(self, *, backend='auto', review=None, recorder=None, workspace=None, gate=None):
        from ..tools import ApprovalDecision, ApprovalGate
        self.workspace = Path(workspace or '.').resolve()
        self.backend, self.review, self.recorder = backend, review, recorder
        callback = None
        if review is not None:
            def callback(spec, args, risk, preview):
                return ApprovalDecision(bool(review(args.get('action', spec.name), args)))
        self.gate = gate or ApprovalGate(callback=callback)
        if recorder is not None and recorder.root != self.workspace:
            raise AssistantError('Recorder and controller must use the same workspace')

    def execute(self, action, args):
        cfg = Permissions(self.workspace).load()['computer']['desktop']
        if self.backend not in ('auto', cfg['backend']):
            raise AssistantError('Backend must match the operator-owned desktop policy')
        payload = dict(args)
        if action == 'key':
            action = 'hotkey'
            if isinstance(payload.get('keys'), str):
                payload['keys'] = payload['keys'].casefold().split('+')
        result = _call(self.workspace, 'desktop_action', {**payload, 'action': action}, self.gate)
        return {**result, 'executed': action}


class WorkflowRecorder(Recorder):
    @property
    def active(self):
        return self.read()['active']

    def append(self, action, args):
        raise AssistantError('Only successful shared-registry actions are recorded; drafts cannot confer approval')

    def replay(self, controller, *, name=None, parameters=None):
        if controller.workspace != self.root:
            raise AssistantError('Workflow/controller workspace mismatch')
        name = name or self.read().get('name')
        return _call(self.root, 'workflow_run', {'name': name, 'parameters': parameters or {}}, controller.gate)


class BackupManager:
    def __init__(self, workspace, *, gate=None):
        self.workspace = Path(workspace).resolve()
        self.root = safe_local(self.workspace, '.nexus/computer/backups')
        self.gate = gate

    def request(self, *, source=None, destination=None):
        cfg = Permissions(self.workspace).load()['computer']['files']
        if not cfg['enabled'] or not cfg['roots']:
            raise AssistantError('Backups require computer.files enabled with operator-owned roots')
        ident = uuid.uuid4().hex[:16]
        return ident, {'action': 'backup', 'path': source or cfg['roots'][0],
                       'destination': destination or str(Path(cfg['roots'][-1]) / 'nexus-backups' / (ident + '.zip'))}

    def remember(self, ident, result):
        runtime(self.workspace, 'backups')
        atomic_json(self.root / (ident + '.json'), {'path': result['path'], 'sha256': result['sha256']})
        return {'id': ident, **result}

    def create(self, *, source=None, destination=None):
        ident, args = self.request(source=source, destination=destination)
        result = _call(self.workspace, 'file_action', args, self.gate)
        return self.remember(ident, result)

    def restore_request(self, ident, destination):
        if not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9]{16}', ident):
            raise AssistantError('Unknown backup id')
        row = json.loads(bounded(safe_local(self.workspace, self.root / (ident + '.json')), 4000))
        from ..computer_files import scoped
        path = scoped(Permissions(self.workspace), row['path'])
        cfg = Permissions(self.workspace).load()['computer']['files']
        if __import__('hashlib').sha256(bounded(path, cfg['max_bytes'])).hexdigest() != row['sha256']:
            raise AssistantError('Backup changed; inspect manually')
        return {'action': 'restore_backup', 'path': row['path'], 'destination': str(destination)}

    def restore(self, ident, destination, *, confirmation):
        if confirmation != f'RESTORE BACKUP {ident}':
            raise AssistantError(f'Type RESTORE BACKUP {ident} exactly; fresh shared-gate approval is also required')
        return _call(self.workspace, 'file_action', self.restore_request(ident, destination), self.gate)


class HomeAssistant:
    def __init__(self, vault, *, base_url='', entities=None, workspace=None, gate=None):
        self.workspace, self.gate = Path(workspace or '.').resolve(), gate
        self.vault = vault  # compatibility: wire secrets still come from the native vault/environment
        cfg = Permissions(self.workspace).load()['computer']['smart_home']['home_assistant']
        if base_url and base_url.rstrip('/') != cfg['url']:
            raise AssistantError('Home Assistant endpoint must match the operator YAML allowlist')
        allowed = {d['entity'] for d in cfg['devices'].values()}
        self.entities = set(entities) if entities is not None else allowed
        if self.entities - allowed:
            raise AssistantError('Entities cannot widen the operator YAML allowlist')
        self.base_url = cfg['url']

    def _device(self, entity):
        cfg = Permissions(self.workspace).load()['computer']['smart_home']['home_assistant']
        if cfg['url'] != self.base_url or entity not in self.entities:
            raise AssistantError('Home Assistant scope changed/outside allowlist')
        matches = [name for name, device in cfg['devices'].items() if device['entity'] == entity]
        if len(matches) != 1:
            raise AssistantError('Exactly one configured Home Assistant device required')
        return matches[0]

    def states(self):
        return [_call(self.workspace, 'home_action', {'adapter': 'home_assistant', 'device': self._device(entity), 'action': 'state'}, self.gate)
                for entity in sorted(self.entities) if not entity.startswith('notify.')]

    def call(self, actions, challenge, confirmation, payload):
        entity = payload.get('entity_id', '')
        if set(payload) != {'domain', 'service', 'entity_id'} or entity.split('.')[0] != payload.get('domain'):
            raise AssistantError('Exact Home Assistant service/entity request required')
        request = {'adapter': 'home_assistant', 'device': self._device(entity), 'action': payload['service']}
        return actions.execute(challenge, confirmation, payload, lambda: _call(self.workspace, 'home_action', request, self.gate))


class MqttHome:
    """Preserved publish API. Prefix authority is replaced by exact operator mappings."""
    def __init__(self, *, host='', username='', password='', topic_prefixes=None, port=8883, workspace=None, gate=None):
        self.workspace, self.gate = Path(workspace or '.').resolve(), gate
        cfg = Permissions(self.workspace).load()['computer']['smart_home']['mqtt']
        if host and host != cfg['host'] or port != cfg['port']:
            raise AssistantError('MQTT endpoint must match the operator YAML allowlist')
        topics = {d['topic'] for d in cfg['devices'].values()}
        self.topics = set(topic_prefixes) if topic_prefixes is not None else topics
        if self.topics - topics:
            raise AssistantError('MQTT now requires exact configured topics, never prefixes')
        self.host, self.port = cfg['host'], cfg['port']
        # Never retain caller credentials; documented backend reads env/native keyring.

    def publish(self, actions, challenge, confirmation, payload):
        if set(payload) != {'topic', 'message'} or payload.get('topic') not in self.topics:
            raise AssistantError('MQTT payload outside exact allowlist')
        cfg = Permissions(self.workspace).load()['computer']['smart_home']['mqtt']
        if cfg['host'] != self.host or cfg['port'] != self.port:
            raise AssistantError('MQTT endpoint changed; reconfigure/review')
        matches = [(name, action) for name, device in cfg['devices'].items() if device['topic'] == payload['topic']
                   for action, message in device['commands'].items() if message == payload['message']]
        if len(matches) != 1:
            raise AssistantError('MQTT needs an exact operator-fixed device/action payload')
        device, action = matches[0]
        return actions.execute(challenge, confirmation, payload, lambda: _call(self.workspace, 'home_action',
            {'adapter': 'mqtt', 'device': device, 'action': action}, self.gate))
