"""Direct GitHub task branches: bounded text tools, no local checkout or shell.

Every mutation is approved through the existing task approval channel. Git Data
ref updates are non-force, so concurrent branch advances fail rather than overwrite.
Credentials come only from GITHUB_TOKEN in environment/.env, never records.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import re
import time
from pathlib import PurePosixPath
from urllib.parse import quote

from .tools import ToolFailure
from .types import Event, EventType, Risk, ToolSpec
from .workbench import observation


class GitHubTasks:
    def __init__(self, github):
        self.gh = github
        self.studio = github.local.wb.studio
        self.pending = {}

    def api(self, endpoint, body=None, method=None):
        return self.gh.api(endpoint, body, method)

    def get(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9]{12}', ident):
            raise ValueError('Choose a Nexus GitHub task')
        return self.studio.find('github-task', 'github-task-' + ident)

    def save(self, record):
        return self.studio.put('github-task', record, 'github-task-' + record['task_id'])

    def authorize(self, record, write=False):
        self.gh.network()
        if self.gh.viewer() != record['viewer']:
            raise ValueError('GitHub account changed; task cannot continue under another identity')
        metadata = self.gh.metadata(record['repository'])
        if metadata.get('full_name', '').lower() != record['repository'].lower():
            raise ValueError('GitHub repository identity changed; restart with the correct repository')
        if write and (metadata.get('archived') or not (metadata.get('permissions') or {}).get('push')):
            raise ValueError('GitHub write permission required; archived/read-only repository refused')
        return metadata

    def ref(self, record):
        return self.api('repos/' + record['repository'] + '/git/ref/heads/' + quote(record['branch'], safe=''))['object']['sha']

    def policy(self, session, write=False):
        if session.cancel.is_set() or (write and session.read_only):
            raise ValueError('Cancelled/read-only remote task')
        role = getattr(getattr(session.agent, 'active_role', None), 'name', None) or session.role or 'general'
        permissions = self.studio.settings()['role_permissions'].get(role, {})
        if not permissions.get('network', True) or not permissions.get('read', True) or (write and not permissions.get('write', True)):
            raise ValueError('Role permissions refuse this direct GitHub task')
        self.gh.network()

    def start(self, session):
        self.policy(session, write=not session.read_only)
        repository = self.gh.record(session.options['github_repo'])['repository']
        metadata = self.gh.metadata(repository)
        base = metadata.get('default_branch')
        if not base:
            raise ValueError('Selected GitHub repository has no default branch; initialize it first')
        sha = self.gh.branch_head(repository, base)
        record = {'task_id': session.task_id, 'repository': repository, 'viewer': self.gh.viewer(),
                  'base': base, 'branch': 'nexus/task-' + session.task_id, 'head': sha,
                  'base_sha': sha, 'created': False, 'read_only': session.read_only,
                  'title': self.studio.redact_text(session.task.splitlines()[0])[:120], 'files': [],
                  'tests': 'Not run: direct GitHub mode has no local execution environment'}
        self.authorize(record, write=not session.read_only)
        if session.read_only:
            record['branch'] = base
        self.save(record)
        if not session.read_only:
            spec = ToolSpec(name='github_task_branch', description='Create an isolated task branch on GitHub',
                            input_schema={'type': 'object'}, risk=Risk.WRITE)
            decision = self.gh.local.state._approval(session)(spec, {}, Risk.WRITE,
                f"Create {repository}:{record['branch']} from {base} at {sha}. No clone or local checkout.")
            if not decision.approved or session.cancel.is_set():
                raise ValueError('GitHub task branch permission denied; nothing created')
            self.authorize(record, write=True)
            if session.cancel.is_set():
                raise ValueError('Cancelled before task branch creation')
            # This is a new, random task-specific ref. Never update/reuse an existing branch.
            self.api('repos/' + repository + '/git/refs', {'ref': 'refs/heads/' + record['branch'], 'sha': sha})
            record['created'] = True
            self.save(record)
        session.push({'type': 'github_task', 'task': record})
        return record

    @staticmethod
    def path(name):
        if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
            raise ValueError('Use a repository-relative POSIX file path')
        path = PurePosixPath(name)
        if path.is_absolute() or any(p in {'', '.', '..'} for p in name.split('/')):
            raise ValueError('Unsafe remote file path')
        for part in path.parts:
            part = part.lower()
            if part in {'.git', '.nexus', '.env', 'keys.json', '.venv', 'node_modules', '.ssh', '.aws', '.npmrc', '.netrc', 'credentials'} or (part.startswith('.env.') and part != '.env.example') or part.endswith(('.pem', '.key')):
                raise ValueError('Private/runtime paths are not available to GitHub task tools')
        return path.as_posix()

    def tree(self, record):
        commit = self.api('repos/' + record['repository'] + '/git/commits/' + record['head'])
        data = self.api('repos/' + record['repository'] + '/git/trees/' + commit['tree']['sha'] + '?recursive=1')
        if data.get('truncated'):
            raise ValueError('GitHub tree is truncated; refusing an incomplete project view')
        return data['tree']

    def read(self, record, path, tree=None):
        path = self.path(path)
        entry = next((n for n in (tree if tree is not None else self.tree(record)) if n['path'] == path), None)
        if entry is None:
            return None, None
        if entry.get('mode') not in {'100644', '100755'} or entry.get('type') != 'blob':
            raise ValueError('Only regular text files can be edited; no symlinks or submodules')
        if entry.get('size', 0) > 500_000:
            raise ValueError('Remote text file exceeds 500 KB')
        blob = self.api('repos/' + record['repository'] + '/git/blobs/' + entry['sha'])
        if blob.get('encoding') != 'base64':
            raise ValueError('Unsupported GitHub blob encoding')
        raw = base64.b64decode(''.join(blob['content'].split()), validate=True)
        if len(raw) > 500_000 or b'\x00' in raw:
            raise ValueError('Binary/oversized files are not editable in direct GitHub mode')
        return raw.decode('utf-8'), entry

    def prepare_edit(self, record, tool, args):
        path = self.path(args.get('path'))
        before, entry = self.read(record, path)
        if tool == 'write_file':
            after = args.get('content')
            if not isinstance(after, str):
                raise ValueError('File content must be text')
        elif tool == 'edit_file':
            old, new = args.get('old_text'), args.get('new_text', '')
            if before is None or not isinstance(old, str) or not old or old not in before or not isinstance(new, str):
                raise ValueError('Read the remote file first; old_text must match exactly')
            if before.count(old) > 1 and not args.get('replace_all'):
                raise ValueError('Ambiguous remote edit; add context or explicitly choose replace_all')
            after = before.replace(old, new) if args.get('replace_all') else before.replace(old, new, 1)
        else:
            if before is None:
                raise ValueError('Remote file does not exist')
            after = None
        if after is not None and (len(after.encode()) > 500_000 or '\x00' in after):
            raise ValueError('Only text changes up to 500 KB are supported')
        if after is not None:
            from .github_safety import scan_text
            scan_text(after, workspace=self.studio.wb.workspace)
        diff = ''.join(difflib.unified_diff((before or '').splitlines(True), (after or '').splitlines(True),
                                           fromfile='GitHub/' + path, tofile=record['branch'] + '/' + path))
        if len(diff.encode()) > 1_000_000:
            raise ValueError('Remote diff too large to approve')
        return {'head': record['head'], 'path': path, 'before': before, 'after': after,
                'mode': entry['mode'] if entry else '100644', 'diff': diff}

    def key(self, ident, tool, args):
        return hashlib.sha256(json.dumps([ident, tool, args], sort_keys=True).encode()).hexdigest()

    def proposal(self, session, tool, args):
        record = self.get(session.task_id)
        if tool == 'github_task_branch':
            return {'available': True, 'diff': f"Create {record['repository']}:{record['branch']} from {record['base']} at {record['head']}"}
        if tool not in {'write_file', 'edit_file', 'delete_file'}:
            return {'available': False, 'reason': 'No local filesystem or shell in direct GitHub mode'}
        self.authorize(record, write=True)
        edit = self.prepare_edit(record, tool, args)
        self.pending = {k: v for k, v in self.pending.items() if v[0] > time.time()}
        self.pending[self.key(session.task_id, tool, args)] = (time.time() + 300, edit)
        return {'available': True, 'diff': edit['diff'], 'repository': record['repository'], 'branch': record['branch'], 'head': edit['head']}

    def mutate(self, session, ctx, tool, args):
        if session.cancel.is_set() or session.read_only:
            raise ToolFailure('Cancelled/read-only GitHub task; no mutation allowed')
        record = self.get(session.task_id)
        if not record['created']:
            raise ToolFailure('Task branch has not been approved/created')
        self.policy(session, write=True)
        self.authorize(record, write=True)
        cached = self.pending.pop(self.key(session.task_id, tool, args), None)
        if cached and cached[0] < time.time():
            raise ToolFailure('Remote edit approval expired; review again')
        edit = cached[1] if cached else self.prepare_edit(record, tool, args)
        if edit['head'] != record['head'] or self.ref(record) != edit['head']:
            raise ToolFailure('Task branch advanced since review; nothing committed')
        if edit['before'] == edit['after']:
            return 'No change; no GitHub commit created'
        from .github_safety import conventional_message, scan_text
        if edit['after'] is not None:
            scan_text(edit['after'], workspace=self.studio.wb.workspace)
        message = conventional_message(f'chore(nexus): {tool} remote file\n\nTask: {session.task_id}')
        root = 'repos/' + record['repository']
        commit = self.api(root + '/git/commits/' + edit['head'])
        entry = {'path': edit['path'], 'mode': edit['mode'], 'type': 'blob', 'sha': None}
        if edit['after'] is not None:
            entry['sha'] = self.api(root + '/git/blobs', {'content': edit['after'], 'encoding': 'utf-8'})['sha']
        tree = self.api(root + '/git/trees', {'base_tree': commit['tree']['sha'], 'tree': [entry]})
        new_commit = self.api(root + '/git/commits', {'message': message,
            'tree': tree['sha'], 'parents': [edit['head']]})
        if session.cancel.is_set():
            raise ToolFailure('Task cancelled before branch update; branch unchanged')
        self.policy(session, write=True)
        self.authorize(record, write=True)
        # A raced branch update makes this non-fast-forward and GitHub rejects it.
        self.api(root + '/git/refs/heads/' + quote(record['branch'], safe=''),
                 {'sha': new_commit['sha'], 'force': False}, method='PATCH')
        record['head'] = new_commit['sha']
        record['files'] = sorted(set(record['files']) | {edit['path']})
        self.save(record)
        ctx.state.setdefault('changed_files', set()).add(edit['path'])
        session.push({'type': 'github_task', 'task': record})
        ctx.emit(Event(EventType.FILE_CHANGE, {'path': edit['path'], 'diff': edit['diff'], 'action': tool, 'call_id': ctx.state.get('active_call_id', ''),
                                              'source': 'actual GitHub task commit', 'head': record['head']}))
        return f"Committed {edit['path']} to GitHub {record['repository']}:{record['branch']} at {record['head']}. Tests not run locally."

    def attach(self, agent, session, record):
        original = agent.tools_for
        agent.remote_workspace = f"GitHub {record['repository']}:{record['branch']} (direct API; no local checkout)"
        agent.skills_enabled = False
        agent.memory.enabled = False
        agent.memory_store.enabled = False
        agent.gate.mode = 'ask'
        agent.gate.always_allow.clear()
        def tools_for(role, **kwargs):
            ctx, registry, _ = original(role, **kwargs)
            allowed = {'list_files', 'read_file', 'finish', 'request_clarification'}
            if not (session.read_only or kwargs.get('read_only')):
                allowed |= {'write_file', 'edit_file', 'delete_file'}
            registry.keep_only(allowed)
            def read_tool(args):
                self.policy(session)
                current = self.get(session.task_id)
                self.authorize(current)
                text, _ = self.read(current, args.get('path'))
                if text is None:
                    raise ToolFailure('Remote file not found')
                start = max(1, int(args.get('start_line') or 1))
                end = int(args.get('end_line') or len(text.splitlines()))
                return '\n'.join(text.splitlines()[start - 1:end])[:ctx.max_read_chars]
            def list_tool(args):
                self.policy(session)
                current = self.get(session.task_id)
                self.authorize(current)
                prefix = str(args.get('path') or '.').rstrip('/')
                prefix = '' if prefix == '.' else self.path(prefix) + '/'
                result = []
                for node in self.tree(current):
                    if not node['path'].startswith(prefix):
                        continue
                    try:
                        self.path(node['path'])
                    except ValueError:
                        continue
                    result.append(node['path'] + ('/' if node['type'] == 'tree' else ''))
                return '\n'.join(result[:min(1000, max(1, int(args.get('limit') or 300)))]) or '(empty)'
            for name, handler in [('list_files', list_tool), ('read_file', read_tool)]:
                if registry.has(name):
                    registry._tools[name].handler = handler
            def change_tool(args, name):
                try:
                    result = self.mutate(session, ctx, name, args)
                except Exception as exc:
                    ctx.state['last_remote_error'] = 'GitHub edit failed or outcome uncertain: ' + self.studio.redact_text(str(exc))[:300]
                    raise
                ctx.state.pop('last_remote_error', None)
                return result
            for name in {'write_file', 'edit_file', 'delete_file'}:
                if registry.has(name):
                    registry._tools[name].handler = lambda args, name=name: change_tool(args, name)
            return ctx, registry, registry.specs()
        agent.tools_for = tools_for

    def preview_action(self, payload):
        record = self.get(payload.get('task_id'))
        if record['repository'] != payload.get('repo') or not record['created']:
            raise ValueError('Selected repository does not match the approved task branch')
        self.authorize(record, write=True)
        if self.ref(record) != record['head']:
            raise ValueError('Task branch advanced outside Nexus; review manually')
        action = payload.get('action')
        if action == 'create-pr':
            if record.get('pr'):
                raise ValueError(f"Task already has PR #{record['pr']}; use Merge or the PR link")
            prs = self.api('repos/' + record['repository'] + '/pulls?state=all&head=' +
                           quote(record['repository'].split('/')[0] + ':' + record['branch'], safe=''))
            if prs:
                record['pr'] = prs[0]['number']
                self.save(record)
                raise ValueError(f"Task already has PR #{record['pr']}; no duplicate created. Use Merge.")
            args = {'action': action, 'repo': record['repository'], 'head': record['branch'], 'base': record['base'],
                    'title': record['title'] or 'Nexus task ' + record['task_id'],
                    'body': f"Nexus task {record['task_id']}\n\nChanges committed directly to the task branch.\n\nTests were not run locally. Review GitHub CI and the diff before merging."}
        elif action == 'merge':
            if not record.get('pr'):
                raise ValueError('Create the task PR first')
            args = {'action': action, 'repo': record['repository'], 'number': record['pr'], 'method': 'merge'}
        else:
            raise ValueError('Automatic task actions support Create PR or Merge only')
        reviewed = self.gh.preview(args)
        reviewed['data']['github_task_id'] = record['task_id']
        return reviewed

    def execute_action(self, payload):
        if payload.get('approved') is not True:
            raise ValueError('Explicit approval required')
        data = self.gh.previews.get(payload.get('preview_id'))
        if not data or not data.get('github_task_id'):
            raise ValueError('Review a task-bound action first')
        record = self.get(data['github_task_id'])
        self.authorize(record, write=True)
        if self.ref(record) != record['head']:
            raise ValueError('Task branch changed; review again')
        result = self.gh.execute({'preview_id': data['id'], 'confirm': data['confirmation']})
        if data['action'] == 'create-pr':
            match = re.search(r'https://github\.com/' + re.escape(record['repository']) + r'/pull/(\d+)\b', result['data']['output'])
            if not match:
                raise ValueError('PR command finished with uncertain URL; refresh/review before retrying')
            record['pr'] = int(match[1])
        else:
            record['merged'] = True
        self.save(record)
        result['data']['github_task'] = record
        return observation(result['data'], 'approved task-bound GitHub action; existing GitHub protections apply')
