"""Local extension inspection/invocation with exact fresh permission review."""
import hashlib
import json
from pathlib import Path


def command(args):
    from ..permissions import Permissions
    from ..tools import ApprovalDecision, ApprovalGate, PathGuard, ToolContext, ToolRegistry
    from .catalog import Catalog
    from .tools import register
    root = Path(args.workspace or '.').resolve()
    try:
        entries = Catalog(root).load()
        if args.operation == 'list':
            print(json.dumps(list(entries.values()), indent=2))
            return 0
        arguments = json.loads(args.arguments)
        def review(spec, data, risk, preview):
            details = {'capability': spec.name, 'manifest': entries[args.server], 'arguments': data}
            print(json.dumps(details, indent=2))
            digest = hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()[:16]
            phrase = 'RUN EXTENSION ' + spec.name + ' ' + digest
            print(phrase)
            return ApprovalDecision(input('Exact fresh confirmation (trusted code/network; not an OS sandbox): ') == phrase)
        gate = ApprovalGate(mode='ask', callback=review)
        gate.permissions = Permissions(root)
        ctx = ToolContext(workspace=root, guard=PathGuard(root), gate=gate)
        registry = ToolRegistry(ctx)
        register(ctx, registry)
        text, failed = registry.call('mcp_' + args.server + '_' + args.tool, arguments)
        print(text)
        return 2 if failed else 0
    except (ValueError, OSError, EOFError, KeyError, ImportError):
        print('Extension refused. Inspect manifests/permissions/dependencies; no raw credentials or child errors shown.')
        return 2


def add_parser(sub):
    parser = sub.add_parser('extensions', help='inspect/hot-reload manifests; fresh-reviewed bounded MCP calls')
    parser.add_argument('-C', '--workspace')
    actions = parser.add_subparsers(dest='operation', required=True)
    actions.add_parser('list')
    run = actions.add_parser('run')
    run.add_argument('server')
    run.add_argument('tool')
    run.add_argument('arguments', help='JSON object, never a shell command')
    parser.set_defaults(func=command)
