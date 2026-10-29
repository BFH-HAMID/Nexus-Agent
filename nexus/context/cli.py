"""Explicit operator context inspection/bridge, never an unapproved screenshot server."""
import hashlib
import json
from pathlib import Path


def command(args):
    from .core import Context
    try:
        service = Context(Path(args.workspace or '.'))
        if args.operation in {'status', 'collect', 'clear'}:
            result = getattr(service, args.operation)()
        elif args.operation == 'resolve':
            result = service.resolve(args.task)
        elif args.operation == 'signal':
            if len(args.data) > 4096:
                raise ValueError('Signal too large')
            result = service.record(args.kind, json.loads(args.data))
        else:
            from ..config import load_config
            from ..providers import build_providers
            from ..tools import ApprovalDecision, ApprovalGate
            from .vision import describe
            cfg = load_config(service.root)
            def approve(spec, arguments, *unused):
                phrase = 'ALLOW ' + spec.name + ' ' + hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()[:12]
                from ..redaction import mask, secrets
                print(mask(json.dumps({'operation': spec.name, 'review': arguments, 'confirmation': phrase}), secrets(service.root)))
                return ApprovalDecision(input('Exact confirmation: ') == phrase)
            result = describe(service, ApprovalGate(callback=approve), cfg, build_providers(cfg.agents), cloud_agent=args.cloud_agent)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        print(json.dumps({'error': 'Context refused: inspect explicit policy, fresh reference/consent, native dependencies and privacy blocklist. No automatic upload/replay.'}))
        return 2


def add_parser(sub):
    parser = sub.add_parser('context', help='opt-in bounded local context and separately approved screen vision')
    parser.add_argument('-C', '--workspace')
    actions = parser.add_subparsers(dest='operation', required=True)
    for name in ('status', 'collect', 'clear'):
        actions.add_parser(name)
    actions.add_parser('resolve').add_argument('task')
    signal = actions.add_parser('signal')
    signal.add_argument('kind', choices=['vscode', 'file', 'terminal'])
    signal.add_argument('data', help='bounded JSON metadata, not arbitrary screen/file contents')
    actions.add_parser('vision').add_argument('--cloud-agent', help='requires separate exact image/endpoint upload approval; never implicit')
