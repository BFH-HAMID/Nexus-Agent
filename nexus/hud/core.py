"""Real HUD state/control adapter; no model, context collector or scheduler."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from pathlib import Path

import yaml

from ..context.core import Context
from ..delegation.core import Delegations
from ..jarvis.approvals import Approvals
from ..jarvis.daemon import Daemon
from ..jarvis.inbox import Inbox
from ..jarvis.queue import Queue
from ..jarvis.runtime import Runtime
from ..redaction import mask, secrets
from ..sessions import SessionStore


def settings(root):
    path = Path(root) / 'hud.yaml'
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size > 16000)):
        raise ValueError('HUD policy must be bounded operator YAML')
    try:
        value = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
    except yaml.YAMLError:
        raise ValueError('Invalid HUD YAML') from None
    defaults = {'schema': 1, 'host': '127.0.0.1', 'port': 8790, 'refresh_seconds': 2, 'max_connections': 4}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError('Unknown HUD setting')
    cfg = {**defaults, **value}
    if type(cfg['schema']) is not int or cfg['schema'] != 1 or cfg['host'] not in ('127.0.0.1', '::1'):
        raise ValueError('HUD defaults must bind loopback')
    for key, low, high in [('port', 1024, 65535), ('refresh_seconds', 1, 30), ('max_connections', 1, 8)]:
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError('Invalid HUD limit')
    return cfg


class HUD:
    def __init__(self, studio, *, secret_values=()):
        self.secret_values = tuple(secret_values)
        self.studio = studio
        self.queue = Queue(studio)
        self.sessions = SessionStore()

    def clean(self, data):
        return json.loads(mask(json.dumps(data, allow_nan=False), list(self.secret_values) + secrets(self.queue.root)))

    def audit(self):
        from ..config import nexus_home
        path = nexus_home().resolve() / 'permissions' / hashlib.sha256(str(self.queue.root.resolve()).encode()).hexdigest() / 'audit.jsonl'
        if not path.exists():
            return []
        if path.is_symlink() or path.resolve() != path:
            raise ValueError('Unsafe audit path')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 5000000:
                raise ValueError('Audit size/identity refused')
            offset = max(0, info.st_size - 64000)
            handle.seek(offset)
            lines = handle.read(64000).splitlines()
            if offset:
                lines = lines[1:]
        return [json.loads(line) for line in lines[-20:]]

    def snapshot(self):
        import psutil
        with self.queue.locked():
            rows = self.queue._jobs()
        try:
            policy = self.queue.policy()
            policy_state = {'level': policy['level'], 'runtime_enabled': policy['runtime']['enabled'],
                            'local_model': policy['runtime']['local_model'], 'mode': policy['runtime']['mode'], 'connectivity': policy['runtime']['connectivity']}
        except (ValueError, OSError):
            policy_state = {'error': 'Invalid policy; dispatch unavailable; stop/kill remain available'}
        daemon = Daemon(self.studio).status()
        jobs = [{k: row.get(k) for k in ('id', 'status', 'created', 'attempts', 'active_role', 'parent', 'depth')}
                | {'task': row['task'][:240], 'delegated': bool(row.get('delegation_spec'))}
                for row in sorted(rows, key=lambda r: r['created'], reverse=True)[:50]]
        by_id = {r['id']: r for r in rows}
        actions = [{'id': r['id'], 'job_id': r['job_id'], 'tool': r['tool'], 'expires': r['expires']}
                   for r in Approvals(self.studio).items() if r['state'] == 'pending' and r['expires'] > time.time()
                   and r['job_id'] in by_id and by_id[r['job_id']]['status'] == 'running'
                   and r['lease'] == by_id[r['job_id']].get('lease')]
        workflows = []
        for row in self.studio.items('workflow-run')[-5:]:
            observed = row.get('nodes', {})
            nodes = [{'id': str(n['id'])[:80], 'role': str(n.get('role', ''))[:80],
                      'depends_on': [str(d)[:80] for d in n.get('depends_on', [])[:10]],
                      'status': str(observed.get(n['id'], {}).get('status', 'not_observed'))[:80]}
                     for n in row.get('definition', {}).get('nodes', [])[:40]]
            workflows.append({'id': row['id'], 'status': row['status'], 'nodes': nodes})
        try:
            audit = self.audit()
        except (ValueError, OSError):
            audit = {'error': 'Audit unavailable; no fabricated entries'}
        disk = psutil.disk_usage(str(self.queue.root))
        runtime = Runtime(self.studio)
        return self.clean({'observed_at': time.time(), 'source': 'existing Studio/Jarvis plus local psutil; not demo data',
                           'context': Context(self.queue.root, self.studio).status(),
                           'hardware': {'cpu_percent': psutil.cpu_percent(interval=None), 'memory_percent': psutil.virtual_memory().percent,
                                        'disk_free_bytes': disk.free},
                           'daemon': {k: daemon[k] for k in ('state', 'lock_held', 'execution_enabled', 'dispatch_configured')},
                           'policy': policy_state, 'jobs': jobs, 'jobs_total': len(rows), 'pending_actions': actions[:50], 'pending_actions_total': len(actions),
                           'usage': {'day': runtime.budget(), 'hour': runtime.budget(runtime.budget_id(hourly=True)), 'actual_cost_usd': None},
                           'inbox': Inbox(self.studio).digest(), 'delivery': Inbox(self.studio).delivery_policy(),
                           'workflows': workflows, 'audit': audit,
                           'traces': self.sessions.trace_summaries(self.queue.root),
                           'evaluation': self.sessions.evaluation_report(self.queue.root, roles=self.studio.wb.state.config.roles.roles)})

    def trace(self, identifier, operation='inspect', step=None):
        from ..evaluation.replay import inspect, why
        if operation == 'why':
            return self.clean(why(self.sessions, self.queue.root, identifier))
        return self.clean(inspect(self.sessions, self.queue.root, identifier, step))

    def job(self, identifier):
        row = self.studio.find('jarvis-job', identifier)
        return self.clean(Delegations(self.studio).view(identifier) if row.get('delegation_spec') else row)

    def approval(self, identifier):
        return self.clean(self.studio.find('jarvis-approval', identifier))

    def action(self, operation, data):
        if not isinstance(data, dict):
            raise ValueError('Object required')
        permitted = {'control': {'action', 'confirmation'}, 'review': {'id', 'action', 'confirmation'},
                     'decide': {'id', 'allow', 'confirmation'}, 'create': {'task', 'max_seconds', 'max_cost_usd'},
                     'cancel': {'id'}, 'resume': {'id'}, 'delivery': {'quiet', 'minutes'},
                     'runtime': {'enabled', 'local_model', 'confirmation'}, 'autonomy': {'level', 'confirmation'}}
        if operation not in permitted or set(data) - permitted[operation]:
            raise ValueError('Unsupported action/fields')
        if operation == 'control':
            action = data.get('action')
            if action not in ('start', 'stop', 'pause', 'resume', 'kill'):
                raise ValueError('Unsupported daemon control')
            if action in ('start', 'resume') and data.get('confirmation') != action.upper() + ' JARVIS':
                raise ValueError('Explicit start/resume confirmation required')
            result = Daemon(self.studio).start() if action == 'start' else Daemon(self.studio).request(action)
        elif operation == 'review':
            if data.get('action') not in ('approve', 'reject', 'cancel'):
                raise ValueError('Unsupported job review')
            result = self.queue.review(data['id'], data['action'], data.get('confirmation', ''))
        elif operation == 'decide':
            if type(data.get('allow')) is not bool:
                raise ValueError('Boolean decision required')
            result = Approvals(self.studio).decide(data['id'], data['allow'], data.get('confirmation', ''))
        elif operation == 'create':
            result = Delegations(self.studio).create(data['task'], max_seconds=data.get('max_seconds', 120), max_cost_usd=data.get('max_cost_usd'))
        elif operation in ('cancel', 'resume'):
            result = getattr(Delegations(self.studio), operation)(data['id'])
        elif operation == 'delivery':
            result = Inbox(self.studio).configure(quiet=data.get('quiet'), minutes=data.get('minutes'))
        elif operation == 'runtime':
            result = self.configure_runtime(data)
        else:
            result = self.autonomy(data)
        return self.clean(result)

    def configure_runtime(self, data):
        enabled = data.get('enabled')
        phrase = 'ENABLE LOCAL RUNTIME' if enabled else 'DISABLE LOCAL RUNTIME'
        if type(enabled) is not bool or data.get('confirmation') != phrase:
            raise ValueError('Exact local-runtime confirmation required')
        from ..jarvis.runtime import settings as runtime_settings
        with self.queue.locked():
            policy = self.queue.policy()
            proposed = {**policy['runtime'], 'enabled': enabled, 'local_model': data.get('local_model', '')}
            policy['runtime'] = runtime_settings(proposed)
            self.publish_policy(policy)
        return {'enabled': enabled, 'local_model': proposed['local_model'],
                'note': 'Local policy saved, not a model-availability check or task/tool approval'}

    def publish_policy(self, policy):
        policy.pop('schema', None)
        path = self.queue.root / 'autonomy.yaml'
        fd, temporary = tempfile.mkstemp(dir=self.queue.root, prefix='.autonomy-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                yaml.safe_dump(policy, handle)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def autonomy(self, data):
        level = data.get('level')
        if level not in ('L0', 'L1', 'L2', 'L3') or data.get('confirmation') != 'SET AUTONOMY ' + level:
            raise ValueError('Exact autonomy confirmation required')
        with self.queue.locked():
            policy = self.queue.policy()
            policy['level'] = level
            self.publish_policy(policy)
        return {'level': level, 'source': 'operator policy updated; autonomy never approves individual dangerous/external actions'}
