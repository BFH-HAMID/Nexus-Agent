"""CLI for the guarded queue; never starts a background execution daemon implicitly."""
import json
from pathlib import Path


def command(args):
    try:
        from ..server import ServerState
        from .queue import Queue
        queue = Queue(ServerState(Path(args.workspace or '.').resolve(), quiet=True).workbench.studio)
        if args.operation in {'start', 'stop', 'pause', 'resume', 'kill', 'daemon-status', 'run', 'supervise', 'service'}:
            from .daemon import Daemon
            daemon = Daemon(queue.studio)
            if args.operation == 'run':
                return daemon.run(args.instance)
            if args.operation == 'supervise':
                receipt = daemon.request('start')
                return daemon.run(receipt['control']['instance'])
            if args.operation == 'service':
                import hashlib

                from .services import install, manifest
                data = manifest(queue.root, args.platform, args.user)
                print(json.dumps(data, indent=2))
                if args.install:
                    phrase = 'INSTALL SERVICE ' + data['filename'] + ' ' + hashlib.sha256(data['content'].encode()).hexdigest()[:16]
                    print(phrase)
                    result = install(queue.root, args.platform, args.user, confirmation=input('Exact reviewed service confirmation: '))
                    print(json.dumps(result, indent=2))
                    return 0 if result['installed'] else 2
                return 0
            result = daemon.status() if args.operation == 'daemon-status' else daemon.start() if args.operation == 'start' else daemon.request(args.operation)
        elif args.operation == 'status':
            from .daemon import Daemon
            result = {**queue.status(), 'daemon': Daemon(queue.studio).status()}
        elif args.operation in {'approvals', 'allow', 'deny'}:
            from .approvals import Approvals
            approvals = Approvals(queue.studio)
            if args.operation == 'approvals':
                result = approvals.items()
            else:
                row = queue.studio.find('jarvis-approval', args.identifier)
                print(json.dumps(row, indent=2))
                result = approvals.decide(args.identifier, args.operation == 'allow', input('Exact ALLOW ACTION confirmation: ') if args.operation == 'allow' else '')
        elif args.operation in {'inbox', 'digest', 'acknowledge', 'explain', 'budget', 'notify', 'quiet', 'dnd', 'delivery'}:
            from .inbox import Inbox
            from .runtime import Runtime
            inbox = Inbox(queue.studio)
            if args.operation in {'notify', 'quiet', 'dnd', 'delivery'}:
                result = (inbox.configure(transport=args.mode) if args.operation == 'delivery' else inbox.notify() if args.operation == 'notify' else inbox.configure(quiet=args.mode == 'on')
                          if args.operation == 'quiet' else inbox.configure(minutes=args.minutes))
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            result = ({'notices': inbox.items(), 'pending': inbox.pending()} if args.operation == 'inbox' else inbox.digest() if args.operation == 'digest'
                      else inbox.acknowledge(args.identifier) if args.operation == 'acknowledge'
                      else inbox.explain(args.identifier) if args.operation == 'explain' else {'day': Runtime(queue.studio).budget(), 'hour': Runtime(queue.studio).budget(Runtime(queue.studio).budget_id(hourly=True))})
        elif args.operation == 'event':
            from .events import Events
            result = Events(queue).publish(args.source, args.task, parent=args.parent)
        elif args.operation == 'enqueue':
            result = queue.enqueue(args.task, priority=args.priority, parent=args.parent, estimated_cost=args.estimated_cost, mission=args.mission, requires_online=args.online)
        else:
            confirmation = ''
            if args.operation == 'approve':
                print(f'APPROVE QUEUED TASK {args.identifier}')
                # Show actual queued task before asking for consent, not just its ID.
                print(json.dumps(next(row for row in queue.status()['jobs'] if row['id'] == args.identifier), indent=2))
                confirmation = input('Type exact confirmation (tool approvals remain separate): ')
            result = queue.review(args.identifier, args.operation, confirmation)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, TimeoutError, EOFError, StopIteration):
        print(json.dumps({'error': 'Queue admission/review refused; inspect policy, limits and job state. No task executed.'}))
        return 2


def add_parser(sub):
    parser = sub.add_parser('jarvis', help='Module 4 guarded persistent queue (explicit lifecycle, scoped local dispatch and reviewed actions)')
    parser.add_argument('--workspace', '-C')
    actions = parser.add_subparsers(dest='operation', required=True)
    actions.add_parser('status')
    actions.add_parser('approvals')
    for name in ('allow', 'deny'):
        action = actions.add_parser(name)
        action.add_argument('identifier')
    delivery = actions.add_parser('delivery', help='explicit native counts-only desktop notification opt-in')
    delivery.add_argument('mode', choices=['local', 'native', 'off'])
    quiet = actions.add_parser('quiet')
    quiet.add_argument('mode', choices=['on', 'off'])
    dnd = actions.add_parser('dnd')
    dnd.add_argument('minutes', type=int)
    actions.add_parser('notify', help='explicit local notification pull; honors quiet/DND')
    for name in ('inbox', 'digest', 'budget'):
        actions.add_parser(name)
    for name in ('acknowledge', 'explain'):
        action = actions.add_parser(name)
        action.add_argument('identifier')
    for name in ('start', 'stop', 'pause', 'resume', 'kill', 'daemon-status'):
        actions.add_parser(name, help='durable daemon lifecycle; dispatch requires explicit runtime opt-in')
    actions.add_parser('supervise', help='foreground lifecycle worker for an explicitly installed user service')
    service = actions.add_parser('service', help='review user-service manifest; never auto-installs')
    service.add_argument('--install', action='store_true', help='explicit current-user installation after exact manifest consent; never overwrite')
    service.add_argument('--platform', choices=['systemd', 'windows'], required=True)
    service.add_argument('--user', default='', help='Windows local/domain user; not a password')
    run = actions.add_parser('run', help='foreground lifecycle worker for CLI/user-service supervision')
    run.add_argument('--instance', required=True)
    event = actions.add_parser('event', help='admit an untrusted event as approval-pending, never grant tool execution')
    event.add_argument('source', choices=['file', 'schedule', 'build', 'todo', 'git', 'ci', 'system', 'inbox', 'idle', 'user'])
    event.add_argument('task')
    event.add_argument('--parent')
    enqueue = actions.add_parser('enqueue')
    enqueue.add_argument('task')
    enqueue.add_argument('--online', action='store_true', help='defer dispatch while runtime connectivity is offline; not external-action consent')
    enqueue.add_argument('--mission', choices=['dev', 'trading', 'pentest', 'study'])
    enqueue.add_argument('--priority', type=int, default=5)
    enqueue.add_argument('--parent')
    enqueue.add_argument('--estimated-cost', type=float, default=0)
    for name in ('approve', 'reject', 'cancel', 'retry'):
        review = actions.add_parser(name)
        review.add_argument('identifier')
    parser.set_defaults(func=command)
