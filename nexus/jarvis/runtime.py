"""Bounded local, scoped dispatcher using existing agents/providers/learning.

Reservations survive crashes. No auto retry, remote model fallback or
inherited task approval for tool actions. Daemon owns the worker Popen, never a PID.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .queue import Queue


def settings(value):
    defaults = {'enabled': False, 'local_model': '', 'max_seconds': 120, 'max_turns': 4,
                'max_requests': 8, 'max_request_bytes': 100000, 'max_output_tokens': 512,
                'max_runs_day': 10, 'max_bytes_day': 1000000, 'max_seconds_day': 1200,
                'max_memory_mb': 512, 'max_cpu_percent': 90, 'mode': 'read_only',
                'write_roots': [], 'auto_sources': [], 'allow_safe_writes': False,
                'projects': [], 'connectivity': 'offline', 'max_runs_hour': 5,
                'max_requests_day': 80, 'max_requests_hour': 40, 'max_tokens_day': 2000000, 'max_tokens_hour': 1000000,
                'max_actions': 20, 'max_actions_day': 200, 'max_actions_hour': 100}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError('Unknown runtime setting')
    result = {**defaults, **value}
    if (type(result['enabled']) is not bool or type(result['allow_safe_writes']) is not bool
            or not isinstance(result['local_model'], str)):
        raise ValueError('Runtime requires explicit boolean and local model')
    if result['enabled'] and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}', result['local_model']):
        raise ValueError('Explicit local model name required')
    if result['enabled'] and result['local_model'].lower().endswith(':cloud'):
        raise ValueError('Cloud model aliases are not allowed in the local worker')
    if result['connectivity'] not in ('offline', 'online'):
        raise ValueError('Unknown connectivity policy')
    if (not isinstance(result['projects'], list) or len(result['projects']) > 5
            or any(not isinstance(p, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,59}', p) for p in result['projects'])):
        raise ValueError('Explicit bounded project names required')
    if result['mode'] not in ('read_only', 'reviewed_write'):
        raise ValueError('Unknown execution mode')
    if (not isinstance(result['write_roots'], list) or len(result['write_roots']) > 10
            or any(not isinstance(v, str) or not v or v in {'.', '/'} for v in result['write_roots'])):
        raise ValueError('Bounded workspace write roots required')
    if (not isinstance(result['auto_sources'], list) or len(result['auto_sources']) > 10
            or any(v not in ('file', 'schedule', 'build', 'todo', 'git', 'ci', 'system', 'inbox', 'idle', 'user') for v in result['auto_sources'])):
        raise ValueError('Unknown automatic event source')
    for key, low, high in [('max_seconds', 5, 600), ('max_turns', 1, 20), ('max_requests', 1, 30),
                           ('max_request_bytes', 1000, 1000000), ('max_output_tokens', 32, 4096),
                           ('max_runs_day', 1, 100), ('max_bytes_day', 1000, 10000000), ('max_seconds_day', 5, 36000),
                           ('max_runs_hour', 1, 100), ('max_requests_day', 1, 3000), ('max_requests_hour', 1, 3000),
                           ('max_tokens_day', 1000, 20000000), ('max_tokens_hour', 1000, 20000000),
                           ('max_actions', 1, 100), ('max_actions_day', 1, 10000), ('max_actions_hour', 1, 10000),
                           ('max_memory_mb', 256, 2048), ('max_cpu_percent', 20, 100)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Invalid bounded runtime limit')
    return result


def signature(row):
    payload = {k: row.get(k) for k in ('task', 'priority', 'parent', 'depth', 'expires', 'estimated_cost', 'source', 'mission', 'requires_online')}
    # Keep existing non-delegated Module 4 approvals valid; delegated limits are
    # additional immutable authority and must be covered by their own approval.
    if row.get('delegation_spec') is not None:
        payload['delegation_spec'] = row['delegation_spec']
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()



class Runtime:
    def __init__(self, studio):
        self.studio = studio
        self.queue = Queue(studio)
        self.active = None
        self.active_started = None

    def budget_id(self, hourly=False, now=None):
        return 'jarvis-budget-' + hashlib.sha256(str(self.queue.root).encode()).hexdigest()[:24] + '-' + (now or datetime.now(timezone.utc)).strftime('%Y%m%d%H' if hourly else '%Y%m%d')

    def budget(self, identifier=None):
        return next((r for r in self.studio.items('jarvis-budget') if r['id'] == (identifier or self.budget_id())),
                    {'runs_reserved': 0, 'bytes_reserved': 0, 'seconds_reserved': 0, 'actual_input_tokens': 0, 'actual_output_tokens': 0})

    def claim(self):
        from ..computer_control import EmergencyStop
        with self.queue.locked():
            policy = self.queue.policy()
            cfg = dict(policy['runtime'])
            if not cfg['enabled'] or policy['level'] == 'L0' or EmergencyStop().stopped():
                return None
            if len(self.studio.items('jarvis-notice')) >= 1000:
                return None  # Preserve the inbox; never silently drop results.
            rows = self.queue._jobs()
            if any(r['status'] == 'running' for r in rows):
                return None  # Orphans need explicit recovery; never replay automatically.
            for candidate in rows:
                if candidate['status'] == 'approved' and candidate.get('approval_kind') == 'operator_scoped_autonomy':
                    source = candidate['source'].removeprefix('event:')
                    if policy['level'] not in {'L2', 'L3'} or source not in cfg['auto_sources']:
                        candidate.update(status='awaiting_approval', approval_kind='revoked_autonomy')
                        candidate.pop('approval_digest', None)
                        self.studio.put('jarvis-job', candidate, candidate['id'])
            from ..delegation.costs import price
            row = next((r for r in rows if r['status'] == 'approved'
                        and ((r.get('delegation_spec') or {}).get('max_cost_usd') is None or price(self.studio, cfg['local_model']) is not None)
                        and (not r.get('requires_online') or cfg['connectivity'] == 'online')), None)
            if row is None:
                return None
            if row.get('approval_digest') != signature(row):
                raise ValueError('Queue approval no longer matches the exact task')
            if row.get('delegation_spec'):
                from ..delegation.core import specification
                spec = specification(row['delegation_spec'])
                cfg['max_seconds'] = min(cfg['max_seconds'], spec['max_seconds'])
            from ..delegation.costs import price
            rates = price(self.studio, cfg['local_model']) if (row.get('delegation_spec') or {}).get('max_cost_usd') is not None else None
            if (row.get('delegation_spec') or {}).get('max_cost_usd') is not None and rates is None:
                return None  # No reservation/inference until explicit operator price exists.
            from ..github_safety import scan_text
            scan_text(row['task'], workspace=self.queue.root)
            stamp = datetime.now(timezone.utc)
            budget_key = self.budget_id(now=stamp)
            budget = self.budget(budget_key)
            hour_key = self.budget_id(hourly=True, now=stamp)
            hourly = self.budget(hour_key)
            token_reservation = cfg['max_request_bytes'] + cfg['max_requests'] * cfg['max_output_tokens']
            for record, period in ((budget, 'day'), (hourly, 'hour')):
                if (record['runs_reserved'] >= cfg['max_runs_' + period]
                        or record.get('requests_reserved', 0) + cfg['max_requests'] > cfg['max_requests_' + period]
                        or record.get('tokens_reserved', 0) + token_reservation > cfg['max_tokens_' + period]
                        or record.get('actions_reserved', 0) + cfg['max_actions'] > cfg['max_actions_' + period]):
                    return None
            if (budget['runs_reserved'] >= cfg['max_runs_day'] or
                    budget['bytes_reserved'] + cfg['max_request_bytes'] > cfg['max_bytes_day'] or
                    budget.get('seconds_reserved', 0) + cfg['max_seconds'] > cfg['max_seconds_day']):
                return None
            budget.update(runs_reserved=budget['runs_reserved'] + 1,
                          bytes_reserved=budget['bytes_reserved'] + cfg['max_request_bytes'],
                          seconds_reserved=budget.get('seconds_reserved', 0) + cfg['max_seconds'])
            for record, key in ((budget, budget_key), (hourly, hour_key)):
                if key == hour_key:
                    record['runs_reserved'] += 1
                record.update(requests_reserved=record.get('requests_reserved', 0) + cfg['max_requests'],
                              tokens_reserved=record.get('tokens_reserved', 0) + token_reservation,
                              actions_reserved=record.get('actions_reserved', 0) + cfg['max_actions'])
                self.studio.put('jarvis-budget', record, key)  # Reserve BEFORE launch, never silently refund a crash.
            if row.get('attempts', 0) >= 3:
                raise ValueError('Task attempt cap reached')
            row.update(status='running', attempts=row.get('attempts', 0) + 1, worker_started=False, lease=uuid.uuid4().hex, started=time.time(), runtime=cfg,
                       budget_id=budget_key, hour_budget_id=hour_key, model_price=rates, runtime_policy=hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest())
            from ..delegation.core import append
            append(row, 'claimed')
            row['delegation_alerts'] = []
            return self.studio.put('jarvis-job', row, row['id'])

    def finish(self, row, state, result):
        if state not in {'done', 'error', 'interrupted'}:
            raise ValueError('Unknown runtime outcome')
        from ..redaction import mask, secrets
        with self.queue.locked():
            current = self.studio.find('jarvis-job', row['id'])
            if current.get('lease') != row['lease'] or current['status'] != 'running':
                raise ValueError('Worker lease revoked; outcome is not authoritative')
            safe = json.loads(mask(json.dumps(result), secrets(self.queue.root)))
            if row.get('model_price'):
                from ..delegation.costs import amount
                counts = [result.get(k, 0) for k in ('input_tokens', 'output_tokens')]
                if all(k in result for k in ('input_tokens', 'output_tokens')) and all(type(v) is int and v >= 0 for v in counts):
                    safe['calculated_model_cost_usd'] = float(amount(row['model_price'], *counts))
                    safe['cost_basis'] = row['model_price']
                    safe['actual_cost_usd'] = None
            from ..delegation.core import append
            append(current, 'finished', outcome=state)
            current.update(status=state, finished=time.time(), result=safe, result_attempt=current.get('attempts', 0))
            saved = self.studio.put('jarvis-job', current, current['id'])
            for identifier in (row['budget_id'], row['hour_budget_id']):
                budget = next(r for r in self.studio.items('jarvis-budget') if r['id'] == identifier)
                for field in ('input_tokens', 'output_tokens'):
                    count = result.get(field, 0)
                    if type(count) is not int or count < 0:
                        count = 0
                    budget['actual_' + field] = budget.get('actual_' + field, 0) + count
                self.studio.put('jarvis-budget', budget, identifier)
            self.studio.put('jarvis-notice', {'job_id': row['id'], 'state': state, 'created': time.time(),
                                           'summary': safe.get('summary', '')[:2000], 'read': False})
            return saved

    def tick(self, permitted=True):
        if self.active:
            process, row = self.active
            cfg = self.queue.policy()['runtime'] if permitted else None
            current = self.studio.find('jarvis-job', row['id'])
            import psutil
            try:
                memory_exceeded = process.poll() is None and psutil.Process(process.pid).memory_info().rss > row['runtime']['max_memory_mb'] * 1024 * 1024
            except psutil.Error:
                memory_exceeded = False
            expected_cfg = dict(cfg) if cfg else None
            if expected_cfg and row.get('delegation_spec'):
                expected_cfg['max_seconds'] = min(expected_cfg['max_seconds'], row['delegation_spec']['max_seconds'])
            if memory_exceeded or current['status'] != 'running' or not permitted or expected_cfg != row['runtime'] or time.monotonic() - self.active_started > row['runtime']['max_seconds']:
                from ..live_process import terminate_group
                terminate_group(process)
                current = self.studio.find('jarvis-job', row['id'])
                if current['status'] == 'running':
                    self.finish(row, 'interrupted', {'summary': 'Stopped by lifecycle/policy/time limit; no automatic retry'})
                self.active = None
            elif process.poll() is not None:
                process.wait()
                current = self.studio.find('jarvis-job', row['id'])
                if current['status'] == 'running':
                    self.finish(row, 'error', {'summary': 'Worker exited without a durable outcome; inspect before retry'})
                self.active = None
            elif row.get('delegation_spec'):
                # Optional alerts must never prevent owned-process termination.
                import sqlite3

                from ..delegation.core import Delegations
                try:
                    Delegations(self.studio).progress(row, time.monotonic() - self.active_started)
                except (ValueError, OSError, TimeoutError, sqlite3.Error):
                    pass
            return
        if not permitted:
            return
        import psutil
        if psutil.cpu_percent(interval=None) > self.queue.policy()['runtime']['max_cpu_percent']:
            return
        row = self.claim()
        if row:
            try:
                process = subprocess.Popen([sys.executable, '-I', '-m', 'nexus.jarvis.worker', str(self.queue.root), row['id'], row['lease']],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           start_new_session=os.name == 'posix', close_fds=True)
                self.active_started = time.monotonic()
                self.active = process, row
            except OSError:
                self.finish(row, 'error', {'summary': 'Worker launch failed; reservation retained'})
                raise

    def close(self):
        self.tick(False)


def execute(studio, identifier, lease, *, agent_factory=None, registry_factory=None):
    """Called only by an owned worker. Factories are injectable for offline tests."""
    import httpx

    from ..agent import NexusAgent
    from ..computer_control import EmergencyStop
    from ..config import load_config
    from ..integrations import ollama_url, use_local_model
    from ..providers import OpenAICompatProvider, ProviderRegistry
    from ..types import ProviderError
    runtime = Runtime(studio)
    with runtime.queue.locked():
        row = studio.find('jarvis-job', identifier)
        if row.get('lease') != lease or row['status'] != 'running' or row.get('worker_started'):
            raise ValueError('Invalid or already consumed worker ownership')
        row['worker_started'] = True
        studio.put('jarvis-job', row, identifier)
    cfg = row['runtime']
    calls = used = 0
    from decimal import Decimal
    reserved_cost = Decimal(0)
    observed = {'input_tokens': 0, 'output_tokens': 0}
    client = None
    try:
        endpoint = urlsplit(ollama_url())
        if endpoint.scheme != 'http' or endpoint.hostname not in {'localhost', '127.0.0.1', '::1'} or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path:
            raise ValueError('Background execution requires loopback Ollama; no remote fallback')
        config = load_config(studio.root)
        use_local_model(config, cfg['local_model'])
        for role in config.roles.roles.values():
            role.model = None  # An inherited role override cannot change the operator-pinned model.
        local = config.agents['selected-ollama']
        host = '[::1]' if endpoint.hostname == '::1' else '127.0.0.1'
        local.base_url = f'http://{host}:{endpoint.port or 80}/v1'
        local.max_tokens = cfg['max_output_tokens']
        local.timeout = min(cfg['max_seconds'], 60)
        local.stream = False
        local.fallback = None
        config.agents = {'selected-ollama': local}
        config.settings.max_turns = cfg['max_turns']
        client = httpx.Client(timeout=local.timeout, trust_env=False, follow_redirects=False)
        registry = registry_factory(config) if registry_factory else ProviderRegistry(config.agents, factories={'selected-ollama': OpenAICompatProvider(local, transport=client)})
        def guard(name, payload):
            nonlocal calls, used, reserved_cost
            current = studio.find('jarvis-job', identifier)
            policy = runtime.queue.policy()
            if (EmergencyStop().stopped() or current.get('lease') != lease or current['status'] != 'running'
                    or current.get('approval_digest') != signature(current)
                    or row['runtime_policy'] != hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()):
                raise ProviderError('Runtime permission revoked', retryable=False)
            size = len(json.dumps(payload).encode())
            if (name != 'selected-ollama' or payload.get('model') != cfg['local_model']
                    or type(payload.get('max_tokens')) is not int or not 1 <= payload['max_tokens'] <= cfg['max_output_tokens']):
                raise ProviderError('Only the pinned local model is permitted', retryable=False)
            if calls + 1 > cfg['max_requests'] or used + size > cfg['max_request_bytes']:
                raise ProviderError('Persistent reservation request ceiling reached', retryable=False)
            if row.get('model_price') is not None:
                from ..delegation.costs import alert, amount, price
                if price(studio, cfg['local_model']) != row['model_price']:
                    raise ProviderError('Operator price changed; review and retry', retryable=False)
                allocation = amount(row['model_price'], size, payload['max_tokens'])
                if reserved_cost + allocation > Decimal(str(row['delegation_spec']['max_cost_usd'])):
                    raise ProviderError('Delegation priced model-cost cap exhausted before wire', retryable=False)
                reserved_cost += allocation
                alert(studio, row, reserved_cost)
            calls += 1
            used += size
        registry.wire_guard = guard
        registry.request_guard = lambda *args: cfg['max_output_tokens']
        def observe(name, completion):
            observed['input_tokens'] += max(0, completion.input_tokens)
            observed['output_tokens'] += max(0, completion.output_tokens)
            if row.get('model_price'):
                from ..delegation.costs import amount
                if amount(row['model_price'], observed['input_tokens'], observed['output_tokens']) > Decimal(str(row['delegation_spec']['max_cost_usd'])):
                    raise ProviderError('Reported priced usage exceeds cap; further work refused', retryable=False)
        registry.response_observer = observe
        def checkpoint(event):
            if row.get('delegation_spec'):
                from ..delegation.core import Delegations
                Delegations(studio).checkpoint(row, event)
        agent = (agent_factory or NexusAgent)(config, workspace=studio.root, providers=registry, auto_yes=False,
                                             approval_callback=None, emit=checkpoint)
        from .approvals import Approvals
        writable = cfg['mode'] == 'reviewed_write'
        roots = [agent.gate.permissions.path(p) for p in cfg['write_roots']]
        if any(root == studio.root.resolve() for root in roots):
            raise ValueError('Whole-workspace unattended writes are refused')
        agent.plan_mode = not writable
        agent.gate.mode = 'ask' if writable else 'plan'
        agent.gate.always_allow.clear()
        agent.gate.session_allowed.clear()
        agent.gate.callback = Approvals(studio).callback(row)
        def scope(spec, args):
            current = studio.find('jarvis-job', row['id'])
            if current['status'] != 'running' or current.get('lease') != row['lease']:
                return 'Job lease revoked'
            if hashlib.sha256(json.dumps(runtime.queue.policy(), sort_keys=True).encode()).hexdigest() != row['runtime_policy']:
                return 'Runtime policy changed'
            agent.gate.mode = 'ask' if writable else 'plan'
            if spec.is_readonly_call(args):
                return None
            if spec.name == 'project_action':
                return None if cfg['connectivity'] == 'online' and args.get('name') in cfg['projects'] else 'Offline or outside project allowlist'
            if spec.name not in {'write_file', 'edit_file', 'delete_file'} or not writable:
                return 'Background mutation surface is not authorized'
            path = agent.gate.permissions.path(args['path'])
            if path.is_relative_to(Path(__file__).resolve().parents[1]):
                return 'Cannot rewrite the running agent installation'
            if not any(path.is_relative_to(root) for root in roots):
                return 'Path is outside operator write roots'
            # L3 only opts explicit per-tool rules into non-destructive new/append writes.
            if (runtime.queue.policy()['level'] == 'L3' and cfg['allow_safe_writes']
                    and spec.name == 'write_file' and (not path.exists() or args.get('append') is True)
                    and agent.gate.permissions.load()['tools'].get(spec.name) == 'AUTO_ALLOW'):
                agent.gate.mode = 'auto'
            return None
        agent.gate.policy = scope
        action_calls = 0
        native_tools = agent.tools_for
        def safe_tools(*args, **kwargs):
            ctx, tools, _ = native_tools(*args, **kwargs)
            role = args[0] if args else kwargs['role']
            with runtime.queue.locked():
                current = studio.find('jarvis-job', row['id'])
                if current['status'] != 'running' or current.get('lease') != row['lease']:
                    raise ValueError('Job lease revoked')
                current['active_role'] = role.name
                studio.put('jarvis-job', current, current['id'])
            if writable and cfg['projects'] and cfg['connectivity'] == 'online' and row.get('mission') in (None, 'dev') and (not role.tools or 'project_action' in role.tools):
                from ..types import Risk
                from .actions import project
                tools.register('project_action', 'Review mapped conventional commit/push/CI-gated PR/merge; fresh action approval always.',
                               {'type': 'object', 'properties': {'name': {'type': 'string'},
                                'action': {'enum': ['commit', 'push', 'pr', 'merge']}, 'message': {'type': 'string'}},
                                'required': ['name', 'action']}, lambda data: project(studio, row, data), risk=Risk.DANGEROUS)
            tools.keep_only({'list_files', 'read_file', 'grep', 'finish', 'skill_view'} | ({'write_file', 'edit_file', 'delete_file', 'project_action'} if writable else set()))
            native_call = tools.call
            def bounded_call(name, data):
                nonlocal action_calls
                if action_calls >= cfg['max_actions']:
                    return 'DENIED: per-job action budget exhausted', True
                action_calls += 1
                with runtime.queue.locked():
                    current = studio.find('jarvis-job', row['id'])
                    if current['status'] != 'running' or current.get('lease') != row['lease']:
                        return 'DENIED: job lease revoked', True
                    current['action_calls'] = action_calls
                    studio.put('jarvis-job', current, current['id'])
                return native_call(name, data)
            tools.call = bounded_call
            return ctx, tools, tools.specs()
        agent.tools_for = safe_tools
        task = row['task']
        if row.get('delegation_spec') and row.get('attempts', 0) > 1:
            task += '\nRecovery: inspect existing effects with read-only tools before any new change. Prior calls may have applied. Never replay checkpoint tools automatically.'
        result = agent.run(task, read_only=not writable, max_turns=cfg['max_turns'])
        calculated_cost = None
        if row.get('model_price'):
            from ..delegation.costs import amount
            calculated_cost = float(amount(row['model_price'], observed['input_tokens'], observed['output_tokens']))
        return runtime.finish(row, 'done' if result.ok else 'error', {'summary': str(result.text)[:32000], 'session_id': result.session_id,
                              'role': result.role, 'request_bytes': used, 'requests': calls, **observed,
                              'direct_cloud_adapter_used': False, 'actual_cost_usd': None,
                              'calculated_model_cost_usd': calculated_cost, 'reserved_model_cost_usd': float(reserved_cost) if row.get('model_price') else None,
                              'cost_basis': row.get('model_price'),
                              'source': 'loopback-only scoped agent; source mutations require operator policy/action approval'})
    except Exception as exc:
        return runtime.finish(row, 'error', {'summary': 'Local worker refused/failed: ' + type(exc).__name__, **observed,
                                           'requests': calls, 'request_bytes': used})
    finally:
        if client:
            client.close()
