"""Operator-only computer recovery commands; no model-exposed undo bypass."""
from __future__ import annotations

import json
from pathlib import Path


def command(args):
    from .permissions import Permissions
    from .snapshots import Snapshots
    from .tools import ApprovalDecision, ApprovalGate, ToolFailure
    try:
        workspace = Path(args.workspace or '.').resolve()
        if args.operation in {'kill', 'resume-tools', 'control-status', 'hotkey'}:
            from .computer_control import EmergencyStop, hotkey
            if args.operation == 'hotkey':
                hotkey()
                return 0
            control = EmergencyStop()
            if args.operation != 'control-status':
                print(json.dumps(control.set(args.operation == 'kill')))
            else:
                print(json.dumps({'stopped': control.stopped()}))
            return 0
        if args.operation == 'tool':
            import hashlib

            from .github_safety import scan_text
            from .tools import PathGuard, ToolContext, build_tools
            if len(args.arguments) > 64000:
                raise ValueError('Tool arguments exceed review budget')
            payload = json.loads(args.arguments)
            if not isinstance(payload, dict):
                raise ValueError('JSON object required')
            scan_text(json.dumps(payload), workspace=workspace)
            confirmation = 'RUN ' + args.name + ' ' + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]
            gate = ApprovalGate(mode='ask', callback=lambda *a: ApprovalDecision(input('Exact confirmation: ') == confirmation))
            gate.permissions = Permissions(workspace)
            registry = build_tools(ToolContext(workspace, PathGuard(workspace), gate))
            if not registry.has(args.name):
                raise ValueError('Tool disabled or unavailable')
            spec = next(s for s in registry.specs() if s.name == args.name)
            rule = gate.permissions.evaluate(spec, payload)
            print(json.dumps({'tool': args.name, 'arguments': payload, 'policy': rule, 'confirmation': confirmation,
                              'note': 'Preview only; host commands are not sandboxed unless docker backend is configured'}, indent=2))
            if not args.execute:
                return 0
            output, error = registry.call(args.name, payload)
            print(output)
            return 2 if error else 0
        store = Snapshots(workspace)
        if args.operation == 'snapshots':
            print(json.dumps({'snapshots': store.listing()}, indent=2))
            return 0
        identifier = args.identifier
        if identifier == 'latest':
            rows = [r for r in store.listing() if r['status'] == 'applied']
            if not rows:
                raise ValueError('No applied snapshots')
            identifier = rows[0]['id']
        preview = store.preview(identifier)
        print(json.dumps({'preview': preview, 'executed': False}, indent=2))
        if not args.execute:
            return 0
        def approve(*unused):
            return ApprovalDecision(input('Type exact UNDO confirmation above (anything else cancels): ') == preview['confirmation'])
        gate = ApprovalGate(mode='ask', callback=approve)
        gate.permissions = Permissions(workspace)
        result = store.undo(preview, gate)
        print(json.dumps({'restored': result, 'executed': True}, indent=2))
        return 0
    except (ValueError, OSError, EOFError, TimeoutError, ToolFailure, KeyError, TypeError, RuntimeError):
        print(json.dumps({'error': 'Recovery refused or interrupted. Check permissions, snapshot status and current file; inspect uncertain outcomes manually. No reset/clean is performed.'}))
        return 2


def add_parser(sub):
    parser = sub.add_parser('agent', help='permissioned local computer recovery')
    parser.add_argument('--workspace', '-C')
    actions = parser.add_subparsers(dest='operation', required=True)
    for name in ('kill', 'resume-tools', 'control-status', 'hotkey'):
        actions.add_parser(name, help='user-scoped emergency tool/process control')
    tool = actions.add_parser('tool', help='preview a permissioned computer/file/terminal tool; no execution without --execute')
    tool.add_argument('name', choices=['office_file', 'vscode_open', 'browser_action', 'run_command', 'list_files', 'read_file', 'grep', 'write_file', 'edit_file', 'delete_file'])
    tool.add_argument('arguments', help='bounded JSON object, not shell interpolation')
    tool.add_argument('--execute', action='store_true')
    actions.add_parser('snapshots', help='list bounded typed-file recovery records (no file bodies)')
    undo = actions.add_parser('undo', help='preview restoration; never overwrite later user edits')
    undo.add_argument('identifier', nargs='?', default='latest')
    undo.add_argument('--execute', action='store_true', help='require exact fresh interactive confirmation')
    parser.set_defaults(func=command)
