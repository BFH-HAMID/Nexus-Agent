"""Review-first repository toolbar. No checkout, force push, auto-merge or implicit push.

The public selector is backed by the authenticated GitHub account inventory.
Local helpers operate only on the matching server workspace.
Selecting one changes only Git controls, never an agent's workspace/branch.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from filelock import FileLock

from .workbench import observation, safe_path


class Repositories:
    def __init__(self, workbench):
        self.wb = workbench
        self.state = workbench.state
        self.root = workbench.workspace.resolve()
        self.previews = {}
        self.initial_branch = {}
        try:
            self.initial_branch[hashlib.sha256(str(self.root).encode()).hexdigest()[:20]] = self.branch(self.root)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            pass

    def command(self, root, argv, timeout=25):
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never')
        if argv[0] == 'git':
            argv = ['git', '-c', 'core.fsmonitor=false', *argv[1:]]
        result = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise ValueError((result.stderr or result.stdout or 'Operation failed')[-3000:])
        if len(result.stdout) > 2_000_000:
            raise ValueError('Output too large; narrow the selected changes')
        return result.stdout

    def discover(self):
        candidates = [self.root]
        found = {}
        for path in candidates:
            path = path.resolve()
            if path in found or not (path / '.git').exists():
                continue
            try:
                top = Path(self.command(path, ['git', 'rev-parse', '--show-toplevel']).strip()).resolve()
                if top != path:
                    continue
                branch = self.command(path, ['git', 'branch', '--show-current']).strip()
            except (ValueError, OSError, subprocess.TimeoutExpired):
                continue
            ident = hashlib.sha256(str(path).encode()).hexdigest()[:20]
            self.initial_branch.setdefault(ident, branch)
            found[path] = {'id': ident, 'name': path.name, 'path': str(path), 'branch': branch,
                           'agent_workspace': path == self.root}
        return list(found.values())

    def resolve(self, ident):
        records = self.discover()
        record = next((r for r in records if r['id'] == ident), None)
        if record is None:
            raise ValueError('Choose an existing repository from the server-local selector')
        return Path(record['path']), record

    def branch(self, root):
        return self.command(root, ['git', 'branch', '--show-current']).strip()

    def paths(self, root):
        names = set(self.command(root, ['git', 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--name-only', '-z', 'HEAD']).split('\0'))
        names.update(self.command(root, ['git', 'ls-files', '--others', '--exclude-standard', '-z']).split('\0'))
        result = []
        for name in sorted(names - {''}):
            try:
                safe_path(root, name)
                result.append(name)
            except ValueError:
                pass
        return result

    def fingerprint(self, root):
        digest = hashlib.sha256()
        for args in (['git', 'rev-parse', 'HEAD'], ['git', 'status', '--porcelain=v1', '-z', '--ignore-submodules=all'],
                     ['git', 'diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD'], ['git', 'ls-files', '--stage', '-z']):
            digest.update(self.command(root, args).encode())
        total = 0
        for name in self.paths(root):
            path = safe_path(root, name)
            if path.is_file():
                size = path.stat().st_size
                total += size
                if size > 2_000_000 or total > 20_000_000:
                    raise ValueError('Changes too large for a safe browser review; use your terminal')
                digest.update(name.encode())
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()

    def status(self, ident):
        root, record = self.resolve(ident)
        branch = self.branch(root)
        head = self.command(root, ['git', 'rev-parse', 'HEAD']).strip()
        files = self.paths(root)
        plus = minus = 0
        binary = False
        for line in self.command(root, ['git', 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--numstat', 'HEAD']).splitlines():
            fields = line.split('\t', 2)
            if len(fields) == 3:
                if fields[0].isdigit() and fields[1].isdigit():
                    plus += int(fields[0])
                    minus += int(fields[1])
                else:
                    binary = True
        untracked = self.command(root, ['git', 'ls-files', '--others', '--exclude-standard', '-z']).split('\0')
        for name in untracked:
            if name not in files:
                continue
            path = safe_path(root, name)
            if path.is_file() and path.stat().st_size <= 500000:
                try:
                    plus += len(path.read_text(encoding='utf-8').splitlines())
                except UnicodeError:
                    binary = True
        try:
            ahead, behind = map(int, self.command(root, ['git', 'rev-list', '--left-right', '--count', 'HEAD...@{upstream}']).split())
        except ValueError:
            ahead = behind = None
        return {**record, 'branch': branch, 'head': head, 'files': files, 'additions': plus, 'deletions': minus,
                'binary_changes': binary, 'ahead': ahead, 'behind': behind,
                'note': 'Local refs only; no background fetch. Private/runtime paths excluded from selectable files. Selecting a repo does not switch the agent workspace.'}

    def remote(self, root):
        url = self.command(root, ['git', 'remote', 'get-url', 'origin']).strip()
        match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?', url)
        if not match or '..' in match[1].split('/'):
            raise ValueError('Remote actions require a credential-free GitHub origin URL')
        return match[1]

    def remote_head(self, root, branch):
        text = self.command(root, ['git', 'ls-remote', '--heads', 'origin', 'refs/heads/' + branch])
        return text.split()[0] if text.strip() else None

    def idle(self):
        if self.state.active_task or self.state.task_queue or getattr(self.state, 'workflow_busy', False):
            raise ValueError('Stop active/queued agent runs and workflows before Git mutations')
        if self.wb.studio.settings()['isolation']['enabled']:
            raise ValueError('Host Git/GitHub mutations are refused while enforced isolation is enabled')
        if any(p['running'] for p in self.wb.studio.execution.list()):
            raise ValueError('Stop managed dev apps before committing/pushing/merging')

    def mutable(self, root, record):
        self.idle()
        branch = self.branch(root)
        if not branch or branch.startswith('-') or branch in {'main', 'master'} or branch != self.initial_branch[record['id']]:
            raise ValueError('Protected/detached/changed branch refused. This toolbar never checks out or creates branches')
        return branch

    @property
    def github(self):
        with self.state.lock:
            if not hasattr(self, '_github'):
                from .github_repositories import GitHubRepositories
                self._github = GitHubRepositories(self)
            return self._github

    def get(self, path, query):
        return self.github.get(path, query)

    def pr(self, root, repository, number):
        if type(number) is not int or number <= 0:
            raise ValueError('Explicit positive PR number required')
        return json.loads(self.command(root, ['gh', 'pr', 'view', str(number), '--repo', repository, '--json',
            'number,title,url,state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid,mergeable,mergeStateStatus,statusCheckRollup']))

    def merge_ready(self, pr, branch, head):
        if pr['state'] != 'OPEN' or pr['isDraft'] or pr['headRefName'] != branch or pr['headRefOid'] != head:
            raise ValueError('PR must be open, non-draft, and match this exact local branch/HEAD')
        if pr['mergeable'] != 'MERGEABLE' or pr['mergeStateStatus'] != 'CLEAN':
            raise ValueError('PR is blocked, conflicting or mergeability is unknown; refresh after checks/reviews finish')
        for check in pr.get('statusCheckRollup') or []:
            if 'conclusion' in check:
                passed = check.get('status') == 'COMPLETED' and check.get('conclusion') in {'SUCCESS', 'SKIPPED', 'NEUTRAL'}
            else:
                passed = check.get('state') == 'SUCCESS'
            if not passed:
                raise ValueError('PR has pending/failed checks; no admin bypass is offered')

    def preview(self, payload):
        root, record = self.resolve(str(payload.get('repo', '')))
        action = payload.get('action')
        if action not in {'commit', 'push', 'create-pr', 'merge'}:
            raise ValueError('Unknown repository action')
        branch = self.mutable(root, record)
        head = self.command(root, ['git', 'rev-parse', 'HEAD']).strip()
        data = {'action': action, 'repo': record['id'], 'name': root.name, 'branch': branch, 'head': head,
                'fingerprint': self.fingerprint(root), 'id': uuid.uuid4().hex, 'expires': time.time() + 300}
        if action == 'commit':
            paths = payload.get('paths')
            message = str(payload.get('message', '')).strip()
            if not message or len(message) > 2000 or '\x00' in message:
                raise ValueError('Commit message required (up to 2000 characters)')
            allowed = set(self.paths(root))
            if not isinstance(paths, list) or not paths or len(paths) > 200 or any(not isinstance(p, str) or p not in allowed for p in paths):
                raise ValueError('Select actual changed files; private/runtime files cannot be selected')
            paths = sorted(set(paths))
            previews = []
            tracked = set(self.command(root, ['git', 'ls-files', '-z']).split('\0'))
            for name in paths:
                path = safe_path(root, name)
                if (root / name).is_symlink() or (path.exists() and not path.is_file()):
                    raise ValueError('Only regular file changes/deletions can be committed here')
                if name in tracked:
                    diff = self.command(root, ['git', 'diff', '--no-ext-diff', '--no-textconv', 'HEAD', '--', name])
                else:
                    if path.stat().st_size > 100000:
                        raise ValueError('New file too large for complete review')
                    try:
                        diff = 'NEW FILE ' + name + '\n' + path.read_text(encoding='utf-8')
                    except UnicodeError:
                        diff = 'Binary new file; text preview unavailable. Review the exact size/hash below.'
                previews.append({'path': name, 'diff': diff, 'bytes': path.stat().st_size if path.exists() else None,
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None})
            data.update(paths=paths, message=message, changes=previews,
                        warning='Commit executes trusted repository hooks/signing. Only selected working-tree paths commit; unrelated staged changes remain staged.')
            data['confirmation'] = 'COMMIT ' + branch
        else:
            repository = self.remote(root)
            data['repository'] = repository
            data['remote_head'] = self.remote_head(root, branch)
            if action == 'push':
                data.update(confirmation='PUSH ' + branch, warning='Push committed HEAD only to origin, without force. Hooks/credential helpers may execute.')
            elif action == 'create-pr':
                if data['remote_head'] != head:
                    raise ValueError('Push this exact committed HEAD first; creating a PR never implicitly pushes')
                title, body = str(payload.get('title', '')).strip(), str(payload.get('body', '')).strip()
                base = str(payload.get('base', 'main')).strip()
                if not title or not body or len(title) > 250 or len(body) > 30000:
                    raise ValueError('Reviewed PR title/body required')
                self.command(root, ['git', 'check-ref-format', '--branch', base])
                if base == branch or base.startswith('-'):
                    raise ValueError('Choose a different base branch')
                data.update(title=title, body=body, base=base, confirmation='CREATE PR ' + branch,
                            diff=self.command(root, ['git', 'diff', '--no-ext-diff', '--no-textconv', 'origin/' + base + '...HEAD']),
                            note='Diff uses local origin/base ref; no automatic fetch. GitHub evaluates the current remote base.')
            else:
                pr = self.pr(root, repository, payload.get('number'))
                self.merge_ready(pr, branch, head)
                method = payload.get('method', 'merge')
                if method not in {'merge', 'squash', 'rebase'}:
                    raise ValueError('Choose merge, squash or rebase')
                data.update(pr=pr, method=method, confirmation=f'MERGE #{pr["number"]} {repository}',
                            note='Rechecks remote head/checks before merging. No --admin, --auto or branch deletion. No configured checks is not test evidence.')
        if len(json.dumps(data)) > 2_000_000:
            raise ValueError('Review exceeds 2 MB; split the selected changes')
        with self.state.lock:
            self.previews = {k: v for k, v in self.previews.items() if v['expires'] > time.time()}
            if len(self.previews) >= 8:
                self.previews.pop(min(self.previews, key=lambda k: self.previews[k]['expires']))
            self.previews[data['id']] = data
        return observation(data, 'review preview, exact repository/branch/HEAD and one-use five-minute confirmation')

    def execute(self, payload):
        ident = str(payload.get('preview_id', ''))
        preview = self.previews.get(ident)
        if not preview or preview['expires'] < time.time():
            raise ValueError('Preview expired/missing; refresh and review again')
        if payload.get('confirm') != preview['confirmation']:
            raise ValueError('Typed confirmation must exactly match the reviewed action')
        root, record = self.resolve(preview['repo'])
        lock_path = self.wb.path.parent / ('repo-' + record['id'] + '.lock')
        with self.state.lock, FileLock(str(lock_path), timeout=5):
            if ident not in self.previews or preview['expires'] < time.time():
                raise ValueError('Preview already consumed/expired; review again')
            branch = self.mutable(root, record)
            if branch != preview['branch'] or self.fingerprint(root) != preview['fingerprint']:
                raise ValueError('Repository/index/HEAD changed since preview; nothing executed, review again')
            action = preview['action']
            if preview.get('github_repository') and self.remote(root).lower() != preview['github_repository'].lower():
                raise ValueError('Workspace origin changed since GitHub selection; review again')
            if action != 'commit' and (self.remote(root) != preview['repository'] or self.remote_head(root, branch) != preview['remote_head']):
                raise ValueError('Origin/remote branch changed; review again')
            self.previews.pop(ident, None)  # consumed even if an external operation fails
            if action == 'commit':
                self.command(root, ['git', 'add', '-A', '--', *preview['paths']])
                try:
                    output = self.command(root, ['git', 'commit', '--only', '-m', preview['message'], '--', *preview['paths']], timeout=120)
                except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                    raise ValueError('Commit failed after staging selected paths. Index may now contain staged changes; inspect Git status. ' + str(exc)) from exc
            elif action == 'push':
                output = self.command(root, ['git', 'push', 'origin', branch], timeout=120)
            elif action == 'create-pr':
                output = self.command(root, ['gh', 'pr', 'create', '--repo', preview['repository'], '--head', branch,
                    '--base', preview['base'], '--title', preview['title'], '--body', preview['body']], timeout=120)
            else:
                number = preview['pr']['number']
                current = self.pr(root, preview['repository'], number)
                self.merge_ready(current, branch, preview['head'])
                if current['baseRefOid'] != preview['pr']['baseRefOid']:
                    raise ValueError('PR base advanced; review the merge again')
                output = self.command(root, ['gh', 'pr', 'merge', str(number), '--repo', preview['repository'],
                    '--' + preview['method'], '--match-head-commit', preview['head']], timeout=120)
            return observation({'action': action, 'exit_code': 0, 'output': output, 'note': 'Actual command result. No branch switch, implicit follow-up push, pull, reset or deletion.'}, 'explicit confirmed Git/GitHub command')

    def post(self, path, payload):
        return self.github.post(path, payload)
