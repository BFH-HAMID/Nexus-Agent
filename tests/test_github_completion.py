"""Module 2 acceptance: real runnable scaffolds/Git, explicitly scripted GitHub CI/API."""
import datetime as dt
import hashlib
import json
import subprocess
import sys

import pytest

from nexus.github_checks import require_tests
from nexus.server import ServerState

HEAD = 'a' * 40


def passing(head=HEAD):
    return {'id': 7, 'name': 'tests', 'head_sha': head, 'status': 'completed', 'conclusion': 'success',
            'completed_at': dt.datetime.now(dt.timezone.utc).isoformat()}


def evidence(runs=None, sha=HEAD, count=None, statuses=None):
    runs = [passing()] if runs is None else runs
    def api(endpoint):
        if '/check-runs?' in endpoint:
            return {'total_count': len(runs) if count is None else count, 'check_runs': runs}
        return {'sha': sha, 'total_count': len(statuses or []), 'statuses': statuses or []}
    return api


def test_exact_head_successful_test_required_not_empty_checks_or_model_claim():
    assert require_tests(evidence(), 'owner/project', HEAD)[0]['head'] == HEAD
    with pytest.raises(ValueError, match='No successful test'):
        require_tests(evidence([]), 'owner/project', HEAD)


@pytest.mark.parametrize('mutation', [{'head_sha': 'b' * 40}, {'status': 'queued'}, {'conclusion': 'failure'},
    {'conclusion': 'skipped'}, {'name': 'lint'}, {'completed_at': 'invalid'},
    {'completed_at': '2020-01-01T00:00:00Z'}, {'completed_at': '2099-01-01T00:00:00Z'}])
def test_stale_wrong_head_pending_failed_or_non_test_evidence_rejected(mutation):
    with pytest.raises(ValueError):
        require_tests(evidence([{**passing(), **mutation}]), 'owner/project', HEAD)


def test_truncated_or_wrong_status_evidence_blocks():
    for api in (evidence(count=101), evidence(sha='b' * 40), evidence(statuses=[{'state': 'pending'}])):
        with pytest.raises(ValueError):
            require_tests(api, 'owner/project', HEAD)


@pytest.fixture()
def project_service(repo, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'fixture-scoped-credential')
    service = ServerState(repo, quiet=True).workbench.repositories.github
    store = {'refs': {'main': 'b' * 40}, 'commits': {'b' * 40: {'tree': {'sha': 'c' * 40}}},
             'trees': {}, 'checks': {}, 'writes': [], 'pr': None}
    monkeypatch.setattr(service, 'viewer', lambda: 'fixture-user')
    monkeypatch.setattr(service, 'record', lambda slug: {'id': slug, 'repository': slug})
    monkeypatch.setattr(service, 'metadata', lambda slug: {'full_name': slug, 'default_branch': 'main', 'permissions': {'push': True}})
    monkeypatch.setattr(service, 'branch_head', lambda slug, branch: store['refs'][branch])
    def api(endpoint, body=None, method=None):
        if '/check-runs?' in endpoint:
            head = endpoint.split('/commits/')[1].split('/')[0]
            runs = store['checks'].get(head, [])
            return {'check_runs': runs, 'total_count': len(runs)}
        if endpoint.endswith('/status'):
            return {'sha': endpoint.split('/commits/')[1].split('/')[0], 'total_count': 0, 'statuses': []}
        root = 'repos/fixture-user/project/'
        route = endpoint.removeprefix(root)
        if body is None and route.startswith('git/commits/'):
            commit = store['commits'][route[12:]]
            return {**commit, 'tree': commit['tree'] if isinstance(commit['tree'], dict) else {'sha': commit['tree']}}
        if body is not None:
            store['writes'].append((endpoint, body))
            sha = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
            if route == 'git/trees':
                store['trees'][sha] = body
                return {'sha': sha}
            if route == 'git/commits':
                store['commits'][sha] = body
                return {'sha': sha}
            if route == 'git/refs':
                branch = body['ref'].removeprefix('refs/heads/')
                assert branch not in store['refs']
                store['refs'][branch] = body['sha']
                return {}
            if route.startswith('git/refs/heads/'):
                assert body['force'] is False
                branch = route[len('git/refs/heads/'):]
                assert store['commits'][body['sha']]['parents'] == [store['refs'][branch]]
                store['refs'][branch] = body['sha']
                return {}
        raise AssertionError((endpoint, body))
    monkeypatch.setattr(service, 'api', api)
    actual = service.local.command
    def command(root, argv, timeout=30):
        if argv[0] != 'gh':
            return actual(root, argv, timeout)
        if argv[:3] == ['gh', 'api', 'repos/fixture-user/project/compare/' + store['refs']['main'] + '...' + store['refs'].get('nexus/project-shop', '')]:
            return '+actual scaffold change (scripted remote diff)'
        if argv[:3] == ['gh', 'pr', 'create']:
            store['writes'].append(('create-pr', argv))
            store['pr'] = {'number': 12, 'state': 'OPEN', 'isDraft': False, 'headRefName': 'nexus/project-shop',
                'headRefOid': store['refs']['nexus/project-shop'], 'baseRefName': 'main', 'baseRefOid': store['refs']['main'],
                'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN', 'statusCheckRollup': []}
            return 'https://github.com/fixture-user/project/pull/12'
        if argv[:3] == ['gh', 'pr', 'view']:
            return json.dumps(store['pr'])
        if argv[:3] == ['gh', 'pr', 'diff']:
            return '+scripted remote diff'
        if argv[:3] == ['gh', 'pr', 'merge']:
            assert argv[3] == '12' and '--match-head-commit' in argv
            store['writes'].append(('merge', argv))
            return 'merged (scripted)'
        raise AssertionError(argv)
    monkeypatch.setattr(service.local, 'command', command)
    return service.projects, store, repo


def scaffold_project(service, stack='python'):
    preview = service.preview({'action': 'scaffold', 'name': 'shop', 'repo': 'fixture-user/project', 'stack': stack})['data']
    return service.execute({'preview_id': preview['id'], 'confirm': preview['confirmation']})['data']


def action(service, operation):
    preview = service.preview({'name': 'shop', 'action': operation})['data']
    return service.execute({'preview_id': preview['id'], 'confirm': preview['confirmation']})['data']


@pytest.mark.parametrize('stack', ['python', 'node'])
def test_scaffold_is_runnable_tested_has_ci_and_never_changes_parent_repository(project_service, stack):
    service, store, repo = project_service
    head = service.git(repo, ['rev-parse', 'HEAD'])
    branch = service.git(repo, ['branch', '--show-current'])
    readme = (repo / 'README.md').read_bytes()
    project = scaffold_project(service, stack)
    directory = repo / project['folder']
    args = [sys.executable, '-m', 'unittest', 'discover', '-v'] if stack == 'python' else ['npm', 'test']
    result = subprocess.run(args, cwd=directory, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (directory / 'LICENSE').read_text().startswith('MIT License')
    assert 'on: [push, pull_request]' in (directory / '.github/workflows/ci.yml').read_text()
    assert service.git(repo, ['rev-parse', 'HEAD']) == head and service.git(repo, ['branch', '--show-current']) == branch
    assert (repo / 'README.md').read_bytes() == readme and not store['writes']
    assert service.listing()[0]['repository'] == 'fixture-user/project'


def test_full_project_push_tests_pr_approval_merge_without_manual_branch_or_pr_fields(project_service):
    service, store, repo = project_service
    project = scaffold_project(service)
    action(service, 'push')
    remote = service.get('shop')['remote_head']
    assert store['commits'][remote]['parents'] == ['b' * 40]  # Remote README history retained, not unrelated Git push.
    assert store['refs']['main'] == 'b' * 40
    with pytest.raises(ValueError, match='No successful test'):
        service.preview({'action': 'pr', 'name': 'shop'})
    store['checks'][remote] = [passing(remote)]  # Scripted GitHub CI only; real scaffold tests are tested above.
    action(service, 'pr')
    assert service.get('shop')['pr'] == 12
    action(service, 'merge')
    assert service.get('shop')['merged']
    assert [entry[0] for entry in store['writes']][-2:] == ['create-pr', 'merge']
    # The mapping survives construction of another service; no extra learning database.
    assert ServerState(repo, quiet=True).workbench.repositories.github.projects.get('shop')['pr'] == 12
    assert project['branch'] == 'nexus/project-shop'


def test_pr_execution_rechecks_ci_after_preview(project_service):
    service, store, _ = project_service
    scaffold_project(service)
    action(service, 'push')
    remote = service.get('shop')['remote_head']
    store['checks'][remote] = [passing(remote)]
    preview = service.preview({'action': 'pr', 'name': 'shop'})['data']
    store['checks'][remote][0]['status'] = 'queued'
    with pytest.raises(ValueError, match='pending'):
        service.execute({'preview_id': preview['id'], 'confirm': preview['confirmation']})
    assert not any(entry[0] == 'create-pr' for entry in store['writes'])


def test_existing_folder_wrong_confirmation_and_traversal_never_overwrite(project_service):
    service, store, repo = project_service
    with pytest.raises(ValueError):
        service.preview({'action': 'scaffold', 'name': '../escape', 'repo': 'fixture-user/project', 'stack': 'python'})
    preview = service.preview({'action': 'scaffold', 'name': 'shop', 'repo': 'fixture-user/project', 'stack': 'python'})['data']
    with pytest.raises(ValueError, match='Exact'):
        service.execute({'preview_id': preview['id'], 'confirm': 'yes'})
    assert not (repo / 'projects/shop').exists()
    scaffold_project(service)
    with pytest.raises(ValueError, match='already exists'):
        service.preview({'action': 'scaffold', 'name': 'shop', 'repo': 'fixture-user/project', 'stack': 'node'})
    assert not store['writes']


def test_incremental_publish_preserves_exact_contents_and_deletes_only_owned_paths(project_service):
    service, store, repo = project_service
    project = scaffold_project(service)
    action(service, 'push')
    directory = repo / project['folder']
    (directory / 'app.py').write_text('# exact no trailing newline')
    (directory / 'test_app.py').unlink()
    preview = service.preview({'action': 'commit', 'name': 'shop', 'message': 'feat: update app'})['data']
    service.execute({'preview_id': preview['id'], 'confirm': preview['confirmation']})
    action(service, 'push')
    tree = list(store['trees'].values())[-1]['tree']
    assert next(n for n in tree if n['path'] == 'app.py')['content'] == '# exact no trailing newline'
    assert next(n for n in tree if n['path'] == 'test_app.py')['sha'] is None


def test_raw_gh_writes_and_alternate_commit_producers_are_not_escape_hatches(repo, monkeypatch):
    from nexus.github_safety import validate_gh_args
    from nexus.tools import ApprovalGate, PathGuard, ToolContext, build_tools
    monkeypatch.setenv('GITHUB_TOKEN', 'fixture-scoped-credential')
    for parts in (['api', 'repos/x/y/git/commits', '-X', 'POST'], ['api', 'graphql'],
                  ['api', 'user', '--method=PATCH']):
        with pytest.raises(ValueError):
            validate_gh_args(parts, repo)
    ctx = ToolContext(repo, PathGuard(repo), ApprovalGate(mode='auto'))
    for command in ('merge', 'rebase', 'cherry-pick', 'commit-tree', 'am', 'revert', 'update-ref'):
        assert build_tools(ctx).call('git', {'args': [command]})[1]


def test_publish_secret_and_branch_drift_are_blocked_before_git_data_mutations(project_service):
    service, store, repo = project_service
    project = scaffold_project(service)
    directory = repo / project['folder']
    (directory / 'leak.txt').write_text('ghp_' + 'x' * 36)
    # Simulate a human bypass of the Nexus commit tool; publishing must still refuse.
    service.git(directory, ['add', 'leak.txt'])
    service.git(directory, ['commit', '-m', 'test: unsafe human fixture'])
    before = len(store['writes'])
    with pytest.raises(ValueError, match='Potential secret'):
        action(service, 'push')
    assert len(store['writes']) == before
    service.git(directory, ['branch', '-m', 'other-branch'])
    with pytest.raises(ValueError, match='branch changed'):
        service.preview({'action': 'push', 'name': 'shop'})


def test_generic_pr_flag_reordering_cannot_bypass_test_gate(repo, monkeypatch):
    from nexus import tools
    monkeypatch.setattr(tools, 'gh_available', lambda: True)
    monkeypatch.setattr(tools, 'run_github_command', lambda *a, **k: pytest.fail('Raw PR mutation must not run'))
    ctx = tools.ToolContext(repo, tools.PathGuard(repo), tools.ApprovalGate(mode='auto'))
    for args in (['pr', 'create'], ['pr', 'merge', '1'], ['pr', '--repo', 'x/y', 'create']):
        with pytest.raises(tools.ToolFailure, match='tested review-first'):
            tools.tool_gh(ctx, {'args': args})


def test_generated_ci_is_valid_yaml_and_runs_on_push_before_pr(project_service):
    import yaml

    from nexus.github_projects import scaffold
    for stack in ('python', 'node'):
        workflow = yaml.safe_load(scaffold('project', stack)['.github/workflows/ci.yml'])
        # PyYAML's YAML 1.1 parser interprets the key `on` as boolean True.
        assert workflow.get('on', workflow.get(True)) == ['push', 'pull_request']
        assert workflow['permissions'] == {'contents': 'read'}
        assert workflow['jobs']['tests']['steps'][-1]['run'] in {'npm test', 'python -m unittest discover -v'}
