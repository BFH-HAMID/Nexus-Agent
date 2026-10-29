"""Delegation commands; creation/resume never implies approval or starts a daemon."""
import json
from pathlib import Path


def command(args):
    from ..server import ServerState
    from .core import Delegations
    try:
        service = Delegations(ServerState(Path(args.workspace or '.').resolve(), quiet=True).workbench.studio)
        if args.operation == 'create':
            row = service.create(args.task, max_seconds=args.max_seconds, priority=args.priority, mission=args.mission, online=args.online, max_cost_usd=args.max_cost)
            result = service.view(row['id'])
        elif args.operation == 'tasks':
            result = service.tasks()
        else:
            result = getattr(service, {'status': 'view', 'report': 'view', 'cancel': 'cancel', 'resume': 'resume'}[args.operation])(args.identifier)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, TimeoutError):
        print(json.dumps({'error': 'Delegation refused; inspect workspace, job state, policy and budgets. No automatic replay.'}))
        return 2


def add_parser(sub):
    parser = sub.add_parser('delegate', help='durable bounded delegation on existing Jarvis jobs; fresh approval required')
    parser.add_argument('-C', '--workspace')
    actions = parser.add_subparsers(dest='operation', required=True)
    create = actions.add_parser('create')
    create.add_argument('task')
    create.add_argument('--max-seconds', type=int, default=120)
    create.add_argument('--max-cost', type=float, help='operator-priced local model USD cap; missing price blocks dispatch; not an invoice guarantee')
    create.add_argument('--priority', type=int, default=5)
    create.add_argument('--mission', choices=['dev', 'trading', 'pentest', 'study'])
    create.add_argument('--online', action='store_true')
    actions.add_parser('tasks')
    for name in ('status', 'report', 'cancel', 'resume'):
        action = actions.add_parser(name)
        action.add_argument('identifier')
    parser.set_defaults(func=command)
