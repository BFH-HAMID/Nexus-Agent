"""Computer Studio execution uses the existing server queue/live approval sessions.

This module is transport glue, not another privileged executor. There is no model
interpretation, no saved grant, and every nested macro/capture approval uses the
same live ApprovalGate as other Nexus runs.
"""
from __future__ import annotations

import json
import time

from .computer_actions import PREPARED_TOOLS, digest, registry_for


def studio_policy(state):
    from . import computerstudio as studio
    from .computer_policy import url_origin
    from .permissions import Permissions
    def policy(spec, args):
        try:
            if getattr(state, 'demo', False):
                return 'Computer effects are disabled in demo mode'
            if spec.name in {'file_action', 'write_file', 'edit_file', 'delete_file', 'undo_file'}:
                studio._level_or_refuse(state, 'files', 'ask')
                paths = [args[k] for k in ('path', 'destination') if k in args]
                if spec.name == 'file_action' and args.get('action') == 'organize':
                    paths += [v for move in args.get('moves', []) for v in (move['source'], move['destination'])]
                if spec.name == 'file_action' and args.get('action') == 'restore':
                    from .computer_files import trash_record
                    reg = registry_for(state.workspace)
                    paths.append(str(trash_record(reg.ctx, args['id'])[1]))
                for path in paths:
                    studio._scoped(state, path)
            if spec.name == 'browser_action':
                studio._level_or_refuse(state, 'browser', 'read')
                cfg = Permissions(state.workspace).load()['computer']
                urls = [args[k] for k in ('url', 'target') if k in args]
                if args.get('action') == 'search':
                    urls.append(cfg['browser_controls']['search_url'].replace('{query}', 'query'))
                origins = studio.load_settings(state.workspace)['scopes']['origins']
                if any(url_origin(url) not in origins for url in urls):
                    return 'Browser origin is outside the narrower Computer Studio scope'
        except (ValueError, OSError, KeyError):
            return 'Computer Studio scope/permission validation failed'
        return ''
    return policy


def start(state, payload, *, backup_id=None):
    if getattr(state, 'demo', False):
        raise ValueError('Live computer execution is disabled in demo mode')
    tool, args = payload.get('tool'), payload.get('arguments')
    if tool not in PREPARED_TOOLS or not isinstance(args, dict) or len(json.dumps(args).encode()) > 48000:
        raise ValueError('Choose an enabled computer tool with bounded JSON arguments')
    from .github_safety import scan_text
    scan_text(json.dumps(args), workspace=state.workspace)
    reg = registry_for(state.workspace)
    if not reg.has(tool):
        raise ValueError('Computer tool disabled in operator YAML')
    # Merely queuing cannot execute anything. Exact target resolution occurs when
    # the single existing server queue reaches this task, immediately before review.
    session = state.create('Computer: ' + tool, None, False, options=state.workbench.prepare({}))
    session.computer_request = {'tool': tool, 'arguments': json.loads(json.dumps(args)), 'backup_id': backup_id}
    state.start(session)
    return {'task_id': session.task_id, 'status': session.status, 'queued': True,
            'note': 'Queued in the existing Run/Approval Center. Review each fresh action; enabling YAML never approves it.'}


def run(state, session):
    from .tools import ApprovalGate
    started = time.monotonic()
    gate = ApprovalGate(callback=state._approval(session), emit=state._emit(session))
    narrow = studio_policy(state)
    workbench = state.workbench.write_policy(session.options['policy'])
    gate.policy = lambda spec, args: narrow(spec, args) or workbench(spec, args)
    reg = registry_for(state.workspace, gate, cancel=session.cancel, emit=state._emit(session))
    session.status = 'running'
    request = session.computer_request
    output, error = reg.call(request['tool'], request['arguments'])
    if not error and request.get('backup_id'):
        from .assistant.automation import BackupManager
        value = BackupManager(state.workspace).remember(request['backup_id'], json.loads(output))
        output = json.dumps(value)
    session.status = 'cancelled' if session.cancel.is_set() else 'error' if error else 'done'
    session.result = {'ok': not error, 'execution': 'shared computer tool registry', 'text': output,
                      'summary': 'Computer action refused/failed; inspect prior effects.' if error else 'Reviewed computer action completed.',
                      'tool_calls': len(gate.decisions), 'input_tokens': 0, 'output_tokens': 0,
                      'duration_s': round(time.monotonic() - started, 2), 'changed_files': sorted(reg.ctx.state.get('changed_files', [])),
                      'stopped_reason': 'cancelled' if session.cancel.is_set() else ''}


LEGACY = {'file/write', 'file/undo', 'file/trash', 'trash/restore', 'browser/open'}


def legacy_plan(state, operation, payload):
    from . import computerstudio as studio
    from .computer_actions import identity
    from .computer_files import image
    from .permissions import Permissions
    from .snapshots import Snapshots
    from .types import Risk, ToolSpec
    if operation not in LEGACY:
        raise ValueError('Unknown Computer Studio review action')
    permissions = Permissions(state.workspace)
    config = permissions.load()
    reg = registry_for(state.workspace)
    detail = None
    if operation == 'file/write':
        studio._level_or_refuse(state, 'files', 'ask')
        _, path = studio._scoped(state, payload.get('path'))
        if permissions.operator_control(path):
            raise ValueError('Operator-controlled files cannot be written')
        if path.exists() and (not path.is_file() or path.stat().st_size > studio.MAX_WRITE):
            raise ValueError('Only bounded regular files may be written')
        name = 'write_file'
        args = {'path': payload.get('path'), 'content': payload.get('text')}
        detail = image(reg.ctx, path, config['computer']['files']) if path.exists() else {'identity': identity(path)}
    elif operation == 'file/undo':
        studio._level_or_refuse(state, 'files', 'ask')
        detail = Snapshots(state.workspace).preview(payload.get('id'))
        studio._scoped(state, detail['path'])
        name = 'undo_file'
        args = {'path': detail['path'], 'snapshot': detail['id'], 'after': detail['after']}
    elif operation in {'file/trash', 'trash/restore'}:
        name = 'file_action'
        args = {'action': 'delete', 'path': payload.get('path')} if operation == 'file/trash' else {'action': 'restore', 'id': payload.get('id')}
    else:
        name, args = 'browser_action', {'action': 'inspect', 'url': payload.get('url')}
    from .github_safety import scan_text
    scan_text(json.dumps(args), workspace=state.workspace)
    if name == 'undo_file':
        spec = ToolSpec(name, 'Restore scoped snapshot', {'type': 'object'}, Risk.DANGEROUS)
        prepared = args
    else:
        if not reg.has(name):
            raise ValueError('Computer tool disabled in permissions.yaml')
        tool = reg._tools[name]
        spec, prepared = tool.spec, tool.prepare(args) if tool.prepare else args
    reason = studio_policy(state)(spec, prepared)
    if reason:
        raise ValueError(reason)
    rule = permissions.evaluate(spec, prepared)
    if rule['mode'] == 'READ_ONLY':
        raise ValueError('permissions.yaml READ_ONLY refuses this operation')
    stamp = {'operation': operation, 'payload': {k: v for k, v in payload.items() if k not in {'_client', 'approval_id', 'approval'}},
             'policy': digest(config), 'studio': studio.load_settings(state.workspace)['revision'],
             'prepared': prepared, 'detail': detail, 'client': str(payload.get('_client', 'local'))}
    return reg, name, args, prepared, detail, stamp


def review(state, payload):
    from .assistant.common import FreshApproval
    operation, args = payload.get('operation'), payload.get('payload')
    if not isinstance(args, dict) or len(json.dumps(args).encode()) > 500000:
        raise ValueError('Bounded exact action payload required')
    args = {**args, '_client': payload.get('_client', 'local')}
    _reg, name, _args, prepared, _detail, stamp = legacy_plan(state, operation, args)
    with state.lock:
        if not hasattr(state, '_computer_reviews'):
            state._computer_reviews = FreshApproval(ttl=60)
        # Bound memory and discard expired request digests, never retain file bodies.
        pending = state._computer_reviews._pending
        for ident in list(pending):
            if pending[ident]['expires'] <= state._computer_reviews.clock():
                pending.pop(ident)
        if len(pending) >= 100:
            raise ValueError('Too many pending computer reviews')
        challenge = state._computer_reviews.begin('ALLOW COMPUTER', stamp)
    from .tools import preview_args
    return {**challenge, 'operation': operation, 'preview': preview_args(
            _reg._tools[name].spec, prepared) if name in _reg._tools else 'Restore exact current snapshot',
            'note': 'One use, 60 seconds, bound to this client, payload, file/target, policy and Studio scope.'}


def approved(state, operation, payload):
    from .tools import ApprovalDecision
    reg, name, args, prepared, detail, stamp = legacy_plan(state, operation, payload)
    with state.lock:
        reviews = getattr(state, '_computer_reviews', None)
        if not reviews or not reviews.consume(payload.get('approval_id'), payload.get('approval'), stamp):
            raise ValueError('Fresh one-use computer review required; preview again (changed/expired/replayed approval)')
    used = False
    def decide(spec, current, risk, preview):
        nonlocal used
        ok = not used and spec.name == name and current == prepared
        used = True
        return ApprovalDecision(ok, scope='once', reason='Exact authenticated one-use Computer Studio review')
    reg.ctx.gate.callback = decide
    reg.ctx.gate.policy = studio_policy(state)
    return reg, name, args, detail
