"""GitHub-account repository inventory and review-first remote PR actions.

No clone, checkout, fetch, automatic push, or agent workspace switch. The local
workspace is only eligible for commit/push when its origin matches the selection.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from filelock import FileLock

from .workbench import observation

SLUG = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')


class GitHubRepositories:
    def __init__(self, local):
        self.local = local
        self.records = []
        self.loaded_at = 0
        self.catalog_viewer = None
        self.previews = {}

    @property
    def tasks(self):
        with self.local.state.lock:
            if not hasattr(self, '_tasks'):
                from .github_tasks import GitHubTasks
                self._tasks = GitHubTasks(self)
            return self._tasks

    @property
    def projects(self):
        with self.local.state.lock:
            if not hasattr(self, '_projects'):
                from .github_projects import Projects
                self._projects = Projects(self)
            return self._projects

    def command(self, args, timeout=30):
        return self.local.command(self.local.root, args, timeout=timeout)

    def api(self, endpoint, body=None, method=None):
        args = ['gh', 'api', endpoint]
        if body is None:
            return json.loads(self.command(args))
        # Private temporary request files keep bodies off process argv.
        with tempfile.TemporaryDirectory(prefix='nexus-github-') as directory:
            path = Path(directory) / 'request.json'
            path.write_text(json.dumps(body), encoding='utf-8')
            return json.loads(self.command([*args, '--method', method or 'POST', '--input', str(path)], timeout=90))

    def preview_resource(self, payload):
        from .github_safety import scan_text
        self.network()
        self.local.idle()
        action = payload.get('action')
        viewer = self.viewer()
        if action == 'create-repository':
            name, organization = payload.get('name'), payload.get('organization', '')
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', name) or name in {'.', '..'}:
                raise ValueError('Choose a simple repository name (1–100 characters)')
            if not isinstance(organization, str) or (organization and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', organization)):
                raise ValueError('Invalid organization login')
            if type(payload.get('private')) is not bool:
                raise ValueError('Explicit public/private visibility is required')
            description = payload.get('description', '')
            if not isinstance(description, str) or len(description) > 350:
                raise ValueError('Repository description is limited to 350 characters')
            body = {'name': name, 'private': payload['private'], 'description': description, 'auto_init': True}
            repository = (organization or viewer) + '/' + name
            endpoint = 'orgs/' + organization + '/repos' if organization else 'user/repos'
            confirmation = f"CREATE {'PRIVATE' if body['private'] else 'PUBLIC'} REPOSITORY {repository}"
        elif action == 'create-issue':
            repository = self.record(payload.get('repo'))['repository']
            self.check_writes(repository)
            title, text = payload.get('title'), payload.get('body')
            if not isinstance(title, str) or not title.strip() or len(title) > 250 or not isinstance(text, str) or not text.strip() or len(text) > 30000:
                raise ValueError('Bounded issue title/body required')
            body = {'title': title, 'body': text}
            endpoint = 'repos/' + repository + '/issues'
            confirmation = 'CREATE ISSUE ' + repository
        else:
            raise ValueError('Unknown GitHub resource action')
        scan_text(json.dumps(body), workspace=self.local.root)
        data = {'id': uuid.uuid4().hex, 'action': action, 'repository': repository, 'viewer': viewer,
                'endpoint': endpoint, 'body': body, 'confirmation': confirmation, 'expires': time.time() + 300}
        with self.local.state.lock:
            self.previews = {k: v for k, v in self.previews.items() if v['expires'] > time.time()}
            if len(self.previews) >= 8:
                raise ValueError('Too many outstanding GitHub previews; wait for expiry')
            self.previews[data['id']] = data
        return observation(data, 'review only; creates no repository, issue, local checkout or branch')

    def execute_resource(self, payload):
        from .github_safety import scan_text
        ident = payload.get('preview_id')
        with self.local.state.lock:
            data = self.previews.get(ident)
            if not data or data['expires'] < time.time() or data['action'] not in {'create-repository', 'create-issue'}:
                raise ValueError('Resource preview missing/expired; review again')
            if payload.get('confirm') != data['confirmation']:
                raise ValueError('Typed confirmation must exactly match the reviewed action')
            self.network()
            self.local.idle()
            if self.viewer() != data['viewer']:
                raise ValueError('GitHub account changed; review again')
            if data['action'] == 'create-issue':
                self.check_writes(data['repository'])
            scan_text(json.dumps(data['body']), workspace=self.local.root)
            self.previews.pop(ident)  # Consume before POST; never blindly retry uncertain external mutations.
            result = self.api(data['endpoint'], data['body'])
            if data['action'] == 'create-repository':
                if (result.get('full_name', '').lower() != data['repository'].lower()
                        or result.get('private') is not data['body']['private']
                        or not isinstance(result.get('default_branch'), str) or not result['default_branch']):
                    raise ValueError('Repository creation outcome uncertain; inspect GitHub before retrying')
                self.loaded_at = 0
            record = {'action': data['action'], 'repository': data['repository'],
                      'url': result.get('html_url'), 'number': result.get('number'), 'private': result.get('private'), 'executed': True}
            self.local.wb.studio.put('github-resource', record)
            return observation(record, 'approved GitHub resource creation; no local checkout or main-branch merge')

    def require_tests(self, repository, head):
        from .github_checks import require_tests
        return require_tests(self.api, repository, head)

    def network(self):
        if self.local.wb.studio.settings()['isolation']['enabled']:
            raise ValueError('GitHub transport disabled under enforced isolation')

    def viewer(self):
        self.network()
        try:
            return self.command(['gh', 'api', 'user', '--jq', '.login']).strip()
        except (ValueError, OSError) as exc:
            raise ValueError('GitHub connection needs attention on the Nexus server. Set GITHUB_TOKEN in the server environment/.env; no token belongs in this dashboard.') from exc

    def workspace_slug(self):
        try:
            return self.local.remote(self.local.root)
        except (ValueError, OSError):
            return None

    def catalog(self, refresh=False):
        self.network()
        viewer = self.viewer()
        if not refresh and viewer == self.catalog_viewer and self.loaded_at and time.time() - self.loaded_at < 60:
            return self.records
        # --paginate follows all Link pages. @json emits one bounded record per line,
        # rather than collecting huge unused API payloads or silently taking page one.
        raw = self.command(['gh', 'api', '--paginate',
            'user/repos?per_page=100&affiliation=owner,collaborator,organization_member&sort=full_name',
            '--jq', '.[] | {full_name,private,default_branch,archived,permissions} | @json'], timeout=90)
        origin = self.workspace_slug()
        records = {}
        for line in raw.splitlines():
            item = json.loads(line)
            slug = item['full_name']
            if not SLUG.fullmatch(slug) or '..' in slug.split('/'):
                raise ValueError('GitHub returned an invalid repository identifier')
            records[slug.lower()] = {'id': slug, 'name': slug, 'repository': slug,
                'private': bool(item.get('private')), 'default_branch': item.get('default_branch'),
                'archived': bool(item.get('archived')), 'can_push': bool((item.get('permissions') or {}).get('push')),
                'agent_workspace': bool(origin and origin.lower() == slug.lower()), 'source': 'github'}
        self.records = sorted(records.values(), key=lambda r: r['name'].lower())
        self.catalog_viewer = viewer
        self.loaded_at = time.time()
        return self.records

    def record(self, ident):
        if not isinstance(ident, str) or not SLUG.fullmatch(ident):
            raise ValueError('Choose a repository from the connected GitHub account')
        record = next((r for r in self.catalog() if r['id'].lower() == ident.lower()), None)
        if record is None:
            raise ValueError('Repository is not in the connected GitHub account inventory; refresh repositories')
        return record

    def metadata(self, repository):
        return json.loads(self.command(['gh', 'api', 'repos/' + repository]))

    def local_record(self, record):
        if (self.workspace_slug() or '').lower() != record['repository'].lower():
            raise ValueError('Commit/push require the server workspace origin to match the selected GitHub repository. No other local project is used.')
        ident = hashlib.sha256(str(self.local.root).encode()).hexdigest()[:20]
        return ident

    def status(self, ident):
        record = self.record(ident)
        # Revalidate identity on GitHub; do not equate cached permission flags with authority.
        metadata = self.metadata(record['repository'])
        if metadata.get('full_name', '').lower() != record['repository'].lower():
            raise ValueError('Repository was renamed or redirected; refresh the GitHub inventory')
        result = {**record, 'default_branch': metadata.get('default_branch'), 'branch': metadata.get('default_branch'), 'head': None, 'path': None,
                  'files': [], 'additions': None, 'deletions': None, 'ahead': None, 'behind': None,
                  'local_available': False, 'can_push': bool((metadata.get('permissions') or {}).get('push')),
                  'archived': bool(metadata.get('archived')), 'binary_changes': False,
                  'note': 'GitHub repository selected. No local checkout, agent workspace switch, or branch checkout.'}
        if (self.workspace_slug() or '').lower() == record['repository'].lower():
            local = self.local.status(self.local_record(record))
            result.update({key: local[key] for key in ('branch', 'head', 'path', 'files', 'additions', 'deletions',
                'ahead', 'behind', 'binary_changes')})
            result['local_available'] = True
        return result

    def get(self, path, query):
        if path == 'projects':
            return observation(self.projects.listing(), 'persisted project-folder/GitHub mappings')
        if path == 'task':
            record = self.tasks.get((query.get('task_id') or [''])[0])
            self.tasks.authorize(record)
            return observation(record, 'persisted GitHub task branch; no local checkout')
        if path == 'list':
            return observation(self.catalog(refresh=(query.get('refresh') or [''])[0] == '1'),
                               'GITHUB_TOKEN-authenticated account repositories, all accessible pages')
        if path == 'status':
            return observation(self.status((query.get('repo') or [''])[0]), 'GitHub repository; local diff only for matching workspace origin')
        record = self.record((query.get('repo') or [''])[0])
        if path == 'branches':
            raw = self.command(['gh', 'api', '--paginate', 'repos/' + record['repository'] + '/branches?per_page=100',
                                '--jq', '.[] | .name'])
            return observation(raw.splitlines(), 'actual GitHub branches; no local checkout')
        if path == 'prs':
            raw = self.command(['gh', 'api', '--paginate', 'repos/' + record['repository'] + '/pulls?state=open&per_page=100',
                '--jq', '.[] | {number,title,url:.html_url,state,headRefName:.head.ref,baseRefName:.base.ref,isDraft:.draft} | @json'])
            return observation([json.loads(line) for line in raw.splitlines()], 'all open PRs in the selected GitHub repository')
        raise ValueError('Unknown GitHub repository view')

    def branch_head(self, repository, branch):
        if not isinstance(branch, str) or not branch or branch.startswith('-') or len(branch) > 250:
            raise ValueError('Choose a valid remote branch')
        data = json.loads(self.command(['gh', 'api', 'repos/' + repository + '/branches/' + quote(branch, safe='')]))
        return data['commit']['sha']

    def check_writes(self, repository):
        self.network()
        self.local.idle()
        metadata = self.metadata(repository)
        if metadata.get('full_name', '').lower() != repository.lower():
            raise ValueError('Repository identity changed; refresh and review again')
        if metadata.get('archived') or not (metadata.get('permissions') or {}).get('push'):
            raise ValueError('Selected GitHub repository is archived or the account lacks write permission')

    def preview(self, payload):
        record = self.record(payload.get('repo'))
        action = payload.get('action')
        if action in {'commit', 'push'}:
            self.check_writes(record['repository'])
            reviewed = self.local.preview({**payload, 'repo': self.local_record(record)})
            reviewed['data'].update(github_repository=record['repository'], github_viewer=self.viewer())
            return reviewed
        if action not in {'create-pr', 'merge'}:
            raise ValueError('Unknown repository action')
        repository = record['repository']
        self.check_writes(repository)
        data = {'id': uuid.uuid4().hex, 'repo': record['id'], 'repository': repository, 'action': action,
                'viewer': self.viewer(), 'expires': time.time() + 300}
        if action == 'create-pr':
            base, head = payload.get('base'), payload.get('head')
            if base == head:
                raise ValueError('Head and base must be different remote branches')
            title, body = str(payload.get('title', '')).strip(), str(payload.get('body', '')).strip()
            if not title or not body or len(title) > 250 or len(body) > 30000 or '\x00' in title + body:
                raise ValueError('Reviewed PR title/body required')
            base_sha, head_sha = self.branch_head(repository, base), self.branch_head(repository, head)
            if base_sha == head_sha:
                raise ValueError('Head and base contain the same commit; no PR changes to review')
            data['test_evidence'] = self.require_tests(repository, head_sha)
            diff = self.command(['gh', 'api', 'repos/' + repository + '/compare/' + base_sha + '...' + head_sha,
                                 '-H', 'Accept: application/vnd.github.diff'])
            data.update(base=base, head=head, base_sha=base_sha, head_sha=head_sha, title=title, body=body, diff=diff,
                        confirmation=f'CREATE PR {repository} {head}', note='Existing remote branches only. No commit, clone, checkout or implicit push.')
        else:
            pr = self.local.pr(self.local.root, repository, payload.get('number'))
            self.local.merge_ready(pr, pr['headRefName'], pr['headRefOid'])
            data['test_evidence'] = self.require_tests(repository, pr['headRefOid'])
            method = payload.get('method', 'merge')
            if method not in {'merge', 'squash', 'rebase'}:
                raise ValueError('Choose merge, squash or rebase')
            diff = self.command(['gh', 'pr', 'diff', str(pr['number']), '--repo', repository])
            # The PR can advance while its diff is fetched. Never review mismatched metadata/diff.
            current = self.local.pr(self.local.root, repository, pr['number'])
            if current != pr:
                raise ValueError('PR changed while reading its diff; review again')
            data.update(pr=pr, diff=diff, method=method, confirmation=f'MERGE #{pr["number"]} {repository}',
                        note='Actual remote PR and diff. No admin/auto merge, branch deletion, or local workspace mutation.')
        if len(json.dumps(data)) > 2_000_000:
            raise ValueError('Review exceeds 2 MB; narrow the change')
        with self.local.state.lock:
            self.previews = {key: value for key, value in self.previews.items() if value['expires'] > time.time()}
            if len(self.previews) >= 8:
                self.previews.pop(min(self.previews, key=lambda key: self.previews[key]['expires']))
            self.previews[data['id']] = data
        return observation(data, 'exact remote repository, branches/commits, diff and one-use confirmation')

    def execute(self, payload):
        ident = str(payload.get('preview_id', ''))
        data = self.previews.get(ident)
        if not data or data['expires'] < time.time():
            raise ValueError('Preview expired/missing; review again')
        if payload.get('confirm') != data['confirmation']:
            raise ValueError('Typed confirmation must exactly match the reviewed action')
        lock = self.local.wb.path.parent / ('github-' + hashlib.sha256(data['repository'].lower().encode()).hexdigest() + '.lock')
        with self.local.state.lock, FileLock(str(lock), timeout=5):
            if ident not in self.previews or data['expires'] < time.time():
                raise ValueError('Preview consumed/expired; review again')
            self.check_writes(data['repository'])
            if self.viewer() != data['viewer']:
                raise ValueError('Connected GitHub account changed; review again')
            repository = data['repository']
            if data['action'] == 'create-pr':
                if self.branch_head(repository, data['base']) != data['base_sha'] or self.branch_head(repository, data['head']) != data['head_sha']:
                    raise ValueError('Remote branches advanced since review; review again')
                self.require_tests(repository, data['head_sha'])
                command = ['gh', 'pr', 'create', '--repo', repository, '--base', data['base'], '--head', data['head'],
                           '--title', data['title'], '--body', data['body']]
            else:
                pr = data['pr']
                current = self.local.pr(self.local.root, repository, pr['number'])
                self.local.merge_ready(current, pr['headRefName'], pr['headRefOid'])
                self.require_tests(repository, pr['headRefOid'])
                if current['baseRefOid'] != pr['baseRefOid'] or current['baseRefName'] != pr['baseRefName']:
                    raise ValueError('PR base changed; review again')
                command = ['gh', 'pr', 'merge', str(pr['number']), '--repo', repository, '--' + data['method'],
                           '--match-head-commit', pr['headRefOid']]
            self.previews.pop(ident)  # No duplicate retries after uncertain remote outcomes.
            result = self.command(command, timeout=120)
            return observation({'action': data['action'], 'repository': repository, 'exit_code': 0, 'output': result},
                               'explicit confirmed GitHub command; no local workspace switch')

    def post(self, path, payload):
        self.network()
        if path == 'project-preview':
            return self.projects.preview(payload)
        if path == 'project-execute':
            return self.projects.execute(payload)
        if path == 'resource-preview':
            return self.preview_resource(payload)
        if path == 'resource-execute':
            return self.execute_resource(payload)
        if path == 'auto-preview':
            return self.tasks.preview_action(payload)
        if path == 'auto-execute':
            return self.tasks.execute_action(payload)
        if path == 'preview':
            return self.preview(payload)
        if path == 'execute':
            if payload.get('preview_id') in self.local.previews:
                data = self.local.previews[payload['preview_id']]
                # Local previews only originate from a matching GitHub selection.
                record = self.record(data.get('github_repository'))
                if self.local_record(record) != data['repo']:
                    raise ValueError('Workspace origin changed; review again')
                self.check_writes(record['repository'])
                if self.viewer() != data.get('github_viewer'):
                    raise ValueError('Connected GitHub account changed; review again')
                return self.local.execute(payload)
            return self.execute(payload)
        raise ValueError('Unknown repository action')
