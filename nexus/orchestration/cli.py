"""Explicit CLI entry points: indexing/planning does not execute project tools."""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..config import ConfigError, load_config
from ..providers import build_providers
from ..sessions import SessionStore
from ..studio import redact
from .index import RoleIndex, settings
from .planner import Orchestrator


def command(args):
    workspace = Path(args.workspace or '.').resolve()
    try:
        config = load_config(workspace)
        if args.action == 'feedback':
            if any(role not in config.roles for role in args.roles):
                raise ConfigError('Feedback names an unknown role')
            ident = SessionStore().record_routing_feedback(args.task, args.roles, args.decision == 'approve',
                                                           workspace=workspace, reason=args.reason)
            print(json.dumps({'feedback_id': ident, 'source': 'existing sessions.db learning store'}))
            return 0
        options = settings(workspace, args.config)
        if args.encoder:
            options['encoder'] = args.encoder
        if args.action == 'index':
            force = args.force
            while True:
                config = load_config(workspace)
                options = settings(workspace, args.config)
                if args.encoder:
                    options['encoder'] = args.encoder
                index = RoleIndex(config, workspace, options)
                print(json.dumps(index.ensure(force=force)), flush=True)
                if not args.watch:
                    return 0
                force = False
                time.sleep(5)
        index = RoleIndex(config, workspace, options)
        index.ensure()
        from ..server import ServerState
        studio = ServerState(workspace, quiet=True).workbench.studio
        policy = studio.wb.prepare({})
        studio.wb.configure(config, policy, None)  # Preserve persisted local-only model policy.
        service = Orchestrator(config, workspace, index, build_providers(config.agents))
        routed = service.route(args.task)
        if args.action == 'route' or routed['needs_clarification']:
            print(json.dumps(redact(routed), ensure_ascii=False, indent=2))
            return 2 if routed['needs_clarification'] else 0
        definition = service.plan(routed)
        normalized, _ = studio.workflows.validate(definition)
        directory = service.save(routed, normalized)
        result = {'plan_directory': str(directory), 'definition': normalized, 'executed': False}
        if args.save_workflow:
            result['workflow'] = studio.workflows.save({'definition': normalized})['data']
        print(json.dumps(redact(result), ensure_ascii=False, indent=2))
        return 0
    except (ConfigError, ValueError, OSError) as exc:
        print(json.dumps({'error': redact(str(exc)), 'executed': False}))
        return 2
    except Exception as exc:
        # Provider/embedding failures cannot silently turn into an executed plan.
        print(json.dumps({'error': type(exc).__name__, 'message': redact(str(exc))[:500], 'executed': False}))
        return 2


def add_parser(sub):
    parser = sub.add_parser('orchestrate', help='opt-in local role index, bounded role routing and reviewed DAG planning')
    parser.add_argument('--workspace', '-C')
    parser.add_argument('--config', help='role-index.yaml path')
    parser.add_argument('--encoder', choices=['ollama', 'lexical-hash'])
    actions = parser.add_subparsers(dest='action', required=True)
    index = actions.add_parser('index', help='refresh metadata/vector index (new roles are detected)')
    index.add_argument('--force', action='store_true')
    index.add_argument('--watch', action='store_true', help='poll layered role files every 5s until Ctrl+C')
    for name in ('route', 'plan'):
        action = actions.add_parser(name)
        action.add_argument('task')
        if name == 'plan':
            action.add_argument('--save-workflow', action='store_true', help='save to existing Studio; never auto-execute')
    feedback = actions.add_parser('feedback', help='record explicit human approval/rejection in existing learning storage')
    feedback.add_argument('decision', choices=['approve', 'reject'])
    feedback.add_argument('task')
    feedback.add_argument('--roles', nargs='+', required=True)
    feedback.add_argument('--reason', default='')
    parser.set_defaults(func=command)
