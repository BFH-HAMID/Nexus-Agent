"""Reviewed, runnable project scaffolds and folder/repository lifecycle mapping.

Reuses Studio records, repository approvals, transport, secret scans and CI gates.
Selected remote agent tasks remain API-only; this is an explicit new-project flow.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .github_safety import conventional_message, scan_index, scan_text
from .workbench import observation, safe_path

MIT = '''MIT License

Copyright (c) 2026 Project contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
'''


def scaffold(name, stack):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,59}', name):
        raise ValueError('Project name must be a simple identifier, at most 60 characters')
    if stack not in {'python', 'node'}:
        raise ValueError('Supported runnable stacks: python or node')
    files = {'LICENSE': MIT, '.gitignore': '.env\n.env.*\n!.env.example\n.nexus/\n.venv/\nnode_modules/\n__pycache__/\n',
             '.env.example': '# Optional server-side GitHub credential; never commit actual values.\nGITHUB_TOKEN=\n'}
    if stack == 'python':
        files.update({'app.py': '''import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def health():
    return {"status": "ok"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(health() if self.path == "/health" else {"error": "not found"}).encode()
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
''', 'test_app.py': '''import unittest
from app import health


class HealthTest(unittest.TestCase):
    def test_health(self):
        self.assertEqual(health(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
'''})
        setup, check, start = 'actions/setup-python@v5\n        with:\n          python-version: "3.12"', 'python -m unittest discover -v', 'python app.py'
    else:
        files.update({'package.json': json.dumps({'name': name.lower(), 'private': True, 'type': 'module',
             'scripts': {'start': 'node app.mjs', 'test': 'node --test'}}, indent=2) + '\n',
             'app.mjs': '''import http from 'node:http';
import {pathToFileURL} from 'node:url';
export const health = () => ({status: 'ok'});
export function server() {
  return http.createServer((req, res) => {
    const ok = req.url === '/health';
    res.writeHead(ok ? 200 : 404, {'Content-Type': 'application/json'});
    res.end(JSON.stringify(ok ? health() : {error: 'not found'}));
  });
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  server().listen(8000, '127.0.0.1');
}
''', 'app.test.mjs': '''import {test} from 'node:test';
import assert from 'node:assert/strict';
import {health} from './app.mjs';
test('health is operational', () => assert.deepEqual(health(), {status: 'ok'}));
'''})
        setup, check, start = 'actions/setup-node@v4\n        with:\n          node-version: "22"', 'npm test', 'npm start'
    files['README.md'] = f'# {name}\n\nMinimal {stack} HTTP health service.\n\nRun: `{start}`\n\nTests: `{check}`\n\nGET http://127.0.0.1:8000/health returns actual health JSON.\n\nMIT licensed. No deployment, authentication or payment integration is implied.\n'
    files['.github/workflows/ci.yml'] = f'''name: Project tests
on: [push, pull_request]
permissions:
  contents: read
jobs:
  tests:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: {setup}
      - run: {check}
'''
    return files


class Projects:
    def __init__(self, github):
        self.gh = github
        self.wb = github.local.wb
        self.root = github.local.root
        self.previews = {}

    def get(self, name):
        scaffold(name, 'python')  # Validate only; no filesystem changes.
        return self.wb.studio.find('github-project', 'project-' + name.lower())

    def listing(self):
        return self.wb.studio.items('github-project')

    def preview(self, payload):
        name, action = payload.get('name'), payload.get('action')
        self.gh.network()
        self.gh.local.idle()
        if action == 'scaffold':
            files = scaffold(name, payload.get('stack'))
            repository = self.gh.record(payload.get('repo'))['repository']
            metadata = self.gh.metadata(repository)
            if not metadata.get('default_branch'):
                raise ValueError('Initialize the remote repository README before scaffolding')
            target = safe_path(self.root, 'projects/' + name)
            if target.exists() or target.is_symlink():
                raise ValueError('Project folder already exists; never overwrite it')
            try:
                self.get(name)
            except ValueError:
                pass
            else:
                raise ValueError('Project name already mapped')
            branch = 'nexus/project-' + name.lower()
            data = {'action': action, 'name': name, 'repository': repository, 'base': metadata['default_branch'],
                    'branch': branch, 'stack': payload['stack'], 'files': files,
                    'confirmation': f'SCAFFOLD {name} FOR {repository} WITH MIT LICENSE'}
        else:
            project = self.get(name)
            directory = safe_path(self.root, project['folder'])
            if action not in {'commit', 'push', 'pr', 'merge'}:
                raise ValueError('Unknown project action')
            if self.git(directory, ['branch', '--show-current']) != project['branch']:
                raise ValueError('Project branch changed; review manually before continuing')
            if action == 'commit':
                from .server import ServerState
                service = ServerState(directory, quiet=True).workbench.repositories
                ident = next(r['id'] for r in service.discover() if r['agent_workspace'])
                preview = service.preview({'repo': ident, 'action': 'commit', 'paths': service.paths(directory),
                                           'message': payload.get('message')})['data']
                data = {'action': action, 'name': name, 'project': project, 'local': service,
                        'local_preview': preview, 'confirmation': f'COMMIT PROJECT {name}', 'changes': preview['changes']}
            elif action == 'push':
                head = self.git(directory, ['rev-parse', 'HEAD'])
                data = {'action': action, 'name': name, 'project': project, 'head': head,
                        'confirmation': f'PUSH PROJECT {name} {head}',
                        'warning': 'Publishing workflows can consume GitHub Actions minutes. Verify token workflow scopes and repository CI budget before approving.'}
            else:
                if action == 'pr':
                    args = {'action': 'create-pr', 'repo': project['repository'], 'head': project['branch'],
                            'base': project['base'], 'title': 'feat: ' + name,
                            'body': 'Project ' + name + '. Exact-head tests are required and revalidated before PR creation.'}
                else:
                    if not project.get('pr'):
                        raise ValueError('Create the project PR first')
                    args = {'action': 'merge', 'repo': project['repository'], 'number': project['pr'], 'method': 'merge'}
                remote = self.gh.preview(args)['data']
                data = {'action': action, 'name': name, 'project': project, 'remote': remote,
                        'confirmation': remote['confirmation']}
        data.update(id=hashlib.sha256((str(time.time_ns()) + name).encode()).hexdigest(), expires=time.time() + 300,
                    viewer=self.gh.viewer())
        for value in data.get('files', {}).values():
            scan_text(value, workspace=self.root)
        self.previews = {k: v for k, v in self.previews.items() if v['expires'] > time.time()}
        if len(self.previews) >= 8:
            raise ValueError('Too many outstanding project previews')
        self.previews[data['id']] = data
        return observation({k: v for k, v in data.items() if k != 'local'}, 'review only; project-specific folder/repo/branch')

    @staticmethod
    def git(directory, args, *, strip=True):
        result = subprocess.run(['git', '--no-pager', '-c', 'core.fsmonitor=false', '-c', 'log.showSignature=false', *args], cwd=directory,
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ValueError('Local project Git step failed; inspect project status (output withheld)')
        return result.stdout.strip() if strip else result.stdout

    def execute(self, payload):
        with self.wb.lock:
            data = self.previews.get(payload.get('preview_id'))
            if not data or data['expires'] < time.time():
                raise ValueError('Project preview missing/expired')
            if payload.get('confirm') != data['confirmation']:
                raise ValueError('Exact project confirmation required')
            self.gh.local.idle()
            self.gh.network()
            if self.gh.viewer() != data['viewer']:
                raise ValueError('GitHub identity changed')
            self.previews.pop(data['id'])
            name, action = data['name'], data['action']
            if action == 'scaffold':
                target = safe_path(self.root, 'projects/' + name)
                if target.exists() or target.is_symlink():
                    raise ValueError('Project folder changed; refused')
                parent = safe_path(self.root, 'projects')
                parent.mkdir(exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='.nexus-project-', dir=parent) as stage, tempfile.TemporaryDirectory() as hooks:
                    stage = Path(stage)
                    for path, text in data['files'].items():
                        scan_text(text, workspace=self.root)
                        file = safe_path(stage, path)
                        file.parent.mkdir(parents=True, exist_ok=True)
                        file.write_text(text, encoding='utf-8')
                    self.git(stage, ['init', '--template=', '--initial-branch=' + data['branch']])
                    self.git(stage, ['config', '--local', 'user.name', 'Nexus Agent'])
                    self.git(stage, ['config', '--local', 'user.email', 'nexus-agent@users.noreply.github.com'])
                    self.git(stage, ['remote', 'add', 'origin', 'https://github.com/' + data['repository'] + '.git'])
                    self.git(stage, ['add', '--all'])
                    scan_index(stage)
                    self.git(stage, ['-c', 'core.hooksPath=' + hooks, '-c', 'user.name=Nexus Agent',
                                     '-c', 'user.email=nexus-agent@users.noreply.github.com', 'commit', '-m', 'feat: initialize ' + data['stack'] + ' project'])
                    # Fetching a selected remote task is forbidden. This new-project initializer
                    # instead uses GitHub's base tree when publishing (see publish below).
                    stage.rename(target)
                project = {k: data[k] for k in ('name', 'repository', 'base', 'branch', 'stack')}
                project['folder'] = target.relative_to(self.root).as_posix()
            else:
                project = data['project']
                directory = safe_path(self.root, project['folder'])
                if action == 'commit':
                    local = data['local_preview']
                    data['local'].execute({'preview_id': local['id'], 'confirm': local['confirmation']})
                elif action == 'push':
                    if self.git(directory, ['rev-parse', 'HEAD']) != data['head']:
                        raise ValueError('Project HEAD changed since approval')
                    self.publish(project, directory, expected_head=data['head'])
                else:
                    remote = data['remote']
                    result = self.gh.execute({'preview_id': remote['id'], 'confirm': remote['confirmation']})['data']
                    if action == 'pr':
                        match = re.search(r'/pull/(\d+)\b', result['output'])
                        if not match:
                            raise ValueError('PR outcome uncertain; inspect GitHub before retrying')
                        project['pr'] = int(match[1])
                    else:
                        project['merged'] = True
            self.wb.studio.put('github-project', project, 'project-' + name.lower())
            return observation(project, 'approved project action; persisted folder/repository mapping')

    def publish(self, project, directory, *, expected_head):
        """Publish committed scaffold onto remote base via Git Data API, not clone/fetch.

        Base README history is retained. New immutable task commits never update main.
        Subsequent publishes are pinned to the recorded task head and non-force refs.
        """
        self.gh.check_writes(project['repository'])
        base = project.get('remote_head') or self.gh.branch_head(project['repository'], project['base'])
        root = 'repos/' + project['repository']
        if project.get('remote_head') and self.gh.branch_head(project['repository'], project['branch']) != base:
            raise ValueError('Remote project branch advanced; reconcile manually')
        head = self.git(directory, ['rev-parse', 'HEAD'])
        if head != expected_head:
            raise ValueError('Project HEAD changed; no publish attempted')
        message = conventional_message(self.git(directory, ['show', '-s', '--format=%B', head]))
        scan_text(message, workspace=self.root)
        rows = self.git(directory, ['ls-tree', '-r', '--name-only', head]).splitlines()
        entries = []
        if len(rows) > 200:
            raise ValueError('Project publish exceeds 200 text files')
        deadline = time.monotonic() + 60
        for name in rows:
            if time.monotonic() > deadline:
                raise ValueError('Project publish scan time budget exhausted')
            safe_path(directory, name)
            mode = self.git(directory, ['ls-tree', head, '--', name]).split()[0]
            if mode not in {'100644', '100755'}:
                raise ValueError('Only regular project text blobs may be published')
            size = int(self.git(directory, ['cat-file', '-s', head + ':' + name]))
            if size > 500000:
                raise ValueError('Project file exceeds bounded text API publish limit')
            text = self.git(directory, ['show', '--no-ext-diff', '--no-textconv', head + ':' + name], strip=False)
            scan_text(text, workspace=self.root)
            entries.append({'path': name, 'mode': mode, 'type': 'blob', 'content': text})
        # Preserve base files; deletions only affect paths previously owned by this project.
        entries += [{'path': p, 'mode': '100644', 'type': 'blob', 'sha': None}
                    for p in project.get('published_files', []) if p not in rows]
        if len(json.dumps(entries).encode()) > 2_000_000:
            raise ValueError('Project publish tree exceeds the 2 MB review budget')
        if self.git(directory, ['rev-parse', 'HEAD']) != expected_head:
            raise ValueError('Project HEAD changed during scan; no publish attempted')
        parent = self.gh.api(root + '/git/commits/' + base)
        tree = self.gh.api(root + '/git/trees', {'base_tree': parent['tree']['sha'], 'tree': entries})
        commit = self.gh.api(root + '/git/commits', {'message': message,
                                                   'tree': tree['sha'], 'parents': [base]})
        if project.get('remote_head'):
            self.gh.api(root + '/git/refs/heads/' + project['branch'], {'sha': commit['sha'], 'force': False}, method='PATCH')
        else:
            self.gh.api(root + '/git/refs', {'ref': 'refs/heads/' + project['branch'], 'sha': commit['sha']})
        project.update(remote_head=commit['sha'], local_head=head, published_files=rows)
