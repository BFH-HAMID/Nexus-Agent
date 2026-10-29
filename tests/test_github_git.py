"""Real disposable Git object scans, scripted transport; no external requests."""
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from nexus.github_git import git_environment, transport, typed_transport
from nexus.github_safety import GitHubSafetyError


def git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo).decode().strip()


@pytest.fixture()
def project(repo, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'opaque-fixture-credential')
    git(repo, 'remote', 'add', 'origin', 'https://github.com/fixture/project.git')
    git(repo, 'branch', '-m', 'feature')
    git(repo, 'config', 'user.name', 'Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    return repo


def scripted(repo, remote=None, default='main'):
    calls = []
    def execute(argv, env):
        assert 'opaque-fixture-credential' not in json.dumps(argv)
        assert env['GH_TOKEN'] == env['GITHUB_TOKEN'] == 'opaque-fixture-credential'
        assert env['GIT_CONFIG_NOSYSTEM'] == '1' and env['GIT_CONFIG_GLOBAL'] == os.devnull
        assert 'SSH_AUTH_SOCK' not in env and 'GIT_SSH_COMMAND' not in env
        hooks = next(arg.split('=', 1)[1] for arg in argv if arg.startswith('core.hooksPath='))
        assert Path(hooks).is_dir() and list(Path(hooks).iterdir()) == []
        assert 'credential.helper=' in argv and 'credential.helper=!gh auth git-credential' in argv
        assert 'http.followRedirects=false' in argv and 'http.sslVerify=true' in argv
        assert '--force' not in argv and '--force-with-lease' not in argv
        calls.append(argv)
        if 'ls-remote' in argv:
            if '--symref' in argv:
                return f'ref: refs/heads/{default}\tHEAD\n' + git(repo, 'rev-parse', 'HEAD') + '\tHEAD\n', 0
            return (remote + '\t' + argv[-1] + '\n' if remote else ''), 0
        assert 'push' in argv
        return '', 0
    return calls, execute


def test_push_uses_canonical_https_immutable_commit_and_token_only_environment(project, monkeypatch):
    monkeypatch.setenv('GH_TOKEN', 'wrong-fixture-credential')
    monkeypatch.setenv('SSH_AUTH_SOCK', '/wrong/agent')
    monkeypatch.setenv('GIT_SSH_COMMAND', 'do-not-execute')
    monkeypatch.setenv('HTTPS_PROXY', 'https://wrong.invalid')
    git(project, 'branch', '-m', 'feature/work')
    calls, execute = scripted(project)
    result = transport(project, 'push', 'feature/work', execute=execute)
    head = git(project, 'rev-parse', 'HEAD')
    assert calls[-1][-4:] == ['push', '--porcelain', 'https://github.com/fixture/project.git', head + ':refs/heads/feature/work']
    assert 'Pushed scanned commit ' + head in result
    assert os.environ['GH_TOKEN'] == 'wrong-fixture-credential'


def test_lookup_is_readonly_and_branch_scoped(project):
    head = git(project, 'rev-parse', 'HEAD')
    calls, execute = scripted(project, head)
    result = transport(project, 'ls-remote', 'feature', execute=execute)
    assert result == head + '\trefs/heads/feature\n' and len(calls) == 1


@pytest.mark.parametrize('branch', ['main', 'master', '--all', 'a..b', '../escape', 'x.lock', 'a//b', 'a/.hidden', 'a b'])
def test_protected_or_invalid_push_target_never_connects(project, branch):
    with pytest.raises(GitHubSafetyError):
        transport(project, 'push', branch, execute=lambda *a: pytest.fail('Refused before network'))


def test_remote_default_branch_is_protected_even_when_named_trunk(project):
    git(project, 'branch', '-m', 'trunk')
    calls, execute = scripted(project, default='trunk')
    with pytest.raises(GitHubSafetyError, match='Default branch push'):
        transport(project, 'push', 'trunk', execute=execute)
    assert not any('push' in argv for argv in calls)


@pytest.mark.parametrize('url', ['git@github.com:fixture/project.git', 'https://evil.invalid/fixture/project.git',
    'https://user:credential@github.com/fixture/project.git'])
def test_ssh_custom_hosts_and_embedded_credentials_fail_closed(project, url):
    git(project, 'remote', 'set-url', 'origin', url)
    with pytest.raises(GitHubSafetyError, match='HTTPS'):
        transport(project, 'push', 'feature', execute=lambda *a: pytest.fail('Unsafe origin must not connect'))


@pytest.mark.parametrize('name,value', [
    ('url.https://evil.invalid/.insteadOf', 'https://github.com/'),
    ('url.https://evil.invalid/.pushInsteadOf', 'https://github.com/'),
    ('remote.origin.pushurl', 'https://evil.invalid/repo'),
    ('credential.helper', '!do-not-execute'), ('http.extraHeader', 'Authorization: unsafe-value'),
    ('http.proxy', 'https://evil.invalid'), ('include.path', '/nonexistent/extra-config'),
])
def test_ambient_local_transport_overrides_are_rejected_without_exposing_values(project, name, value):
    git(project, 'config', '--local', name, value)
    with pytest.raises(GitHubSafetyError) as error:
        transport(project, 'push', 'feature', execute=lambda *a: pytest.fail('Unsafe configuration must not connect'))
    assert value not in str(error.value)


def test_worktree_transport_config_is_also_screened(project):
    git(project, 'config', 'extensions.worktreeConfig', 'true')
    git(project, 'config', '--worktree', 'url.https://evil.invalid/.insteadOf', 'https://github.com/')
    with pytest.raises(GitHubSafetyError, match='overrides'):
        transport(project, 'push', 'feature', execute=lambda *a: pytest.fail('Worktree override must not connect'))


def test_historical_secret_blocks_push_even_if_latest_snapshot_removes_it(project):
    (project / 'legacy.txt').write_text('ghp_' + 'x' * 36)
    git(project, 'add', 'legacy.txt')
    git(project, 'commit', '-m', 'test: historical fixture')
    git(project, 'rm', 'legacy.txt')
    git(project, 'commit', '-m', 'fix: remove legacy fixture')
    calls, execute = scripted(project)
    with pytest.raises(GitHubSafetyError, match='Potential secret'):
        transport(project, 'push', 'feature', execute=execute)
    assert not any('push' in argv for argv in calls)


def test_known_remote_base_excludes_preexisting_remote_history(project):
    base = git(project, 'rev-parse', 'HEAD')
    (project / 'new.txt').write_text('clean new code')
    git(project, 'add', 'new.txt')
    git(project, 'commit', '-m', 'feat: new code')
    calls, execute = scripted(project, base)
    assert 'Pushed scanned commit' in transport(project, 'push', 'feature', execute=execute)
    assert any('push' in argv for argv in calls)


def test_unavailable_remote_base_does_not_trigger_implicit_fetch_or_push(project):
    calls, execute = scripted(project, 'a' * 40)
    with pytest.raises(GitHubSafetyError, match='inspect local Git'):
        transport(project, 'push', 'feature', execute=execute)
    assert not any('push' in argv or 'fetch' in argv for argv in calls)


def test_outbound_binary_and_private_paths_are_refused(project):
    (project / 'binary.dat').write_bytes(b'\x00\xff')
    git(project, 'add', 'binary.dat')
    git(project, 'commit', '-m', 'test: binary fixture')
    calls, execute = scripted(project)
    with pytest.raises(GitHubSafetyError, match='binary'):
        transport(project, 'push', 'feature', execute=execute)
    assert not any('push' in argv for argv in calls)


def test_cancellation_before_dispatch_does_not_connect(project):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(GitHubSafetyError, match='cancelled'):
        transport(project, 'push', 'feature', cancel=cancel, execute=lambda *a: pytest.fail('No launch'))


def test_network_failure_has_no_credential_output_and_no_retry(project):
    calls = []
    def fail(argv, env):
        calls.append(argv)
        return env['GITHUB_TOKEN'], 128
    with pytest.raises(GitHubSafetyError) as error:
        transport(project, 'push', 'feature', execute=fail)
    assert 'opaque-fixture-credential' not in str(error.value) and len(calls) == 1


@pytest.mark.parametrize('parts', [['push', '--force', 'origin', 'feature'], ['push', 'other', 'feature'],
    ['push', 'origin', 'HEAD:main'], ['push', '--all'], ['fetch', 'origin']])
def test_typed_transport_does_not_accept_arbitrary_flags_remotes_or_refspecs(project, parts):
    with pytest.raises(GitHubSafetyError):
        typed_transport(project, parts, execute=lambda *a: pytest.fail('Invalid invocation'))


def test_environment_discards_git_injection_and_proxy_settings(project, monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'http.extraHeader')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', 'Authorization: unsafe')
    monkeypatch.setenv('https_proxy', 'https://evil.invalid')
    env = git_environment(project)
    assert 'GIT_CONFIG_COUNT' not in env and 'GIT_CONFIG_KEY_0' not in env and 'https_proxy' not in env


def test_mismatched_local_branch_cannot_silently_push_head_to_another_branch(project):
    with pytest.raises(GitHubSafetyError, match='current local branch'):
        transport(project, 'push', 'other-feature', execute=lambda *a: pytest.fail('No network for wrong branch'))


def test_head_advance_during_scan_blocks_push(project, monkeypatch):
    from nexus import github_git
    original = github_git.scan_outbound
    def advance(*args, **kwargs):
        result = original(*args, **kwargs)
        git(project, 'commit', '--allow-empty', '-m', 'test: concurrent head change')
        return result
    monkeypatch.setattr(github_git, 'scan_outbound', advance)
    calls, execute = scripted(project)
    with pytest.raises(GitHubSafetyError, match='advanced during scan'):
        transport(project, 'push', 'feature', execute=execute)
    assert not any('push' in argv for argv in calls)


def test_integration_toolbar_and_typed_tools_share_transport(project, monkeypatch):
    from nexus import github_git, tools
    from nexus.server import ServerState
    seen = []
    monkeypatch.setattr(github_git, 'typed_transport', lambda workspace, parts, **kwargs: seen.append((workspace, parts)) or 'scripted transport')
    service = ServerState(project, quiet=True).workbench.repositories
    assert service.command(project, ['git', 'push', 'origin', 'feature']) == 'scripted transport'
    ctx = tools.ToolContext(workspace=project, guard=tools.PathGuard(project), gate=tools.ApprovalGate(mode='auto'))
    assert tools.run_git_transport(ctx, ['push', 'origin', 'feature']) == ('scripted transport', 0)
    assert seen == [(project, ['push', 'origin', 'feature'])] * 2


def test_credential_in_branch_name_never_reaches_network_argv(project, monkeypatch):
    token = 'opaque-fixture-credential'
    git(project, 'branch', '-m', token)
    with pytest.raises(GitHubSafetyError, match='Potential secret'):
        transport(project, 'push', token, execute=lambda *a: pytest.fail('Credential in argv'))


def test_outbound_scan_time_budget_fails_before_push(project, monkeypatch):
    from nexus import github_git
    clock = iter([0, 61])
    monkeypatch.setattr(github_git.time, 'monotonic', lambda: next(clock))
    calls, execute = scripted(project)
    with pytest.raises(GitHubSafetyError, match='time budget'):
        transport(project, 'push', 'feature', execute=execute)
    assert not any('push' in argv for argv in calls)


def test_default_transport_subprocess_is_argv_only_and_uses_isolated_environment(project, monkeypatch):
    from types import SimpleNamespace
    actual = subprocess.run
    calls, execute = scripted(project)
    def run(argv, **kwargs):
        if 'ls-remote' in argv or 'push' in argv:
            assert kwargs['stdin'] == subprocess.DEVNULL and not kwargs.get('shell', False)
            output, code = execute(argv, kwargs['env'])
            return SimpleNamespace(stdout=output, stderr='withheld transport diagnostics', returncode=code)
        return actual(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', run)
    assert 'Pushed scanned commit' in transport(project, 'push', 'feature')
    assert len(calls) == 3


def test_cancellation_after_lookup_prevents_push(project):
    cancel = threading.Event()
    calls, execute = scripted(project)
    def cancelling(argv, env):
        result = execute(argv, env)
        if '--symref' in argv:
            cancel.set()
        return result
    with pytest.raises(GitHubSafetyError, match='cancelled'):
        transport(project, 'push', 'feature', execute=cancelling, cancel=cancel)
    assert not any('push' in argv for argv in calls)


def test_case_variant_private_paths_are_not_exempt_from_outbound_scan(project):
    (project / '.ENV').write_text('unlabelled private data')
    git(project, 'add', '-f', '.ENV')
    git(project, 'commit', '-m', 'test: private path fixture')
    calls, execute = scripted(project)
    with pytest.raises(ValueError, match='Private/runtime'):
        transport(project, 'push', 'feature', execute=execute)
    assert not any('push' in argv for argv in calls)
