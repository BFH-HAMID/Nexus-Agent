"""All remote APIs are scripted. No real GitHub mutations or cloned projects."""
import base64
import hashlib
import json
from urllib.parse import unquote

import pytest

from nexus.agent import NexusAgent
from nexus.server import ServerState
from nexus.tools import ApprovalDecision
from nexus.workbench import observation


def sha(text):
    return hashlib.sha1(text.encode()).hexdigest()


@pytest.fixture()
def direct(repo, monkeypatch):
    state = ServerState(repo, quiet=True)
    github = state.workbench.repositories.github
    tasks = github.tasks
    monkeypatch.setattr(github, 'record', lambda ident: {'repository': ident})
    actor = {'login': 'fixture-user', 'write': True, 'approve': True, 'race': False, 'truncated': False}
    monkeypatch.setattr(github, 'viewer', lambda: actor['login'])
    monkeypatch.setattr(github, 'metadata', lambda slug: {'full_name': slug, 'default_branch': 'main', 'permissions': {'push': actor['write']}})
    content = '# Remote project\n'
    blob = sha(content)
    nodes = [{'path': 'README.md', 'mode': '100644', 'type': 'blob', 'sha': blob, 'size': len(content)}]
    tree_sha = sha('base-tree')
    base = sha('base-commit')
    store = {'refs': {'main': base}, 'commits': {base: {'sha': base, 'tree': {'sha': tree_sha}, 'parents': []}},
             'trees': {tree_sha: nodes}, 'blobs': {blob: content}, 'calls': [], 'approvals': []}
    def transport(args, timeout=30):
        assert args[:2] == ['gh', 'api'], 'Remote task must never run local Git or shell'
        endpoint = args[2]
        method = args[args.index('--method') + 1] if '--method' in args else 'GET'
        if '--input' in args:
            from pathlib import Path
            body = json.loads(Path(args[args.index('--input') + 1]).read_text())
        else:
            body = None
        store['calls'].append((method, endpoint, body))
        route = endpoint.removeprefix('repos/example/project/')
        if route.startswith('branches/'):
            result = {'commit': {'sha': store['refs'][unquote(route[9:])]}}
        elif route == 'git/refs' and method == 'POST':
            branch = body['ref'].removeprefix('refs/heads/')
            if branch in store['refs']:
                raise ValueError('Reference already exists')
            store['refs'][branch] = body['sha']
            result = {'object': {'sha': body['sha']}}
        elif route.startswith('git/ref/heads/'):
            result = {'object': {'sha': store['refs'][unquote(route[14:])]}}
        elif route.startswith('git/refs/heads/'):
            branch = unquote(route[15:])
            commit = store['commits'][body['sha']]
            assert body['force'] is False
            if actor['race'] or commit['parents'] != [store['refs'][branch]]:
                raise ValueError('Not a fast forward (scripted GitHub protection)')
            store['refs'][branch] = body['sha']
            result = {'object': {'sha': body['sha']}}
        elif route == 'git/blobs':
            ident = sha(body['content'])
            store['blobs'][ident] = body['content']
            result = {'sha': ident}
        elif route.startswith('git/blobs/'):
            text = store['blobs'][route[10:]]
            result = {'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()}
        elif route == 'git/trees':
            nodes = {n['path']: dict(n) for n in store['trees'][body['base_tree']]}
            for entry in body['tree']:
                if entry['sha'] is None:
                    nodes.pop(entry['path'])
                else:
                    nodes[entry['path']] = {**entry, 'size': len(store['blobs'][entry['sha']])}
            result_nodes = list(nodes.values())
            ident = sha(json.dumps(result_nodes))
            store['trees'][ident] = result_nodes
            result = {'sha': ident}
        elif route.startswith('git/trees/'):
            ident = route[10:].split('?')[0]
            result = {'tree': store['trees'][ident], 'truncated': actor['truncated']}
        elif route == 'git/commits':
            ident = sha(json.dumps(body))
            result = {'sha': ident, 'tree': {'sha': body['tree']}, 'parents': body['parents']}
            store['commits'][ident] = result
        elif route.startswith('git/commits/'):
            result = store['commits'][route[12:]]
        elif route.startswith('pulls?'):
            result = []
        else:
            raise AssertionError((route, method, body))
        return json.dumps(result)
    monkeypatch.setattr(github, 'command', transport)
    session = state.create('Implement a remote feature', 'general', False,
        options=state.workbench.prepare({'github_repo': 'example/project'}))
    def approval(session):
        def decide(spec, args, risk, preview):
            store['approvals'].append((spec.name, tasks.proposal(session, spec.name, args)))
            return ApprovalDecision(actor['approve'], reason='scripted permission')
        return decide
    monkeypatch.setattr(state, '_approval', approval)
    return tasks, session, actor, store, repo


def tools(direct):
    tasks, session, _, _, repo = direct
    record = tasks.start(session)
    agent = NexusAgent(workspace=repo, auto_yes=False, approval_callback=tasks.gh.local.state._approval(session))
    tasks.attach(agent, session, record)
    ctx, registry, _ = agent.tools_for(agent.config.role('general'))
    return ctx, registry


def test_selected_repo_gets_new_remote_branch_after_permission_only(direct):
    tasks, session, _, store, repo = direct
    before = (repo / 'README.md').read_bytes()
    record = tasks.start(session)
    assert record['branch'] == 'nexus/task-' + session.task_id
    assert record['created'] and store['refs'][record['branch']] == record['base_sha']
    assert store['approvals'][0][0] == 'github_task_branch'
    assert all(method == 'GET' for method, _, _ in store['calls'][:-1])
    assert (repo / 'README.md').read_bytes() == before


def test_denied_branch_permission_has_no_remote_mutation(direct):
    tasks, session, actor, store, _ = direct
    actor['approve'] = False
    with pytest.raises(ValueError, match='permission denied'):
        tasks.start(session)
    assert list(store['refs']) == ['main']
    assert not any(method != 'GET' for method, _, _ in store['calls'])


def test_remote_tools_cannot_escape_to_local_shell_or_filesystem(direct):
    _, registry = tools(direct)
    assert {'list_files', 'read_file', 'write_file', 'edit_file', 'delete_file', 'finish'} == set(registry.names())
    assert not registry.has('run_command') and not registry.has('git')
    output, error = registry.call('read_file', {'path': 'README.md'})
    assert not error and output == '# Remote project'


def test_approved_remote_edit_is_actual_nonforce_commit_and_local_file_untouched(direct):
    tasks, session, _, store, repo = direct
    before = (repo / 'README.md').read_bytes()
    ctx, registry = tools(direct)
    output, error = registry.call('edit_file', {'path': 'README.md', 'old_text': 'Remote project', 'new_text': 'Real remote change'})
    assert not error, output
    record = tasks.get(session.task_id)
    assert 'Committed README.md to GitHub' in output
    assert record['head'] == store['refs'][record['branch']] and record['head'] != record['base_sha']
    assert tasks.read(record, 'README.md')[0] == '# Real remote change\n'
    assert ctx.state['changed_files'] == {'README.md'}
    assert (repo / 'README.md').read_bytes() == before
    assert store['refs']['main'] == record['base_sha']
    approval = store['approvals'][-1][1]
    assert '-# Remote project' in approval['diff'] and '+# Real remote change' in approval['diff']
    assert all(body.get('force') is False for method, endpoint, body in store['calls'] if method == 'PATCH')


def test_create_and_delete_are_remote_only(direct):
    tasks, session, _, _, repo = direct
    _, registry = tools(direct)
    output, error = registry.call('write_file', {'path': 'src/new.txt', 'content': 'actual remote file\n'})
    assert not error, output
    assert tasks.read(tasks.get(session.task_id), 'src/new.txt')[0] == 'actual remote file\n'
    assert not (repo / 'src/new.txt').exists()
    output, error = registry.call('delete_file', {'path': 'src/new.txt'})
    assert not error, output
    assert tasks.read(tasks.get(session.task_id), 'src/new.txt')[0] is None


def test_denied_edit_does_not_commit(direct):
    tasks, session, actor, store, _ = direct
    _, registry = tools(direct)
    before = tasks.get(session.task_id)['head']
    actor['approve'] = False
    output, error = registry.call('write_file', {'path': 'README.md', 'content': 'denied content'})
    assert error and 'DENIED' in output
    assert store['refs'][tasks.get(session.task_id)['branch']] == before


def test_concurrent_branch_advance_is_never_force_overwritten(direct):
    tasks, session, actor, store, _ = direct
    _, registry = tools(direct)
    before = tasks.get(session.task_id)['head']
    actor['race'] = True
    output, error = registry.call('write_file', {'path': 'README.md', 'content': 'approved but raced'})
    assert error and 'Not a fast forward' in output
    record = tasks.get(session.task_id)
    assert record['head'] == before and store['refs'][record['branch']] == before


@pytest.mark.parametrize('path', ['../outside', '/root.txt', 'C:\\file', '.git/config', '.env', 'folder/.env.local', '.ssh/key', 'a/../b'])
def test_remote_paths_are_safe(direct, path):
    tasks, _, _, _, _ = direct
    with pytest.raises(ValueError):
        tasks.path(path)


def test_changed_github_identity_cannot_continue_a_task(direct):
    tasks, _, actor, _, _ = direct
    _, registry = tools(direct)
    actor['login'] = 'another-account'
    output, error = registry.call('read_file', {'path': 'README.md'})
    assert error and 'account changed' in output


def test_read_only_task_never_creates_branch_or_exposes_writes(direct):
    tasks, session, _, store, _ = direct
    session.read_only = True
    _, registry = tools(direct)
    assert tasks.get(session.task_id)['created'] is False
    assert list(store['refs']) == ['main']
    assert 'write_file' not in registry.names() and not store['approvals']


def test_isolation_refuses_remote_task_before_mutation(direct):
    tasks, session, _, store, _ = direct
    settings = tasks.studio.settings()
    settings['isolation']['enabled'] = True
    tasks.studio.settings = lambda: settings
    with pytest.raises(ValueError, match='isolation'):
        tasks.start(session)
    assert not any(method != 'GET' for method, _, _ in store['calls'])


def test_truncated_remote_tree_is_not_used_as_complete_evidence(direct):
    _, _, actor, _, _ = direct
    _, registry = tools(direct)
    actor['truncated'] = True
    output, error = registry.call('list_files', {})
    assert error and 'truncated' in output


def test_task_pr_and_merge_use_bound_task_metadata_and_approval(direct, monkeypatch):
    tasks, session, _, _, _ = direct
    _, registry = tools(direct)
    assert registry.call('write_file', {'path': 'README.md', 'content': 'change'})[1] is False
    captured = []
    def preview(payload):
        captured.append(payload)
        data = {'id': sha(str(payload)), 'action': payload['action'], 'confirmation': 'server-owned task confirmation'}
        tasks.gh.previews[data['id']] = data
        return observation(data, 'scripted preview')
    def execute(payload):
        tasks.gh.previews.pop(payload['preview_id'])
        return observation({'output': 'https://github.com/example/project/pull/42'}, 'scripted command')
    monkeypatch.setattr(tasks.gh, 'preview', preview)
    monkeypatch.setattr(tasks.gh, 'execute', execute)
    payload = {'task_id': session.task_id, 'repo': 'example/project', 'action': 'create-pr'}
    reviewed = tasks.preview_action(payload)['data']
    assert captured[-1]['head'] == 'nexus/task-' + session.task_id and captured[-1]['base'] == 'main'
    assert 'Tests were not run locally' in captured[-1]['body']
    with pytest.raises(ValueError, match='Explicit approval'):
        tasks.execute_action({'preview_id': reviewed['id'], 'approved': False})
    tasks.execute_action({'preview_id': reviewed['id'], 'approved': True})
    assert tasks.get(session.task_id)['pr'] == 42
    reviewed = tasks.preview_action({**payload, 'action': 'merge'})['data']
    assert captured[-1]['number'] == 42
    tasks.execute_action({'preview_id': reviewed['id'], 'approved': True})
    assert tasks.get(session.task_id)['merged'] is True
    with pytest.raises(ValueError):
        tasks.execute_action({'preview_id': reviewed['id'], 'approved': True})


def test_task_action_refuses_cross_repository_target(direct):
    tasks, session, _, _, _ = direct
    tasks.start(session)
    with pytest.raises(ValueError, match='does not match'):
        tasks.preview_action({'task_id': session.task_id, 'repo': 'other/project', 'action': 'create-pr'})


def test_full_server_run_uses_remote_tools_even_when_auto_yes_was_selected(direct, monkeypatch):
    from nexus import server as server_module
    from tests.conftest import FakeProvider, fake_registry, finish_completion, tool_completion
    tasks, session, _, store, repo = direct
    before = (repo / 'README.md').read_bytes()
    session.auto_yes = True
    def factory(config, **kwargs):
        name = config.agent_for_role(config.role('general')).name
        provider = FakeProvider(name, [
            tool_completion([('read_file', {'path': 'README.md'})]),
            tool_completion([('edit_file', {'path': 'README.md', 'old_text': 'Remote project', 'new_text': 'Verified remote task edit'})]),
            finish_completion('Edited the remote file. No local tests were run.'),
        ])
        return NexusAgent(config, providers=fake_registry(provider, config=config), **kwargs)
    monkeypatch.setattr(server_module, 'NexusAgent', factory)
    tasks.gh.local.state._run(session)
    assert session.result['ok'], session.result
    assert session.result['github_task']['repository'] == 'example/project'
    assert session.result['changed_files'] == ['README.md']
    assert session.result['execution']['workspace'].startswith('GitHub example/project:nexus/task-')
    assert [name for name, _ in store['approvals']] == ['github_task_branch', 'edit_file']
    assert (repo / 'README.md').read_bytes() == before
    assert tasks.read(tasks.get(session.task_id), 'README.md')[0] == '# Verified remote task edit\n'
    assert tasks.studio.find('evidence', 'evidence-' + session.task_id)['result']['github_task']['created']
    assert not any('evidence/history update failed' in e.get('message', '') for e in session.log)


def test_stale_approved_edit_cannot_modify_an_advanced_task_branch(direct):
    tasks, session, _, store, _ = direct
    ctx, _ = tools(direct)
    args = {'path': 'README.md', 'content': 'reviewed change'}
    tasks.proposal(session, 'write_file', args)
    record = tasks.get(session.task_id)
    store['refs'][record['branch']] = sha('external concurrent commit')
    calls = len(store['calls'])
    with pytest.raises(Exception, match='advanced since review'):
        tasks.mutate(session, ctx, 'write_file', args)
    assert all(method == 'GET' for method, _, _ in store['calls'][calls:])


def test_policy_revocation_after_file_approval_blocks_remote_commit(direct):
    tasks, session, _, store, _ = direct
    ctx, _ = tools(direct)
    args = {'path': 'README.md', 'content': 'approved before revocation'}
    tasks.proposal(session, 'write_file', args)
    settings = tasks.studio.settings()
    settings['role_permissions']['general'] = {'network': False}
    tasks.studio.settings = lambda: settings
    calls = len(store['calls'])
    with pytest.raises(ValueError, match='Role permissions'):
        tasks.mutate(session, ctx, 'write_file', args)
    assert len(store['calls']) == calls


def test_failed_remote_edit_is_recorded_as_blocker_until_a_successful_retry(direct):
    _, _, actor, _, _ = direct
    ctx, registry = tools(direct)
    actor['race'] = True
    output, error = registry.call('write_file', {'path': 'README.md', 'content': 'approved raced write'})
    assert error and 'Not a fast forward' in output
    assert 'GitHub edit failed' in ctx.state['last_remote_error']
    actor['race'] = False
    output, error = registry.call('write_file', {'path': 'README.md', 'content': 'approved recovery'})
    assert not error, output
    assert 'last_remote_error' not in ctx.state


def test_remote_secret_scan_cannot_be_disabled_by_studio_preference(direct):
    tasks, session, _, store, _ = direct
    _, registry = tools(direct)
    tasks.studio.save_settings({'secret_scan': False})
    before = len(store['calls'])
    output, error = registry.call('write_file', {'path': 'config.txt', 'content': 'ghp_' + 'x' * 36})
    assert error and 'DENIED' in output
    assert 'ghp_' + 'x' * 36 not in output
    assert all(method == 'GET' for method, _, _ in store['calls'][before:])


def test_remote_cached_edit_is_rescanned_immediately_before_blob_creation(direct):
    tasks, session, _, store, _ = direct
    ctx, _ = tools(direct)
    args = {'path': 'config.txt', 'content': 'clean configuration'}
    tasks.proposal(session, 'write_file', args)
    tasks.pending[tasks.key(session.task_id, 'write_file', args)][1]['after'] = 'ghp_' + 'z' * 36
    before = len(store['calls'])
    with pytest.raises(ValueError, match='Potential secret'):
        tasks.mutate(session, ctx, 'write_file', args)
    assert all(method == 'GET' for method, _, _ in store['calls'][before:])


def test_remote_generated_commit_message_is_conventional(direct):
    _, registry = tools(direct)
    _, error = registry.call('write_file', {'path': 'config.txt', 'content': 'clean configuration'})
    assert not error
    _, _, _, store, _ = direct
    messages = [body['message'] for method, endpoint, body in store['calls'] if endpoint.endswith('/git/commits') and method == 'POST']
    assert messages and messages[0].startswith('chore(nexus): write_file remote file\n')
