"""Explicit delegation/checkpoint inspection. Never replays a recorded tool call."""
from __future__ import annotations

import hashlib
import json
import re
import time

from ..jarvis.queue import Queue


def specification(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or not {'schema', 'max_seconds'} <= set(value) or set(value) - {'schema', 'max_seconds', 'max_cost_usd'} or type(value['schema']) is not int or value['schema'] != 1):
        raise ValueError('Invalid delegation specification')
    if type(value['max_seconds']) is not int or not 5 <= value['max_seconds'] <= 600:
        raise ValueError('Delegation requires a bounded 5–600 second deadline')
    if 'max_cost_usd' in value:
        from .costs import cap
        cap(value['max_cost_usd'])
    return dict(value)


def status_is_running(row):
    return row['status'] == 'running'


def append(row, event, **fields):
    if not row.get('delegation_spec'):
        return
    entries = row.setdefault('delegation_checkpoints', [])
    if len(entries) >= 1024:
        raise ValueError('Checkpoint quota exceeded; inspect rather than replay')
    entries.append({'sequence': len(entries) + 1, 'at': time.time(), 'attempt': row.get('attempts', 0), 'event': event, **fields})


class Delegations:
    def __init__(self, studio):
        self.studio = studio
        self.queue = Queue(studio)

    def create(self, task, *, max_seconds=120, priority=5, mission=None, online=False, max_cost_usd=None):
        spec = {'schema': 1, 'max_seconds': max_seconds}
        if max_cost_usd is not None:
            spec['max_cost_usd'] = max_cost_usd
        return self.queue.enqueue(task, priority=priority, mission=mission, requires_online=online,
                                  source='explicit operator delegation',
                                  delegation_spec=specification(spec))

    def _job(self, identifier):
        row = next((r for r in self.queue._jobs() if r['id'] == identifier), None)
        if not row or not row.get('delegation_spec'):
            raise ValueError('Delegation not found in this workspace')
        return row

    def view(self, identifier):
        with self.queue.locked():
            row = self._job(identifier)
            policy = self.queue.policy()
            pending = [r['id'] for r in self.studio.items('jarvis-approval') if status_is_running(row) and r['job_id'] == identifier
                       and r['state'] == 'pending' and r['expires'] > time.time() and r['lease'] == row.get('lease')]
            status = row['status']
            from .costs import price
            cost_requested = row['delegation_spec'].get('max_cost_usd') is not None
            blockers = []
            if cost_requested and price(self.studio, policy['runtime']['local_model']) is None:
                blockers.append('unknown_model_price')
            if not policy['runtime']['enabled']:
                blockers.append('runtime_disabled')
            if policy['level'] == 'L0':
                blockers.append('autonomy_L0')
            if row.get('requires_online') and policy['runtime']['connectivity'] != 'online':
                blockers.append('offline')
            if status == 'interrupted':
                blockers.append('explicit_resume_and_fresh_approval_required')
            state = {'awaiting_approval': 'waiting_approval', 'approved': 'queued', 'running': 'running',
                     'done': 'done', 'error': 'failed', 'interrupted': 'blocked', 'expired': 'failed',
                     'cancelled': 'cancelled', 'rejected': 'rejected'}[status]
            if status == 'running' and pending:
                state = 'waiting_approval'
            if status == 'approved' and blockers:
                state = 'blocked'
            checkpoints = row.get('delegation_checkpoints', [])
            outcomes = {(c['attempt'], c.get('call_id')): c for c in checkpoints if c['event'] == 'tool_result' and c.get('call_id')}
            uncertain = [c for c in checkpoints if c['event'] == 'tool_call' and c.get('call_id')
                         and ((c['attempt'], c['call_id']) not in outcomes or outcomes[(c['attempt'], c['call_id'])]['reported_error'])]
            return {'id': identifier, 'state': state, 'queue_status': status, 'task': row['task'], 'blockers': blockers,
                    'attempts': row.get('attempts', 0), 'pending_actions': pending,
                    'deadline_seconds': min(row['delegation_spec']['max_seconds'], policy['runtime']['max_seconds']),
                    'checkpoints': checkpoints, 'uncertain_calls': uncertain, 'alerts': row.get('delegation_alerts', []),
                    'result': row.get('result'),
                    'result_provenance': {'attempt': row.get('result_attempt'), 'current_attempt_terminal': status in {'done', 'error', 'interrupted'}},
                    'executor_liveness': 'Not inferred from queue state; use nexus jarvis daemon-status', 'actual_cost_usd': row.get('result', {}).get('actual_cost_usd'),
                    'cost_limit_enforced': bool(cost_requested and row.get('model_price')),
                    'model_cost_cap_usd': row['delegation_spec'].get('max_cost_usd'),
                    'calculated_model_cost_usd': row.get('result', {}).get('calculated_model_cost_usd'),
                    'cost_basis': 'operator-priced token accounting, not electricity or provider invoice',
                    'evidence': 'Observed lifecycle/tool metadata and agent report; not independent test/acceptance proof',
                    'recovery': 'Resume creates a fresh approved attempt, never replays recorded tools; inspect prior effects first.'}

    def tasks(self):
        return [self.view(r['id']) for r in self.queue._jobs() if r.get('delegation_spec')]

    def cancel(self, identifier):
        self._job(identifier)
        self.queue.review(identifier, 'cancel')
        return self.view(identifier)

    def resume(self, identifier):
        # In-flight/stale-running jobs must first be reconciled by existing daemon
        # recovery. Never infer that a worker is dead from a persisted PID.
        self._job(identifier)
        self.queue.review(identifier, 'retry')
        return self.view(identifier)

    def checkpoint(self, job, event):
        kind = event.type.value
        if kind not in {'role', 'tool_call', 'tool_result'}:
            return
        data = event.data
        fields = {}
        if kind == 'role':
            role = data.get('role', '')
            if not isinstance(role, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', role):
                raise ValueError('Invalid checkpoint role')
            fields['role'] = role
        else:
            tool = data.get('tool', '')
            if not isinstance(tool, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', tool):
                raise ValueError('Invalid checkpoint tool')
            fields['tool'] = tool
            call_id = data.get('call_id')
            if call_id is not None and (not isinstance(call_id, str) or not re.fullmatch(r'[a-f0-9]{32}', call_id)):
                raise ValueError('Invalid checkpoint call ID')
            fields['call_id'] = call_id
            if kind == 'tool_call':
                # Do NOT persist file bodies, credentials, untrusted output or screenshots.
                fields['arguments_sha256'] = hashlib.sha256(json.dumps(data.get('args', {}), sort_keys=True).encode()).hexdigest()
            else:
                fields['reported_error'] = data.get('is_error') is True
        with self.queue.locked():
            current = self._job(job['id'])
            if current['status'] != 'running' or current.get('lease') != job['lease']:
                raise ValueError('Delegation lease revoked')
            append(current, kind, **fields)
            self.studio.put('jarvis-job', current, current['id'])

    def progress(self, job, elapsed):
        if not job.get('delegation_spec') or elapsed < .8 * job['runtime']['max_seconds']:
            return
        with self.queue.locked():
            current = self._job(job['id'])
            if current['status'] != 'running' or current.get('lease') != job['lease'] or any(a['kind'] == 'time_80_percent' for a in current.get('delegation_alerts', [])):
                return
            current.setdefault('delegation_alerts', []).append({'kind': 'time_80_percent', 'at': time.time(), 'elapsed_seconds': round(elapsed, 2)})
            self.studio.put('jarvis-job', current, current['id'])
            if len(self.studio.items('jarvis-notice')) < 1000:
                self.studio.put('jarvis-notice', {'job_id': job['id'], 'state': 'budget_warning', 'created': time.time(),
                                               'summary': 'Delegation has used 80% of its wall-time allowance.', 'read': False})
