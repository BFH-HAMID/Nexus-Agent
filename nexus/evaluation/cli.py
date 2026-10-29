"""Weekly advisory plus explicit inspect, assessment and freshly authorized file re-execution."""
import json
import sqlite3
from pathlib import Path


def command(args):
    from ..config import load_config
    from ..redaction import mask, secrets
    from ..sessions import SessionStore
    try:
        root = Path(args.workspace or '.').resolve()
        store = SessionStore()
        operation = getattr(args, 'operation', 'report') or 'report'
        if operation == 'report':
            report = store.evaluation_report(root, days=args.days, roles=load_config(root).roles.roles)
        elif operation == 'feedback':
            phrase = 'ASSESS ' + args.identifier + ' ' + args.verdict
            if input('Exact operator assessment confirmation: ' + phrase + ': ') != phrase:
                raise ValueError('Assessment not confirmed')
            report = store.assess(args.identifier, root, args.verdict == 'accept', args.reason)
        else:
            from .replay import inspect, reexecute, why
            if operation == 'why':
                report = why(store, root, args.identifier)
            elif operation == 'replay' and args.execute:
                import hashlib

                from ..tools import ApprovalDecision
                def callback(spec, arguments, *unused):
                    phrase = 'REEXECUTE ' + spec.name + ' ' + hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()[:12]
                    print(mask(json.dumps({'review': arguments, 'confirmation': phrase}), secrets(root)))
                    return ApprovalDecision(input('Exact confirmation: ') == phrase)
                report = reexecute(store, root, args.identifier, args.step, callback)
            else:
                report = inspect(store, root, args.identifier, args.step)

        print(mask(json.dumps(report, ensure_ascii=False, indent=2), secrets(root)))
        return 0
    except (ValueError, OSError, sqlite3.DatabaseError, KeyError, TypeError, EOFError):
        print(json.dumps({'error': 'Evaluation unavailable; no invented outcomes, replay or role changes.'}))
        return 2


def add_parser(sub):
    parser = sub.add_parser('evaluate', help='read-only weekly report from existing sessions; agent claims are not test proof')
    parser.add_argument('-C', '--workspace')
    parser.add_argument('--days', type=int, default=7)
    parser.set_defaults(func=command)


def add_trace_parser(sub):
    parser = sub.add_parser('trace', help='redacted inspect/why/step replay; effects OFF unless explicitly re-reviewed')
    parser.add_argument('-C', '--workspace')
    parser.set_defaults(days=7)
    actions = parser.add_subparsers(dest='operation', required=True)
    for name in ('inspect', 'why', 'replay', 'feedback'):
        action = actions.add_parser(name)
        action.add_argument('identifier')
        action.add_argument('--step', type=int)
        action.set_defaults(func=command)
        if name == 'replay':
            action.add_argument('--execute', action='store_true', help='fresh approval; only selected workspace file tools, never external/uncertain effects')
        if name == 'feedback':
            action.add_argument('--verdict', choices=['accept', 'reject'], required=True)
            action.add_argument('--reason', required=True)
