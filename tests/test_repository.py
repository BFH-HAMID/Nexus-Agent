"""Real disposable Git commits; remote GitHub operations are scripted test transports."""
import json
import subprocess

import pytest

from nexus.server import ServerState


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repos(repo):
    git(repo, 'branch', '-m', 'feature')
    git(repo, 'config', 'user.name', 'Repository Tests')
    git(repo, 'config', 'user.email', 'tests@example.org')
    service = ServerState(repo, quiet=True).workbench.repositories
    ident = next(r['id'] for r in service.discover() if r['agent_workspace'])
    return service, repo, ident


def test_real_status_and_server_local_selection(repos):
    service, root, ident = repos
    (root / 'new.txt').write_text('one\ntwo\n')
    (root / '.env').write_text('PRIVATE_SECRET=do-not-expose')
    status = service.status(ident)
    assert status['branch'] == 'feature' and status['additions'] == 2
    assert 'new.txt' in status['files'] and '.env' not in status['files']
    assert service.wb.workspace == root
    with pytest.raises(ValueError):
        service.status('invented-repo-id')


def test_commit_selected_paths_preserves_unrelated_staged_changes(repos):
    service, root, ident = repos
    (root / 'README.md').write_text('unrelated staged changes\n')
    git(root, 'add', 'README.md')
    (root / 'chosen.txt').write_text('reviewed selected content\n')
    before = git(root, 'rev-parse', 'HEAD').strip()
    preview = service.preview({'repo':ident, 'action':'commit', 'paths':['chosen.txt'], 'message':'feat: selected change'})['data']
    assert 'reviewed selected content' in preview['changes'][0]['diff']
    assert git(root, 'rev-parse', 'HEAD').strip() == before
    result = service.execute({'preview_id':preview['id'], 'confirm':preview['confirmation']})['data']
    assert result['exit_code'] == 0
    assert git(root, 'show', '--format=', '--name-only', 'HEAD').strip() == 'chosen.txt'
    assert git(root, 'diff', '--cached', '--name-only').strip() == 'README.md'
    with pytest.raises(ValueError, match='expired|missing'):
        service.execute({'preview_id':preview['id'], 'confirm':preview['confirmation']})


def test_commit_stale_preview_does_nothing(repos):
    service, root, ident = repos
    (root / 'chosen.txt').write_text('first\n')
    preview = service.preview({'repo':ident, 'action':'commit', 'paths':['chosen.txt'], 'message':'review'})['data']
    before = git(root, 'rev-parse', 'HEAD')
    (root / 'chosen.txt').write_text('manual update\n')
    with pytest.raises(ValueError, match='changed'):
        service.execute({'preview_id':preview['id'], 'confirm':preview['confirmation']})
    assert git(root, 'rev-parse', 'HEAD') == before
    assert git(root, 'diff', '--cached', '--name-only') == ''


@pytest.mark.parametrize('paths', [['../outside'], ['.env'], [], ['not-a-change']])
def test_commit_path_validation(repos, paths):
    service, _, ident = repos
    with pytest.raises(ValueError):
        service.preview({'repo':ident, 'action':'commit', 'paths':paths, 'message':'review'})


def test_active_runs_protected_branches_and_isolation_block_mutations(repos):
    service, root, ident = repos
    service.state.active_task = 'active'
    with pytest.raises(ValueError, match='Stop'):
        service.preview({'repo':ident, 'action':'push'})
    service.state.active_task = None
    git(root, 'branch', '-m', 'main')
    with pytest.raises(ValueError, match='Protected'):
        service.preview({'repo':ident, 'action':'push'})
    assert git(root, 'branch', '--show-current').strip() == 'main'


def remote_script(repos, monkeypatch):
    service, root, ident = repos
    git(root, 'remote', 'add', 'origin', 'https://github.com/example/repo.git')
    head = git(root, 'rev-parse', 'HEAD').strip()
    pr = {'number':42, 'state':'OPEN', 'title':'Reviewed PR', 'url':'https://github.com/example/repo/pull/42',
          'isDraft':False,'headRefName':'feature','headRefOid':head,'baseRefName':'main','baseRefOid':'b'*40,
          'mergeable':'MERGEABLE','mergeStateStatus':'CLEAN','statusCheckRollup':[]}
    calls = []
    actual = service.command
    def command(directory, argv, timeout=25):
        if argv[:2] == ['git','ls-remote']:
            calls.append(argv)
            return head+'\trefs/heads/feature\n'
        if argv[:3] == ['gh','pr','view']:
            calls.append(argv)
            return json.dumps(pr)
        if argv[:2] == ['git','push'] or argv[:3] in (['gh','pr','merge'], ['gh','pr','create']):
            calls.append(argv)
            return 'scripted remote result (test only)'
        return actual(directory, argv, timeout)
    monkeypatch.setattr(service, 'command', command)
    return service, ident, head, pr, calls


def test_push_requires_typed_confirmation_and_never_forces(repos, monkeypatch):
    service, ident, _, _, calls = remote_script(repos, monkeypatch)
    preview = service.preview({'repo':ident, 'action':'push'})['data']
    assert not any(c[:2] == ['git','push'] for c in calls)
    with pytest.raises(ValueError, match='Typed'):
        service.execute({'preview_id':preview['id'], 'confirm':'wrong'})
    service.execute({'preview_id':preview['id'], 'confirm':preview['confirmation']})
    assert calls[-1] == ['git','push','origin','feature']


def test_merge_explicit_pr_head_match_and_no_auto_or_admin(repos, monkeypatch):
    service, ident, head, _, calls = remote_script(repos, monkeypatch)
    preview = service.preview({'repo':ident, 'action':'merge', 'number':42, 'method':'squash'})['data']
    assert not any(c[:3] == ['gh','pr','merge'] for c in calls)
    service.execute({'preview_id':preview['id'], 'confirm':preview['confirmation']})
    assert calls[-1] == ['gh','pr','merge','42','--repo','example/repo','--squash','--match-head-commit',head]
    assert '--auto' not in calls[-1] and '--admin' not in calls[-1] and '--delete-branch' not in calls[-1]


def test_merge_refuses_pending_checks_and_changed_head(repos, monkeypatch):
    service, ident, _, pr, calls = remote_script(repos, monkeypatch)
    pr['statusCheckRollup'] = [{'status':'IN_PROGRESS','conclusion':''}]
    with pytest.raises(ValueError, match='pending/failed'):
        service.preview({'repo':ident, 'action':'merge', 'number':42})
    pr['statusCheckRollup'] = []
    preview = service.preview({'repo':ident, 'action':'merge', 'number':42})['data']
    pr['headRefOid'] = 'c'*40
    with pytest.raises(ValueError, match='exact local'):
        service.execute({'preview_id':preview['id'], 'confirm':preview['confirmation']})
    assert not any(c[:3] == ['gh','pr','merge'] for c in calls)


def test_pr_creation_never_implicitly_pushes(repos, monkeypatch):
    service, ident, _, _, calls = remote_script(repos, monkeypatch)
    actual = service.command
    def command(root, argv, timeout=25):
        if argv[:2] == ['git','diff'] and 'origin/main...HEAD' in argv:
            return 'committed diff fixture'
        return actual(root, argv, timeout)
    monkeypatch.setattr(service, 'command', command)
    preview = service.preview({'repo':ident,'action':'create-pr','base':'main','title':'Reviewed','body':'Reviewed changes and evidence'})['data']
    service.execute({'preview_id':preview['id'],'confirm':preview['confirmation']})
    assert calls[-1][:3] == ['gh','pr','create']
    assert not any(c[:2] == ['git','push'] for c in calls)


def test_concurrent_push_cannot_reuse_one_preview(repos, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    service, ident, _, _, calls = remote_script(repos, monkeypatch)
    preview = service.preview({'repo':ident,'action':'push'})['data']
    def execute():
        try:
            service.execute({'preview_id':preview['id'],'confirm':preview['confirmation']})
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda n: execute(), range(2)))
    assert results.count(True) == 1
    assert sum(c[:2] == ['git','push'] for c in calls) == 1
