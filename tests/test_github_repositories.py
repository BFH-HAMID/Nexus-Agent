"""Scripted GitHub transport and real disposable local Git: never live mutations."""
import json
import subprocess
import time
from urllib.parse import unquote

import pytest

from nexus.server import ServerState


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


@pytest.fixture()
def remote(repo, monkeypatch):
    git(repo, 'remote', 'add', 'origin', 'https://github.com/example/local.git')
    git(repo, 'config', 'user.name', 'Nexus Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(repo, 'branch', '-m', 'feature/test')
    local = ServerState(repo, quiet=True).workbench.repositories
    service = local.github
    records = [dict(full_name=name, private=name.endswith('private'), default_branch='main', archived=False,
                    permissions={'push': True}) for name in ('example/local', 'example/private')]
    state = {'viewer': 'test-user', 'records': records, 'heads': {'main': 'a'*40, 'feature/test': 'b'*40},
             'write': True, 'archived': False, 'calls': [], 'writes': []}
    state['pr'] = {'number': 42, 'title': 'Test PR', 'url': 'https://github.com/example/private/pull/42',
        'state': 'OPEN', 'isDraft': False, 'headRefName': 'feature/test', 'headRefOid': 'b'*40,
        'baseRefName': 'main', 'baseRefOid': 'a'*40, 'mergeable': 'MERGEABLE',
        'mergeStateStatus': 'CLEAN', 'statusCheckRollup': []}
    actual = local.command
    def command(root, argv, timeout=25):
        if argv[0] != 'gh':
            return actual(root, argv, timeout)
        state['calls'].append(argv)
        if argv[:3] == ['gh', 'api', 'user']:
            return state['viewer']
        if argv[:3] == ['gh', 'api', '--paginate']:
            endpoint = argv[3]
            if endpoint.startswith('user/repos?'):
                return '\n'.join(json.dumps(r) for r in state['records'])
            if '/branches?' in endpoint:
                return '\n'.join(state['heads'])
            if '/pulls?' in endpoint:
                return json.dumps(state['pr'])
        if argv[:2] == ['gh', 'api']:
            endpoint = argv[2]
            if '/branches/' in endpoint:
                return json.dumps({'commit': {'sha': state['heads'][unquote(endpoint.split('/branches/')[1])]}})
            if '/compare/' in endpoint:
                return '--- a/app.py\n+++ b/app.py\n+reviewed change\n'
            slug = endpoint.removeprefix('repos/')
            return json.dumps({'full_name': slug, 'default_branch': 'main', 'archived': state['archived'],
                               'permissions': {'push': state['write']}})
        if argv[:3] == ['gh', 'pr', 'view']:
            return json.dumps(state['pr'])
        if argv[:3] == ['gh', 'pr', 'diff']:
            return '+reviewed PR diff'
        if argv[:3] in (['gh', 'pr', 'create'], ['gh', 'pr', 'merge']):
            state['writes'].append(argv)
            return 'scripted GitHub command result (test only)'
        raise AssertionError(argv)
    monkeypatch.setattr(local, 'command', command)
    return service, state, repo


def create(service):
    return service.post('preview', {'repo': 'example/private', 'action': 'create-pr', 'base': 'main',
        'head': 'feature/test', 'title': 'Reviewed change', 'body': 'Reviewed evidence'})['data']


def test_catalog_uses_authenticated_paginated_github_inventory_not_siblings(remote):
    service, state, root = remote
    sibling = root.parent / 'unrelated-local'
    sibling.mkdir()
    git(sibling, 'init', '-q')
    records = service.get('list', {})['data']
    assert [r['id'] for r in records] == ['example/local', 'example/private']
    assert records[1]['private'] and records[0]['agent_workspace']
    request = next(c for c in state['calls'] if 'user/repos?' in ' '.join(c))
    assert '--paginate' in request and 'organization_member' in request[3]
    assert all('unrelated-local' not in r['name'] for r in records)
    assert state['writes'] == []


def test_remote_status_never_fabricates_local_diff_or_switches_workspace(remote):
    service, state, root = remote
    before = git(root, 'branch', '--show-current')
    status = service.get('status', {'repo': ['example/private']})['data']
    assert status['local_available'] is False and status['path'] is None
    assert status['additions'] is None and status['deletions'] is None
    assert status['branch'] == 'main'
    assert service.local.wb.workspace == root and git(root, 'branch', '--show-current') == before
    assert state['writes'] == []


def test_matching_workspace_counts_and_local_commit_guard(remote):
    service, _, root = remote
    (root / 'fresh.txt').write_text('actual\n')
    status = service.get('status', {'repo': ['example/local']})['data']
    assert status['local_available'] is True and status['additions'] == 1
    with pytest.raises(ValueError, match='origin to match'):
        service.post('preview', {'repo': 'example/private', 'action': 'commit', 'paths': ['fresh.txt'], 'message': 'No wrong repo'})


def test_branch_and_pr_lists_target_selected_repository_without_local_head_filter(remote):
    service, state, _ = remote
    assert service.get('branches', {'repo': ['example/private']})['data'] == ['main', 'feature/test']
    assert service.get('prs', {'repo': ['example/private']})['data'][0]['number'] == 42
    endpoints = [c[3] for c in state['calls'] if c[:3] == ['gh', 'api', '--paginate']]
    assert 'repos/example/private/pulls?state=open&per_page=100' in endpoints


def test_remote_create_pr_reviews_diff_and_never_clones_pushes_or_changes_local_git(remote):
    service, state, root = remote
    before = git(root, 'rev-parse', 'HEAD')
    review = create(service)
    assert 'reviewed change' in review['diff'] and review['head_sha'] == 'b'*40
    assert state['writes'] == []
    with pytest.raises(ValueError, match='Typed'):
        service.post('execute', {'preview_id': review['id'], 'confirm': 'wrong'})
    result = service.post('execute', {'preview_id': review['id'], 'confirm': review['confirmation']})['data']
    assert result['exit_code'] == 0
    assert state['writes'][-1] == ['gh', 'pr', 'create', '--repo', 'example/private', '--base', 'main',
        '--head', 'feature/test', '--title', 'Reviewed change', '--body', 'Reviewed evidence']
    assert git(root, 'rev-parse', 'HEAD') == before
    with pytest.raises(ValueError, match='missing'):
        service.post('execute', {'preview_id': review['id'], 'confirm': review['confirmation']})


@pytest.mark.parametrize('branch', ['main', 'feature/test'])
def test_create_rejects_advanced_remote_branches(remote, branch):
    service, state, _ = remote
    review = create(service)
    state['heads'][branch] = 'c'*40
    with pytest.raises(ValueError, match='advanced'):
        service.post('execute', {'preview_id': review['id'], 'confirm': review['confirmation']})
    assert not state['writes']


def test_account_and_permissions_revalidated(remote):
    service, state, _ = remote
    review = create(service)
    state['viewer'] = 'another-account'
    with pytest.raises(ValueError, match='account changed'):
        service.post('execute', {'preview_id': review['id'], 'confirm': review['confirmation']})
    state['viewer'] = 'test-user'
    state['write'] = False
    with pytest.raises(ValueError, match='write permission'):
        service.post('execute', {'preview_id': review['id'], 'confirm': review['confirmation']})
    assert not state['writes']


def test_merge_remote_pr_uses_explicit_repo_number_and_exact_sha(remote):
    service, state, _ = remote
    review = service.post('preview', {'repo': 'example/private', 'action': 'merge', 'number': 42, 'method': 'squash'})['data']
    assert 'reviewed PR diff' in review['diff']
    service.post('execute', {'preview_id': review['id'], 'confirm': review['confirmation']})
    assert state['writes'][-1] == ['gh', 'pr', 'merge', '42', '--repo', 'example/private', '--squash',
                                  '--match-head-commit', 'b'*40]
    assert '--auto' not in state['writes'][-1] and '--admin' not in state['writes'][-1]


@pytest.mark.parametrize('field', ['headRefOid', 'baseRefOid', 'baseRefName'])
def test_merge_refuses_changed_remote_head_or_base(remote, field):
    service, state, _ = remote
    review = service.post('preview', {'repo': 'example/private', 'action': 'merge', 'number': 42})['data']
    state['pr'][field] = 'changed'
    with pytest.raises(ValueError):
        service.post('execute', {'preview_id': review['id'], 'confirm': review['confirmation']})
    assert not state['writes']


def test_merge_refuses_pending_checks(remote):
    service, state, _ = remote
    state['pr']['statusCheckRollup'] = [{'status': 'IN_PROGRESS', 'conclusion': ''}]
    with pytest.raises(ValueError, match='pending/failed'):
        service.post('preview', {'repo': 'example/private', 'action': 'merge', 'number': 42})


def test_expiration_and_unlisted_repo_fail_closed(remote):
    service, state, _ = remote
    with pytest.raises(ValueError, match='not in'):
        service.get('status', {'repo': ['other/not-accessible']})
    review = create(service)
    review['expires'] = time.time() - 1
    with pytest.raises(ValueError, match='expired'):
        service.post('execute', {'preview_id': review['id'], 'confirm': review['confirmation']})
    assert not state['writes']


def test_disconnected_github_has_no_local_fallback(remote, monkeypatch):
    service, _, _ = remote
    def fail(*args, **kwargs):
        raise FileNotFoundError('gh not installed')
    monkeypatch.setattr(service, 'command', fail)
    with pytest.raises(ValueError, match='connection needs attention'):
        service.get('list', {})


def test_isolation_refuses_github_discovery_before_transport(remote, monkeypatch):
    service, state, _ = remote
    monkeypatch.setattr(service.local.wb.studio, 'settings', lambda: {'isolation': {'enabled': True}})
    with pytest.raises(ValueError, match='isolation'):
        service.get('list', {})
    assert not state['calls']


def test_inventory_cache_invalidates_on_account_change_and_explicit_refresh(remote):
    service, state, _ = remote
    service.get('list', {})
    state['records'] = [state['records'][1]]
    assert len(service.get('list', {})['data']) == 2  # Same-account cache.
    state['viewer'] = 'another-account'
    assert [r['id'] for r in service.get('list', {})['data']] == ['example/private']
    state['records'] = []
    assert service.get('list', {'refresh': ['1']})['data'] == []


def local_commit(service, root):
    (root / 'fresh.txt').write_text('reviewed local content\n')
    return service.post('preview', {'repo': 'example/local', 'action': 'commit',
                                  'paths': ['fresh.txt'], 'message': 'Reviewed fixture commit'})['data']


def test_matching_workspace_commit_delegates_review_and_commits_only_fixture(remote):
    service, state, root = remote
    review = local_commit(service, root)
    assert review['github_repository'] == 'example/local'
    assert review['github_viewer'] == 'test-user'
    service.post('execute', {'preview_id': review['id'], 'confirm': review['confirmation']})
    assert git(root, 'log', '-1', '--format=%s') == 'Reviewed fixture commit'
    assert not state['writes']


@pytest.mark.parametrize('change', ['account', 'origin'])
def test_local_preview_rejects_changed_account_or_origin(remote, change):
    service, state, root = remote
    review = local_commit(service, root)
    before = git(root, 'rev-parse', 'HEAD')
    if change == 'account':
        state['viewer'] = 'another-account'
    else:
        git(root, 'remote', 'set-url', 'origin', 'https://github.com/example/private.git')
    with pytest.raises(ValueError):
        service.post('execute', {'preview_id': review['id'], 'confirm': review['confirmation']})
    assert git(root, 'rev-parse', 'HEAD') == before


def test_concurrent_remote_preview_reuse_executes_once(remote):
    from concurrent.futures import ThreadPoolExecutor
    service, state, _ = remote
    review = create(service)
    payload = {'preview_id': review['id'], 'confirm': review['confirmation']}
    def execute():
        try:
            service.post('execute', payload)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: execute(), range(2))) == [False, True]
    assert len(state['writes']) == 1
