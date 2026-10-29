"""Explicit review-first GitHub provisioning; existing transport and Studio audit."""
from __future__ import annotations

import json
from pathlib import Path


def command(args):
    from .server import ServerState
    from .studio import redact
    attempted = False
    try:
        github = ServerState(Path(args.workspace or '.').resolve(), quiet=True).workbench.repositories.github
        if args.action == 'projects':
            print(json.dumps(github.get('projects', {}), ensure_ascii=False, indent=2))
            return 0
        project_action = args.action in {'scaffold', 'project'}
        if args.action == 'scaffold':
            payload = {'action': 'scaffold', 'name': args.name, 'stack': args.stack, 'repo': args.repo}
        elif args.action == 'project':
            payload = {'action': args.operation, 'name': args.name, 'message': args.message}
        elif args.action == 'create':
            payload = {'action': 'create-repository', 'name': args.name, 'private': args.private,
                       'organization': args.organization, 'description': args.description}
        else:
            payload = {'action': 'create-issue', 'repo': args.repo, 'title': args.title, 'body': args.body}
        preview = github.post('project-preview' if project_action else 'resource-preview', payload)['data']
        print(json.dumps({'preview': preview, 'executed': False}, ensure_ascii=False, indent=2))
        if not args.execute:
            return 0
        confirmation = input('Type the exact confirmation above (anything else cancels): ')
        attempted = True
        result = github.post('project-execute' if project_action else 'resource-execute', {'preview_id': preview['id'], 'confirm': confirmation})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, EOFError):
        # Errors may originate from remote transport or user content; withhold raw output.
        print(json.dumps({'error': 'GitHub operation refused or failed. Check GITHUB_TOKEN, permissions and reviewed inputs. Inspect GitHub before retrying uncertain mutations.', 'executed': None if attempted else False, 'outcome': 'not_confirmed'}))
        return 2
    except Exception as exc:
        print(json.dumps({'error': redact(type(exc).__name__), 'executed': None if attempted else False, 'outcome': 'not_confirmed'}))
        return 2


def add_parser(sub):
    parser = sub.add_parser('github', help='review-first repository/issue creation using GITHUB_TOKEN only')
    parser.add_argument('--workspace', '-C')
    actions = parser.add_subparsers(dest='action', required=True)
    create = actions.add_parser('create', help='preview repository creation with README initialization')
    create.add_argument('name')
    visibility = create.add_mutually_exclusive_group(required=True)
    visibility.add_argument('--private', action='store_true', dest='private')
    visibility.add_argument('--public', action='store_false', dest='private')
    create.add_argument('--organization', default='')
    create.add_argument('--description', default='')
    issue = actions.add_parser('issue', help='preview an issue in an accessible repository')
    issue.add_argument('repo')
    issue.add_argument('--title', required=True)
    issue.add_argument('--body', required=True)
    scaffold = actions.add_parser('scaffold', help='review a runnable project, tests, README, MIT license and push CI')
    scaffold.add_argument('name')
    scaffold.add_argument('--repo', required=True)
    scaffold.add_argument('--stack', choices=['python', 'node'], required=True)
    project = actions.add_parser('project', help='mapped project commit/push/PR/merge; approval only, no branch or PR-number entry')
    project.add_argument('name')
    project.add_argument('operation', choices=['commit', 'push', 'pr', 'merge'])
    project.add_argument('--message', default='')
    actions.add_parser('projects', help='list actual persisted project-folder/repo mappings')
    for action in (create, issue, scaffold, project):
        action.add_argument('--execute', action='store_true', help='after preview require an interactive exact confirmation')
    parser.set_defaults(func=command)
