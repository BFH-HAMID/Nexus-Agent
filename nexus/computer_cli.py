"""Operator-only computer recovery commands; no model-exposed undo bypass."""
from __future__ import annotations

import json
from pathlib import Path


def operator_gate(workspace):
    """Every callback shows the CURRENT action/target, including nested macro steps."""
    import hashlib

    from .redaction import mask, secrets
    from .tools import ApprovalDecision, ApprovalGate
    def review(spec, args, risk, preview):
        phrase = 'RUN ' + spec.name + ' ' + hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:12]
        print(mask(json.dumps({'tool': spec.name, 'arguments': args, 'risk': risk.value,
                               'confirmation': phrase}, ensure_ascii=False, indent=2), secrets(workspace)))
        return ApprovalDecision(input('Exact fresh confirmation (anything else cancels): ') == phrase, scope='once')
    from .permissions import Permissions
    gate = ApprovalGate(callback=review)
    gate.permissions = Permissions(workspace)
    return gate


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
        if args.operation == 'workflow':
            from .computer_workflows import Recorder
            recorder = Recorder(workspace, gate=operator_gate(workspace))
            if args.workflow_operation == 'run':
                args.operation, args.name = 'tool', 'workflow_run'
                args.arguments = json.dumps({'name': args.macro, 'parameters': json.loads(args.parameters)})
            else:
                result = recorder.start(args.macro) if args.workflow_operation == 'start' else recorder.stop() if args.workflow_operation == 'stop' else recorder.read()
                print(json.dumps(result, indent=2))
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
            gate = operator_gate(workspace)
            registry = build_tools(ToolContext(workspace, PathGuard(workspace), gate))
            if not registry.has(args.name):
                raise ValueError('Tool disabled or unavailable')
            spec = next(s for s in registry.specs() if s.name == args.name)
            prepared = registry._tools[args.name].prepare(payload) if registry._tools[args.name].prepare else payload
            rule = gate.permissions.evaluate(spec, prepared)
            confirmation = 'RUN ' + args.name + ' ' + hashlib.sha256(json.dumps(prepared, sort_keys=True).encode()).hexdigest()[:12]
            print(json.dumps({'tool': args.name, 'arguments': prepared, 'policy': rule, 'confirmation': confirmation,
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
    tool.add_argument('name', choices=['office_file', 'vscode_open', 'browser_action', 'run_command', 'list_files', 'read_file', 'grep', 'write_file', 'edit_file', 'delete_file', 'desktop_action', 'file_action', 'workflow_run', 'home_action'])
    tool.add_argument('arguments', help='bounded JSON object, not shell interpolation')
    tool.add_argument('--execute', action='store_true')
    workflow = actions.add_parser('workflow', help='record unreviewed drafts or replay operator-pinned macros')
    operations = workflow.add_subparsers(dest='workflow_operation', required=True)
    record = operations.add_parser('start')
    record.add_argument('macro', help='lowercase name; records successful approved semantic calls only')
    operations.add_parser('stop')
    operations.add_parser('show')
    run = operations.add_parser('run')
    run.add_argument('macro')
    run.add_argument('--parameters', default='{}', help='JSON data; never shell interpolation')
    run.add_argument('--execute', action='store_true')
    actions.add_parser('snapshots', help='list bounded typed-file recovery records (no file bodies)')
    undo = actions.add_parser('undo', help='preview restoration; never overwrite later user edits')
    undo.add_argument('identifier', nargs='?', default='latest')
    undo.add_argument('--execute', action='store_true', help='require exact fresh interactive confirmation')
    parser.set_defaults(func=command)
