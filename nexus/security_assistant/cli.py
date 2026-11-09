"""Explicit foreground security monitor and original-gate reviewed actions."""
from __future__ import annotations

import json
import threading
from pathlib import Path


def command(args):
    from . import policy
    from .service import SecurityService
    try:
        root = Path(args.workspace or '.').resolve()
        service = SecurityService(root)
        if args.operation == 'status':
            print(json.dumps(service.status(), indent=2))
            return 0
        if args.operation == 'export':
            print(service.report(format=args.format, incident=args.incident))
            return 0
        if args.operation == 'roles':
            from ..config import load_config
            from .analysis import DEFENSIVE_ROLES
            cfg = policy.require(root)
            config = load_config(root)
            rows = [{'role': name, 'available': name in config.roles}
                    for name in cfg['analysis_roles'] if name in DEFENSIVE_ROLES]
            print(json.dumps({'roles': rows, 'analysis_enabled': cfg['analysis_enabled'], 'tools_granted': 0}, indent=2))
            return 0
        from ..computer_cli import operator_gate
        from ..tools import PathGuard, ToolContext, build_tools
        gate = operator_gate(root)
        if getattr(args, 'yes', False):
            gate.mode = 'auto'  # DANGEROUS still requires the exact interactive callback EVERY time.
        ctx = ToolContext(root, PathGuard(root), gate)
        registry = build_tools(ctx)
        if args.operation in {'monitor', 'watch'}:
            if args.operation == 'watch':
                policy.require(root, 'watch_enabled')
            wake = threading.Event()
            while True:
                if args.operation == 'watch':
                    policy.require(root, 'watch_enabled')
                output, error = registry.call('security_monitor', {})
                print(output, flush=True)
                if error or args.operation != 'watch':
                    return 2 if error else 0
                cfg = policy.require(root, 'watch_enabled')
                wake.wait(cfg['poll_seconds'])
        if args.operation == 'scan':
            from .scope import allowed
            selected = [int(p) for p in args.ports.split(',')] if args.ports else None
            _, target, ports = allowed(root, args.target, selected)
            name, payload = 'security_scan', {'target': target, 'ports': ports}
        elif args.operation == 'respond':
            name, payload = 'security_response', {'incident': args.incident, 'action': args.action}
            payload[{'block_ip': 'backend', 'disable_account': 'account', 'rotate_key': 'key'}[args.action]] = (
                args.backend if args.action == 'block_ip' else args.account if args.action == 'disable_account' else args.key)
        else:
            name, payload = 'security_analyze', {'incident': args.incident, 'role': args.role}
        if not registry.has(name):
            raise ValueError('Security capability is OFF in operator security.yaml')
        if args.preview:
            prepared = registry._tools[name].prepare(payload)
            print(json.dumps({'tool': name, 'review': prepared, 'approved': False, 'executed': False}, ensure_ascii=False, indent=2))
            return 0
        output, error = registry.call(name, payload)
        print(output)
        return 2 if error else 0
    except KeyboardInterrupt:
        print('Security foreground operation stopped; no automatic replay.')
        return 0
    except (ValueError, OSError, TimeoutError, EOFError, TypeError, KeyError, RecursionError) as exc:
        from ..redaction import redact_text
        message = str(exc) if type(exc) is ValueError else 'Security operation unavailable; inspect policy, dependencies and private state.'
        print(json.dumps({'error': redact_text(message, Path(args.workspace or '.').resolve()), 'response_executed': False}))
        return 2


def add_parser(sub):
    parser = sub.add_parser('security', help='OFF-by-default defensive monitoring, scoped TCP checks and response PROPOSALS only')
    parser.add_argument('-C', '--workspace')
    actions = parser.add_subparsers(dest='operation', required=True)
    for name in ('status', 'monitor', 'watch', 'roles'):
        actions.add_parser(name)
    export = actions.add_parser('export', help='write verified JSON/Markdown to stdout; operator chooses any redirection/file')
    export.add_argument('--format', choices=['json', 'markdown'], default='markdown')
    export.add_argument('--incident')
    scan = actions.add_parser('scan', help='one allowlisted IP, TCP connect only, fresh approval')
    scan.add_argument('target')
    scan.add_argument('--ports', help='comma-separated subset of operator scan_ports')
    respond = actions.add_parser('respond', help='review/record a proposal with rollback; NEVER execute it')
    respond.add_argument('incident')
    respond.add_argument('action', choices=['block_ip', 'disable_account', 'rotate_key'])
    respond.add_argument('--backend', choices=['ufw', 'iptables'], default='ufw')
    respond.add_argument('--account')
    respond.add_argument('--key')
    analyze = actions.add_parser('analyze', help='one reviewed tool-free report using an existing defensive role')
    analyze.add_argument('incident')
    analyze.add_argument('--role', default='blue-team')
    for action in (scan, respond, analyze):
        action.add_argument('--preview', action='store_true', help='show the unapproved exact review; no scan/model call or recorded approval')
        action.add_argument('--yes', action='store_true', help='compatibility flag; NEVER bypasses fresh dangerous-action confirmation')
    parser.set_defaults(func=command)
