"""Adapters to the ORIGINAL ToolRegistry/ApprovalGate, with one-use exact review leases."""
from __future__ import annotations

import copy
import json
import re
import secrets
import time

from . import policy
from .service import SecurityService
from .store import clean, sha

PREPARED = frozenset({'security_response', 'security_scan', 'security_analyze'})
FEATURES = {'security_response': 'responses_enabled', 'security_scan': 'scan_enabled', 'security_analyze': 'analysis_enabled'}


def recheck(ctx, feature):
    from ..computer_control import EmergencyStop
    cfg = policy.require(ctx.workspace, feature)
    review = ctx.state.get('security_active_review')
    if (not review or policy.digest(cfg) != review['policy_sha256'] or time.monotonic() >= review['deadline']
            or EmergencyStop().stopped() or ctx.cancel_event is not None and ctx.cancel_event.is_set()):
        raise ValueError('Security review expired, cancelled or policy changed; request a new fresh review')
    return cfg


def prepare(ctx, name, args):
    if not isinstance(args, dict) or len(json.dumps(args).encode()) > 32_000 or '_review' in args:
        raise ValueError('Fresh literal security arguments required; caller-supplied reviews are refused')
    cfg = policy.require(ctx.workspace, FEATURES[name])
    args = copy.deepcopy(args)
    nonce = secrets.token_hex(16)
    if name == 'security_scan':
        from .scope import allowed
        if set(args) - {'target', 'ports'} or 'target' not in args:
            raise ValueError('Active checks accept one target and an explicit bounded TCP port list only')
        cfg, target, ports = allowed(ctx.workspace, args['target'], args.get('ports'))
        args = {'target': target, 'ports': ports}
        plan = {**args, 'method': 'TCP connect only', 'timeout_per_port_seconds': .3,
                'max_seconds': 12, 'no_payloads': True, 'risk_class': 'DANGEROUS',
                'warning': 'Active network traffic on explicitly authorized systems only. No scripts/exploits, DNS, banners or retries.'}
    elif name == 'security_response':
        from .responses import plan as response_plan
        plan = response_plan(ctx.workspace, args, nonce)
    else:
        from .analysis import prepare as analysis_prepare
        plan = analysis_prepare(ctx.workspace, args)[0]
    prepared = {**args, '_review': {'nonce': nonce, 'expires_at': time.time() + 60,
                 'policy_sha256': policy.digest(cfg), 'plan': plan}}
    if clean(ctx.workspace, prepared) != prepared:
        raise ValueError('Potential credential in security review; refused before display or logging')
    pending = ctx.state.setdefault('security_pending_reviews', {})
    for key, value in list(pending.items()):
        if value['deadline'] <= time.monotonic():
            pending.pop(key)
    if len(pending) >= 32:
        raise ValueError('Pending security review quota exceeded')
    pending[nonce] = {'sha256': sha(prepared), 'deadline': time.monotonic() + 60,
                      'policy_sha256': policy.digest(cfg), 'tool': name}
    return prepared


def perform(ctx, name, prepared):
    review = prepared.get('_review')
    if not isinstance(review, dict):
        raise ValueError('An exact current security review is required')
    held = ctx.state.get('security_pending_reviews', {}).pop(review.get('nonce'), None)
    if not held or held['tool'] != name or held['sha256'] != sha(prepared):
        raise ValueError('Security review already consumed or its exact arguments changed')
    ctx.state['security_active_review'] = held
    try:
        cfg = recheck(ctx, FEATURES[name])
        args = {key: value for key, value in prepared.items() if key != '_review'}
        service = SecurityService(ctx.workspace)
        if name == 'security_response':
            from .responses import plan
            proposal = plan(ctx.workspace, args, review['nonce'])
            if proposal != review['plan']:
                raise ValueError('Response evidence/preimage changed after approval; no proposal recorded')
            recheck(ctx, FEATURES[name])
            service.record('reviewed_response_proposal', review['nonce'], proposal,
                           summary='Freshly reviewed response proposal only; NO host command executed.', severity='high')
            return json.dumps({**proposal, 'reviewed': True, 'response_executed': False}, ensure_ascii=False)
        if name == 'security_scan':
            from .scope import scan
            def checked_scan():
                recheck(ctx, FEATURES[name])
                return scan(ctx.workspace, args, cfg,
                            cancelled=lambda: bool(ctx.cancel_event is not None and ctx.cancel_event.is_set()))
            result = service.observe_action(cfg, review['nonce'], review['plan'], 'active_test', checked_scan)
            return untrusted(ctx, result)
        from .analysis import report
        result = service.observe_action(cfg, review['nonce'], review['plan'], 'defensive_analysis',
                                        lambda: report(ctx, args, review['plan']))
        return untrusted(ctx, result)
    finally:
        ctx.state.pop('security_active_review', None)


def untrusted(ctx, result):
    from ..untrusted import wrap
    text = result if isinstance(result, str) else json.dumps(clean(ctx.workspace, result), ensure_ascii=False)
    output, flags = wrap(text, source='security:observations', kind='defensive-security')
    ctx.mark_tainted('security:observations', flags)
    return output


def passive(ctx, name, args):
    service = SecurityService(ctx.workspace)
    if name == 'security_report':
        if (set(args) - {'format', 'incident'} or args.get('format', 'json') not in {'json', 'markdown'}
                or 'incident' in args and (not isinstance(args['incident'], str) or not re.fullmatch(r'[a-f0-9]{24}', args['incident']))):
            raise ValueError('Use a JSON/Markdown export and optional recorded incident ID')
        return untrusted(ctx, service.report(**args))
    if args:
        raise ValueError('Passive security status/monitor accepts no target or path overrides')
    policy.require(ctx.workspace)
    return untrusted(ctx, service.status() if name == 'security_status' else service.tick())


def register(reg, ctx):
    if ctx.gate.permissions is None:
        return
    try:
        cfg = policy.load(ctx.workspace)
    except (ValueError, OSError, RecursionError, TypeError):
        return  # The shared gate still refuses unsafe execution under invalid policy.
    if not cfg['enabled']:
        return
    from ..types import Risk
    specs = {
        'security_status': ('Read current defensive security status; no collection or network IO.', {}, []),
        'security_report': ('Export existing untrusted incident timeline as JSON/Markdown; no mutation.',
            {'format': {'enum': ['json', 'markdown']}, 'incident': {'type': 'string'}}, []),
        'security_monitor': ('One passive read-only collection tick for operator-selected local sources; never responds automatically.', {}, []),
        'security_scan': ('One IP-literal, allowlisted bounded TCP-connect test. DANGEROUS/fresh review; never exploits or raw scanners.',
            {'target': {'type': 'string'}, 'ports': {'type': 'array', 'items': {'type': 'integer'}}}, ['target']),
        'security_response': ('DANGEROUS review of an exact command AND rollback PROPOSAL; NEVER executes host response commands.',
            {'action': {'enum': ['block_ip', 'disable_account', 'rotate_key']}, 'incident': {'type': 'string'},
             'backend': {'enum': ['ufw', 'iptables']}, 'account': {'type': 'string'}, 'key': {'type': 'string'}}, ['action', 'incident']),
        'security_analyze': ('Freshly reviewed tool-free defensive role report for an incident. No learning/fallback or execution.',
            {'incident': {'type': 'string'}, 'role': {'type': 'string'}}, ['incident', 'role']),
    }
    for name, (description, properties, required) in specs.items():
        feature = FEATURES.get(name, 'monitoring_enabled' if name == 'security_monitor' else 'enabled')
        if not cfg[feature]:
            continue
        prepared = name in PREPARED
        reg.register(name, description, {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False},
            lambda args, name=name: perform(ctx, name, args) if name in PREPARED else passive(ctx, name, args),
            risk=Risk.DANGEROUS if prepared else Risk.READ, read_only=not prepared,
            prepare=(lambda args, name=name: prepare(ctx, name, args)) if prepared else None)
