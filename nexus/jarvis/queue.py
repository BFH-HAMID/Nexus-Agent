"""Persisted priority admission, not an enabled background execution loop."""
from __future__ import annotations

import hashlib
import math
import time
from contextlib import contextmanager

import yaml
from filelock import FileLock


class Queue:
    def __init__(self, studio):
        self.studio = studio
        self.root = studio.root

    def policy(self):
        path = self.root / 'autonomy.yaml'
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size > 64000)):
            raise ValueError('Autonomy requires a bounded operator policy')
        try:
            data = yaml.safe_load(path.read_text()) if path.exists() else {}
        except yaml.YAMLError:
            raise ValueError('Malformed autonomy policy') from None
        keys = {'schema', 'level', 'max_queue', 'max_depth', 'cooldown_seconds', 'ttl_seconds', 'max_reserved_cost', 'runtime', 'triggers'}
        if not isinstance(data, dict) or set(data) - keys or type(data.get('schema', 1)) is not int or data.get('schema', 1) != 1:
            raise ValueError('Invalid autonomy policy')
        level = data.get('level', 'L1')
        if not isinstance(level, str) or level not in {'L0', 'L1', 'L2', 'L3'}:
            raise ValueError('Unknown autonomy level')
        policy = {'level': level, 'max_queue': 20, 'max_depth': 3, 'cooldown_seconds': 60,
                  'ttl_seconds': 86400, 'max_reserved_cost': 1.0, **data}
        for key, low, high in [('max_queue', 1, 100), ('max_depth', 0, 5), ('cooldown_seconds', 1, 86400), ('ttl_seconds', 60, 604800)]:
            if type(policy[key]) is not int or not low <= policy[key] <= high:
                raise ValueError('Invalid bounded autonomy limit')
        cost = policy['max_reserved_cost']
        if type(cost) not in {int, float} or not math.isfinite(cost) or not 0 <= cost <= 100:
            raise ValueError('Invalid queue cost reservation limit')
        from .runtime import settings
        policy['runtime'] = settings(data.get('runtime', {}))
        from .events import settings as trigger_settings
        policy['triggers'] = trigger_settings(data.get('triggers', []), self.root)
        return policy

    def directory(self):
        from ..config import nexus_home
        home = nexus_home().resolve()
        root = home / 'jarvis' / hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()
        if root.resolve() != root:
            raise ValueError('Unsafe queue lock directory')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    @contextmanager
    def locked(self):
        root = self.directory()
        path = root / 'queue.lock'
        if path.is_symlink():
            raise ValueError('Unsafe queue lock')
        with FileLock(str(path), timeout=5):
            yield

    def _jobs(self):
        rows = self.studio.items('jarvis-job')
        if len(rows) > 1000:
            raise ValueError('Queue history budget exceeded; archive manually')
        for row in rows:
            if row['status'] in {'awaiting_approval', 'approved'} and row['expires'] <= time.time():
                row.update(status='expired')
                self.studio.put('jarvis-job', row, row['id'])
        return sorted(rows, key=lambda row: (row['priority'], row['created'], row['id']))

    def status(self):
        with self.locked():
            policy = self.policy()
            return {'policy': policy, 'jobs': self._jobs(), 'execution_enabled': policy['runtime']['enabled'] and policy['level'] != 'L0',
                    'execution_scope': 'loopback Ollama, scoped tools; exact task approval or explicit L2/L3 source admission; mutations separately gated',
                    'note': 'Queue inspection does not launch a worker. Runtime is operator opt-in; inspect daemon status for liveness.'}

    def enqueue(self, task, *, priority=5, parent=None, estimated_cost=0, source='explicit operator enqueue', mission=None, requires_online=False, delegation_spec=None):
        from ..computer_control import EmergencyStop
        from ..delegation.core import specification
        from ..github_safety import scan_text
        from .missions import task as mission_task
        delegation_spec = specification(delegation_spec)
        task = mission_task(mission, task)
        if EmergencyStop().stopped():
            raise ValueError('Emergency stop latched')
        if not isinstance(task, str) or not 0 < len(task.strip()) <= 8000:
            raise ValueError('Bounded task required')
        scan_text(task, workspace=self.root)
        if type(requires_online) is not bool:
            raise ValueError('Explicit online requirement must be boolean')
        if type(priority) is not int or not 0 <= priority <= 9:
            raise ValueError('Priority must be 0–9 (0 is highest)')
        if type(estimated_cost) not in {int, float} or not math.isfinite(estimated_cost) or estimated_cost < 0:
            raise ValueError('Finite nonnegative estimate required')
        signature = hashlib.sha256(' '.join(task.casefold().split()).encode()).hexdigest()
        with self.locked():
            policy = self.policy()
            if policy['level'] == 'L0':
                raise ValueError('L0 disables Jarvis admission')
            rows = self._jobs()
            if len(rows) >= 1000:
                raise ValueError('Queue history quota full; archive manually')
            active = [row for row in rows if row['status'] in {'awaiting_approval', 'approved', 'running'}]
            if len(active) >= policy['max_queue']:
                raise ValueError('Queue admission limit reached')
            if sum(row['estimated_cost'] for row in active) + estimated_cost > policy['max_reserved_cost']:
                raise ValueError('Queue reservation limit reached; no model cost was incurred')
            if any(row['signature'] == signature and (row in active or time.time() - row['created'] < policy['cooldown_seconds']) for row in rows):
                raise ValueError('Duplicate/loop cooldown blocks admission')
            depth = 0
            if parent:
                origin = next((row for row in rows if row['id'] == parent), None)
                if not origin or origin['status'] in {'cancelled', 'rejected', 'expired'}:
                    raise ValueError('Unknown or inactive parent')
                depth = origin['depth'] + 1
            if depth > policy['max_depth']:
                raise ValueError('Message/task depth budget exceeded')
            if EmergencyStop().stopped() or self.policy() != policy:
                raise ValueError('Admission policy changed; review again')
            now = time.time()
            row = {'task': task.strip(), 'priority': priority, 'parent': parent, 'depth': depth,
                                   'signature': signature, 'created': now, 'expires': now + policy['ttl_seconds'],
                                   'mission': mission, 'requires_online': requires_online, 'delegation_spec': delegation_spec, 'estimated_cost': estimated_cost, 'status': 'awaiting_approval', 'source': source}
            event_source = source.removeprefix('event:') if source.startswith('event:') else None
            if (policy['level'] in {'L2', 'L3'} and policy['runtime']['enabled']
                    and event_source in policy['runtime']['auto_sources']):
                from .runtime import signature
                row.update(status='approved', approval_kind='operator_scoped_autonomy')
                row['approval_digest'] = signature(row)
            return self.studio.put('jarvis-job', row)

    def review(self, identifier, action, confirmation=''):
        if action not in {'approve', 'reject', 'cancel', 'retry'}:
            raise ValueError('Unknown queue action')
        with self.locked():
            self.policy()
            row = next((r for r in self._jobs() if r['id'] == identifier), None)
            if action == 'retry':
                if not row or row['status'] not in {'error', 'interrupted'} or row.get('attempts', 0) >= 3 or self.policy()['level'] == 'L0':
                    raise ValueError('Task is not eligible for retry')
                if len([r for r in self._jobs() if r['status'] in {'awaiting_approval', 'approved', 'running'}]) >= self.policy()['max_queue']:
                    raise ValueError('Queue full')
                row.update(status='awaiting_approval', expires=time.time() + self.policy()['ttl_seconds'])
                row.pop('approval_digest', None)
                return self.studio.put('jarvis-job', row, row['id'])
            if not row or (row['status'] not in {'awaiting_approval', 'approved'} and not (action == 'cancel' and row['status'] == 'running')):
                raise ValueError('Unknown, expired or terminal job')
            if action == 'approve':
                if confirmation != f'APPROVE QUEUED TASK {identifier}':
                    raise ValueError('Exact fresh task approval required')
                from ..computer_control import EmergencyStop
                if EmergencyStop().stopped() or self.policy()['level'] == 'L0':
                    raise ValueError('Admission paused by policy/emergency stop')
            if action == 'approve':
                from .runtime import signature
                row['approval_digest'] = signature(row)
                row['approval_kind'] = 'human'
            row['status'] = {'approve': 'approved', 'reject': 'rejected', 'cancel': 'cancelled'}[action]
            # Queue approval is not approval for tools, GitHub mutations or external actions.
            return self.studio.put('jarvis-job', row, row['id'])
