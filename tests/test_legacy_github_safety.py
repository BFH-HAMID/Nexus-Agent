"""Typed legacy tools: real disposable staged blobs; scripted GitHub transport."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from nexus import tools
from nexus.github_safety import GitHubSafetyError, scan_index, validate_gh_args
from nexus.live_process import stream_shell
from nexus.tools import ApprovalDecision, ApprovalGate, PathGuard, ToolContext, build_tools
from nexus.types import Event, EventType


def git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo).decode().strip()


def context(repo, events=None):
    return ToolContext(workspace=repo, guard=PathGuard(repo), gate=ApprovalGate(mode='auto'),
                       emit=(events.append if events is not None else lambda event: None))


@pytest.fixture(autouse=True)
def isolated_tokens(monkeypatch):
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    monkeypatch.delenv('GH_TOKEN', raising=False)


def test_staged_secret_is_detected_even_if_worktree_is_clean(repo):
    (repo / 'chosen.txt').write_text('ghp_' + 'x' * 36)
    git(repo, 'add', 'chosen.txt')
    (repo / 'chosen.txt').write_text('clean worktree replacement')
    with pytest.raises(GitHubSafetyError, match='Potential secret'):
        scan_index(repo)


def test_staged_clean_blob_is_not_confused_with_dirty_worktree(repo):
    (repo / 'chosen.txt').write_text('clean staged content')
    git(repo, 'add', 'chosen.txt')
    (repo / 'chosen.txt').write_text('ghp_' + 'x' * 36)
    assert scan_index(repo) == ['chosen.txt']


@pytest.mark.parametrize('path,content', [('.env', 'opaque'), ('binary.bin', b'\xff\x00'), ('large.txt', b'x' * 2_000_001)])
def test_index_scan_fails_closed_on_private_binary_and_oversized_blobs(repo, path, content):
    (repo / path).write_bytes(content.encode() if isinstance(content, str) else content)
    git(repo, 'add', '-f', '--', path)
    with pytest.raises((GitHubSafetyError, ValueError)):
        scan_index(repo)


def test_index_scan_refuses_staged_symlinks(repo):
    (repo / 'link.txt').symlink_to('README.md')
    git(repo, 'add', 'link.txt')
    with pytest.raises(GitHubSafetyError, match='regular staged'):
        scan_index(repo)


def test_deleting_an_existing_secret_is_allowed(repo):
    (repo / 'legacy.txt').write_text('ghp_' + 'x' * 36)
    git(repo, 'add', 'legacy.txt')
    git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'test: legacy fixture')
    git(repo, 'rm', 'legacy.txt')
    assert scan_index(repo) == []


def test_typed_commit_blocks_staged_secret_without_creating_commit(repo):
    (repo / 'chosen.txt').write_text('GITHUB_TOKEN=opaque-fixture-credential')
    git(repo, 'add', 'chosen.txt')
    before = git(repo, 'rev-parse', 'HEAD')
    output, error = build_tools(context(repo)).call('git', {'args': ['commit', '-m', 'feat: add configuration']})
    assert error and 'Potential secret' in output and git(repo, 'rev-parse', 'HEAD') == before


def test_git_prepare_scans_all_preexisting_staged_content(repo):
    (repo / 'chosen.txt').write_text('ghp_' + 'z' * 36)
    git(repo, 'add', 'chosen.txt')
    (repo / 'chosen.txt').write_text('clean working content')
    # git_prepare stages requested paths only, but commits the entire index.
    before = git(repo, 'rev-parse', 'HEAD')
    output, error = build_tools(context(repo)).call('git_prepare', {'task': 'Add readme update', 'paths': ['README.md']})
    assert error and 'Potential secret' in output and git(repo, 'rev-parse', 'HEAD') == before


@pytest.mark.parametrize('args', [['commit', '-am', 'feat: hidden staging'], ['commit', '--amend'], ['commit', '-m', 'unconventional']])
def test_typed_commit_rejects_implicit_staging_or_unknown_message_sources(repo, args):
    output, error = build_tools(context(repo)).call('git', {'args': args})
    assert error


def test_real_typed_commit_preserves_clean_staged_content(repo):
    git(repo, 'config', 'user.name', 'Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    (repo / 'chosen.txt').write_text('actual code')
    git(repo, 'add', 'chosen.txt')
    output, error = build_tools(context(repo)).call('git', {'args': ['commit', '-m', 'feat: add real file']})
    assert not error, output
    assert git(repo, 'show', 'HEAD:chosen.txt') == 'actual code'


def test_opaque_token_in_arguments_is_refused_before_any_approval_or_args_event(repo, monkeypatch):
    token = 'opaque-fixture-credential'
    monkeypatch.setenv('GITHUB_TOKEN', token)
    events, approvals = [], []
    ctx = context(repo, events)
    ctx.gate = ApprovalGate(callback=lambda *args: approvals.append(args) or ApprovalDecision(True))
    output, error = build_tools(ctx).call('gh', {'args': ['issue', 'create', '--body', token]})
    assert error and 'before approval' in output and not approvals
    assert token not in json.dumps([event.data for event in events])


@pytest.mark.parametrize('parts', [
    ['auth', 'token'], ['alias', 'set', 'x', '!echo unsafe'], ['extension', 'exec', 'x'],
    ['api', 'https://evil.invalid/user'], ['api', '//evil.invalid/user'], ['api', 'user', '--hostname=evil.invalid'],
    ['api', 'user', '--header', 'Authorization: replacement'], ['api', 'user', '--input', '.env'],
    ['api', 'repos/x/y/contents/x', '-F', 'content=@.env'],
    ['pr', 'create', '-F', '.env'], ['pr', 'create', '--body-file=.env'],
    ['pr', 'merge', '1', '--auto'], ['pr', 'merge', '1', '--admin'],
    ['pr', 'view', 'https://evil.invalid/x/y/pull/1'], ['pr', 'list', '--repo', 'evil.invalid/x/y'],
    ['repo', 'create', '--push'], ['release', 'upload', 'v1', '.env'],
])
def test_generic_gh_cannot_override_host_read_credentials_or_expand_files(repo, monkeypatch, parts):
    monkeypatch.setenv('GITHUB_TOKEN', 'scoped-fixture-credential')
    with pytest.raises(GitHubSafetyError):
        validate_gh_args(parts, repo)


def test_gh_body_links_are_data_not_transport_targets(repo, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'scoped-fixture-credential')
    validate_gh_args(['pr', 'create', '--repo', 'owner/repo', '--body', 'See https://docs.example.com/guide'], repo)


def test_missing_token_fails_before_spawning_gh_even_if_gh_token_exists(repo, monkeypatch):
    monkeypatch.setenv('GH_TOKEN', 'not-accepted-fixture-credential')
    monkeypatch.setattr('nexus.live_process.stream_shell', lambda *a, **k: pytest.fail('No gh child without GITHUB_TOKEN'))
    with pytest.raises(tools.ToolFailure, match='fallback is disabled'):
        tools.run_github_command(context(repo), ['api', 'user'])


def test_gh_uses_argv_child_env_and_withholds_chunked_secret_output(repo, monkeypatch):
    token = 'opaque-fixture-credential'
    monkeypatch.setenv('GITHUB_TOKEN', token)
    monkeypatch.setenv('GH_TOKEN', 'wrong-fixture-credential')
    events = []
    def stream(command, cwd, **kwargs):
        assert command == ['gh', 'api', 'user'] and isinstance(command, list)
        assert kwargs['env']['GH_TOKEN'] == token
        kwargs['emit'](Event(EventType.PROCESS, {'status': 'running', 'command': command}))
        for piece in (token[:10], token[10:]):
            kwargs['emit'](Event(EventType.TOOL_OUTPUT, {'text': piece}))
        return token + '\n[exit 0]', 0
    monkeypatch.setattr('nexus.live_process.stream_shell', stream)
    output, code = tools.run_github_command(context(repo, events), ['api', 'user'])
    assert code == 0 and token not in output and '[REDACTED_SECRET]' in output
    assert len(events) == 1 and events[0].type == EventType.PROCESS
    assert token not in json.dumps([e.data for e in events])


def test_gh_truncated_output_is_not_returned_as_a_partial_secret(repo, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'opaque-fixture-credential')
    monkeypatch.setattr('nexus.live_process.stream_shell', lambda *a, **k: ('opaque-fixture-\n[output capped at 1024 characters]', 0))
    output, code = tools.run_github_command(context(repo), ['api', 'user'])
    assert code == 1 and 'opaque-fixture-' not in output


def test_scalar_git_metadata_does_not_include_exit_markers(repo):
    git(repo, 'remote', 'add', 'origin', 'https://github.com/owner/project.git')
    assert tools.repo_slug(context(repo)) == 'owner/project'
    assert tools.default_branch(context(repo)) == git(repo, 'branch', '--show-current')
    assert tools.git_scalar('unsafe\nsecond-line\n[exit 0]') == ''


def test_list_argv_process_preserves_shell_metacharacters_as_data(repo):
    events = []
    payload = 'literal ; $(not-a-command) & < > |'
    output, code = stream_shell([sys.executable, '-c', 'import sys;print(sys.argv[1])', payload],
                                Path(repo), timeout=5, emit=events.append, env={})
    assert code == 0 and payload in output


def test_precancelled_command_does_not_spawn_a_child(repo, monkeypatch):
    import threading
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Cancelled command must not launch'))
    with pytest.raises(RuntimeError, match='before launch'):
        stream_shell([sys.executable, '-c', 'print(1)'], repo, timeout=5, emit=lambda e: None, cancel=cancel)


def test_gh_explicit_repo_does_not_inherit_other_context(repo, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'opaque-fixture-credential')
    monkeypatch.setenv('GH_REPO', 'enterprise.invalid/owner/private')
    def stream(command, cwd, **kwargs):
        assert 'GH_REPO' not in kwargs['env'] and kwargs['env']['GH_HOST'] == 'github.com'
        return '[]', 0
    monkeypatch.setattr('nexus.live_process.stream_shell', stream)
    assert tools.run_github_command(context(repo), ['pr', 'list', '--repo', 'owner/project']) == ('[]', 0)


def test_gh_cannot_infer_an_enterprise_account_from_local_origin(repo, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'opaque-fixture-credential')
    git(repo, 'remote', 'add', 'origin', 'https://enterprise.invalid/owner/private.git')
    monkeypatch.setattr('nexus.live_process.stream_shell', lambda *a, **k: pytest.fail('Wrong host must not launch'))
    with pytest.raises(tools.ToolFailure, match='github.com origin'):
        tools.run_github_command(context(repo), ['pr', 'list'])


def test_gh_failed_command_withholds_provider_output_and_keeps_exit_code(repo, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'opaque-fixture-credential')
    monkeypatch.setattr('nexus.live_process.stream_shell', lambda *a, **k: ('opaque-fixture-credential', 23))
    ctx = context(repo)
    output, code = tools.run_github_command(ctx, ['api', 'user'])
    assert code == ctx.state['last_exit_code'] == 23
    assert 'opaque-fixture-credential' not in output and 'withheld' in output


@pytest.mark.parametrize('name,args', [('gh', {'args': ['issue', 'create', '--title', 'Follow-up']}),
    ('gh_create_pr', {'title': 'Follow-up'}), ('gh_merge_pr', {'number': 1}),
    ('git', {'args': ['push', 'origin', 'feature']}), ('git_prepare', {'task': 'Fix', 'push': True}),
    ('delete_file', {'path': 'obsolete.txt'})])
def test_external_or_destructive_typed_calls_never_inherit_yes_or_persistent_approval(repo, name, args):
    from nexus.types import ToolSpec
    gate = ApprovalGate(mode='auto', allow_dangerous=True, always_allow=[name])
    gate.session_allowed.add(name)
    registry = build_tools(context(repo))
    spec = registry._tools[name].spec
    assert isinstance(spec, ToolSpec)
    assert not gate.check(spec, args).approved
    approvals = []
    gate.callback = lambda *a: approvals.append(a) or ApprovalDecision(True, scope='always')
    assert gate.check(spec, args).approved and gate.check(spec, args).approved
    assert len(approvals) == 2


def test_readonly_gh_queries_do_not_get_misclassified_as_mutations(repo):
    registry = build_tools(context(repo))
    gate = ApprovalGate(mode='auto')
    assert gate.check(registry._tools['gh'].spec, {'args': ['pr', 'list']}).approved
