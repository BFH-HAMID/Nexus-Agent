"""Bounded redacted run observations in the EXISTING sessions database.

A trace is evidence of observed calls, never permission or independent test proof.
"""
from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import json
import math
import re
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from urllib.parse import quote

from ..redaction import mask, secrets


def sanitize(value, root, depth=0, values=None):
    if values is None:
        original = secrets(root)
        values = sorted({variant for secret in original for variant in (secret, json.dumps(secret)[1:-1], quote(secret, safe=''), base64.b64encode(secret.encode()).decode())}, key=len, reverse=True)
    if depth > 30:
        return '[OMITTED_DEPTH]'
    if dataclasses.is_dataclass(value):
        if getattr(value, 'type', '') == 'image':
            return '[IMAGE_OMITTED]'
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        if isinstance(value.get('type'), str) and value.get('type') in {'image', 'image_url'}:
            return {'type': value['type'], 'content': '[IMAGE_OMITTED]'}
        result = {sanitize(str(k), root, depth + 1, values): ('[REDACTED]' if re.search(r'(?i)^(authorization|api[_-]?key|password|token|secret|pin)$', str(k))
                         else sanitize(v, root, depth + 1, values)) for k, v in list(value.items())[:1000]}
        if len(value) > 1000:
            result['__trace_truncated__'] = '[TRUNCATED_DICT]'
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize(v, root, depth + 1, values) for v in value[:1000]] + (['[TRUNCATED_LIST]'] if len(value) > 1000 else [])
    if isinstance(value, str):
        value = mask(value, values)
        value = re.sub(r'(?i)(password|passwd|pin|otp|secret|authorization|api[_ -]?key)\s*[:=]\s*\S+', r'\1=[REDACTED]', value)
        if not re.fullmatch(r'[0-9a-f]{64}', value):
            value = re.sub(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)', '[REDACTED_CARD]', value)
        if value.startswith('data:image/'):
            return '[IMAGE_OMITTED]'
        return value[:64000] + ('[TRUNCATED]' if len(value) > 64000 else '')
    if type(value) is int and (str(value) in values or 10**12 <= value < 10**19):
        return '[REDACTED_NUMBER]'
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    return '[UNSUPPORTED_VALUE]'


def fingerprint(spec):
    return hashlib.sha256(json.dumps({'name': spec.name, 'schema': spec.input_schema, 'risk': spec.risk.value,
        'description': spec.description, 'approval_context': spec.approval_context}, sort_keys=True).encode()).hexdigest()


class Trace:
    def __init__(self, store, root, task, price_loader):
        self.store, self.root, self.price_loader = store, root, price_loader
        self.id = uuid.uuid4().hex[:12]
        self.body = {'schema': 1, 'id': self.id, 'started': time.time(), 'workspace': str(root),
                     'trigger': sanitize(task, root), 'kind': 'agent_run', 'state': 'running', 'complete': True, 'events': [], 'models': [],
                     'approvals': {'allowed': 0, 'denied': 0}, 'provider_errors': 0, 'wire_attempts': 0}
        self.pending = {}
        self.pending_wires = {}
        self.tool_calls = {}
        self.failed_tools = set()
        self.save()

    def save(self):
        try:
            self.store.save_observation(self.id, self.root, self.body)
        except Exception:
            self.body['complete'] = False  # No replay of a trace whose journal failed.

    def add(self, kind, data):
        safe = sanitize(data, self.root)
        event = {'step': len(self.body['events']), 'at': time.time(), 'kind': kind, 'data': safe}
        if len(self.body['events']) >= 1000 or len(json.dumps(self.body)) + len(json.dumps(event)) > 1000000:
            self.body['complete'] = False
            return
        if '[TRUNCATED' in json.dumps(safe) or '[OMITTED_DEPTH]' in json.dumps(safe):
            self.body['complete'] = False
        self.body['events'].append(event)
        self.save()

    def event(self, event):
        if event.type.value in {'text_delta', 'tool_output'}:
            return  # Final model content and bounded complete tool output are recorded.
        data = dict(event.data)
        if event.type.value == 'agent' and str(data.get('message', '')).startswith('falling back to agent '):
            self.body['fallback_transitions'] = self.body.get('fallback_transitions', 0) + 1
        if event.type.value == 'tool_call':
            signature = hashlib.sha256(json.dumps(data.get('args'), sort_keys=True).encode()).hexdigest() + str(data.get('tool'))
            self.tool_calls[data.get('call_id')] = signature
            if signature in self.failed_tools:
                self.body['exact_tool_retries'] = self.body.get('exact_tool_retries', 0) + 1
            data['replayable'] = bool(data.get('call_id')) and sanitize(data.get('args'), self.root) == data.get('args')
        if event.type.value == 'tool_result' and data.get('is_error') and data.get('call_id') and data.get('call_id') in self.tool_calls:
            self.failed_tools.add(self.tool_calls[data['call_id']])
        self.add(event.type.value, data)

    def request(self, name, system, messages, tools):
        try:
            prices = self.price_loader()
        except Exception:
            prices = {}
        self.pending[name] = json.loads(json.dumps(prices))
        self.pending_wires[name] = 0
        self.add('model_request', {'agent': name, 'system': system, 'messages': messages, 'tools': tools})

    def wire(self, name, payload):
        self.body['wire_attempts'] += 1
        if self.pending_wires.get(name, 0):
            self.body['wire_retries'] = self.body.get('wire_retries', 0) + 1
        self.pending_wires[name] = self.pending_wires.get(name, 0) + 1
        self.add('model_wire', {'agent': name, 'payload': payload})

    def response(self, name, completion):
        rates = self.pending.get(name, {}).get(completion.model)
        # Zero without an explicit usage object is UNKNOWN, not free inference.
        raw_usage = completion.raw.get('usage') if isinstance(completion.raw, dict) else None
        explicit_zero_usage = isinstance(raw_usage, dict) and any(k in raw_usage for k in ('input_tokens', 'prompt_tokens')) and any(k in raw_usage for k in ('output_tokens', 'completion_tokens'))
        known = explicit_zero_usage or completion.input_tokens > 0 or completion.output_tokens > 0
        counts = (completion.input_tokens, completion.output_tokens)
        known = known and all(type(v) is int and 0 <= v < 10**12 for v in counts)
        valid = isinstance(rates, dict) and isinstance(rates.get('source'), str) and bool(rates['source'].strip())
        valid = valid and all(type(rates.get(k)) in (float, int) and math.isfinite(rates[k]) and rates[k] >= 0
                              for k in ('input_per_million', 'output_per_million'))
        cost = None
        if known and valid:
            cost = float((Decimal(str(rates['input_per_million'])) * counts[0] + Decimal(str(rates['output_per_million'])) * counts[1]) / Decimal(1000000))
        if cost is not None and not math.isfinite(cost):
            cost = None
        item = {'agent': name, 'model': completion.model, 'input_tokens': counts[0] if known else None,
                'output_tokens': counts[1] if known else None, 'price_snapshot': sanitize(rates, self.root) if valid else None,
                'calculated_model_cost_usd': cost, 'actual_cost_usd': None,
                'day': datetime.datetime.now(datetime.timezone.utc).date().isoformat()}
        if len(self.body['models']) >= 500:
            self.body['complete'] = False
            return
        self.body['models'].append(item)
        self.add('model_response', {'usage': item, 'content': completion.content, 'stop_reason': completion.stop_reason})

    def error(self, name, error):
        self.body['provider_errors'] += 1
        self.add('provider_error', {'agent': name, 'error': str(error)})

    def decision(self, spec, args, decision):
        self.body['approvals']['allowed' if decision.approved else 'denied'] += 1
        self.add('approval_decision', {'tool': spec.name, 'allowed': decision.approved, 'reason': decision.reason,
                                     'risk': spec.risk.value, 'scope': decision.scope})

    def finish(self, result=None, error=None):
        self.body.update(state='error' if error else 'finished', finished=time.time(),
                         duration_s=time.time() - self.body['started'])
        if result:
            self.body['outcome'] = {'role': result.role, 'agent': result.agent, 'reported_ok': result.ok,
                                    'stop_reason': result.stopped_reason, 'verified_test_success': None,
                                    'learning': sanitize(result.learned, self.root)}
        if error:
            self.add('run_error', {'error': str(error)})
        # Failed wire attempts can have unknown charges, even when another succeeds.
        self.body['uncertain_model_calls'] = max(0, self.body['wire_attempts'] - len(self.body['models']))
        self.save()


@contextmanager
def observed_registry(trace, registry, gate=None):
    """Compose, never replace, existing policy/budget/learning observers."""
    prior = {key: getattr(registry, key, None) for key in ('request_guard', 'wire_guard', 'response_observer', 'error_observer')}
    marker = object()
    payload_guards = {id(p): getattr(p, 'payload_guard', marker) for p in registry._cache.values()}
    old_decision = getattr(gate, 'decision_observer', None) if gate else None
    def request(name, system, messages, tools):
        cap = prior['request_guard'](name, system, messages, tools) if prior['request_guard'] else None
        trace.request(name, system, messages, tools)
        return cap
    def wire(name, payload):
        if prior['wire_guard']:
            prior['wire_guard'](name, payload)
        trace.wire(name, payload)
    def response(name, completion):
        trace.response(name, completion)
        if prior['response_observer']:
            prior['response_observer'](name, completion)
    def error(name, exc):
        trace.error(name, exc)
        if prior['error_observer']:
            prior['error_observer'](name, exc)
    def decision(spec, args, verdict):
        trace.decision(spec, args, verdict)
        if old_decision:
            old_decision(spec, args, verdict)
    registry.request_guard, registry.wire_guard = request, wire
    registry.response_observer, registry.error_observer = response, error
    if gate:
        gate.decision_observer = decision
    try:
        yield
    finally:
        for key, value in prior.items():
            setattr(registry, key, value)
        for provider in registry._cache.values():
            saved = payload_guards.get(id(provider), marker)
            if saved is marker:
                if hasattr(provider, 'payload_guard'):
                    del provider.payload_guard
            else:
                provider.payload_guard = saved
        if gate:
            gate.decision_observer = old_decision
