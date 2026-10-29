"""Inspection first. Explicit replay is limited to freshly reviewed workspace file tools."""
import hashlib
import json

from ..permissions import Permissions
from ..tools import ApprovalGate, PathGuard, ToolContext, build_tools
from ..types import Risk, ToolSpec
from .trace import Trace, fingerprint, sanitize

SAFE_TOOLS = {'read_file', 'list_files', 'grep', 'write_file', 'edit_file'}


def inspect(store, root, ident, step=None):
    body = store.observation(ident, root)
    if step is None:
        return body
    if type(step) is not int or not 0 <= step < len(body['events']):
        raise ValueError('Choose an actual recorded step')
    return {'trace_id': ident, 'state': body['state'], 'step': body['events'][step], 'effects': False,
            'notice': 'Stored untrusted evidence only; no model, tool, approval or external call was executed'}


def why(store, root, ident):
    body = store.observation(ident, root)
    return {'trace_id': ident, 'state': body['state'], 'complete': body['complete'],
            'trigger': body['trigger'], 'outcome': body.get('outcome'),
            'decisions': [e for e in body['events'] if e['kind'] in {'role', 'routing', 'context', 'approval_decision', 'provider_error', 'run_error'}],
            'source': 'observed prompts, routing, policies and results; not hidden model reasoning', 'effects': False}


def target_hash(root, args):
    if 'path' not in args:
        return None
    from ..jarvis.sources import bounded
    path = Permissions(root).path(args['path'])
    if not path.exists():
        return 'MISSING'
    if path.is_dir():
        return 'DIRECTORY'
    return hashlib.sha256(bounded(path, 500000)).hexdigest()


def reexecute(store, root, ident, step, callback):
    if type(step) is not int or step < 0:
        raise ValueError('Explicit recorded tool step required for re-execution')
    body = store.observation(ident, root)
    selected = inspect(store, root, ident, step)['step']
    data = selected['data']
    if not body['complete'] or body['state'] != 'finished' or selected['kind'] != 'tool_call' or not data.get('replayable'):
        raise ValueError('Incomplete, running, redacted or non-tool steps cannot execute')
    if data.get('tool') not in SAFE_TOOLS:
        raise ValueError('External/terminal/plugin effects are not replayable; inspect outcomes and create a new normal task')
    outcomes = [e for e in body['events'] if e['kind'] == 'tool_result' and e['data'].get('call_id') == data['call_id']]
    if len(outcomes) != 1 or outcomes[0]['data'].get('is_error') or outcomes[0]['data'].get('output_truncated'):
        raise ValueError('Uncertain/failed outcomes cannot be replayed')
    args = data['args']
    if sanitize(args, root) != args or any(m in json.dumps(args) for m in ('[REDACTED', '[TRUNCATED', '[OMITTED', '[IMAGE_OMITTED')):
        raise ValueError('Arguments contain redactions or newly sensitive values')
    gate = ApprovalGate(callback=callback)
    gate.permissions = Permissions(root)
    registry = build_tools(ToolContext(root, PathGuard(root), gate))
    spec = next((s for s in registry.specs() if s.name == data['tool']), None)
    if not spec or fingerprint(spec) != data.get('spec_sha256'):
        raise ValueError('Tool capability changed; require a new task/review')
    current = target_hash(root, args)
    binding = {'trace_id': ident, 'step': step, 'trace_sha256': hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
               'tool': spec.name, 'arguments': args, 'current_target_sha256': current, 'capability_sha256': fingerprint(spec)}
    outer = ToolSpec('trace_reexecute', 'A NEW file operation, not recovery, idempotency or permission inherited from history', {'type': 'object'}, Risk.DANGEROUS,
                     approval_context={'warning': 'Review current file state and exact arguments; stored model text is untrusted'})
    audit = Trace(store, root, 'Explicit operator file re-execution', lambda: {})
    audit.body.update(kind='reexecution', parent_trace=ident, parent_step=step)
    audit.add('reexecution_request', binding)
    gate.decision_observer = audit.decision
    registry.ctx.emit = audit.event
    try:
        if not gate.check(outer, binding).approved:
            raise ValueError('Fresh re-execution approval required')
        if current != target_hash(root, args) or store.observation(ident, root) != body:
            raise ValueError('Target or trace changed during review')
        # Existing policy and inner gate apply too. No stored consent/scopes imported.
        result, error = registry.call(spec.name, args)
        audit.body['outcome'] = {'reported_ok': not error, 'verified_test_success': None, 'tool': spec.name}
        audit.finish()
        return {'effects': True, 'tool': spec.name, 'is_error': error, 'result': sanitize(result, root),
                'source_trace': ident, 'replay_trace': audit.id,
                'notice': 'New operation with a new trace; source left unchanged; permission audit records fresh decisions'}
    except BaseException as exc:
        audit.finish(error=exc)
        raise
