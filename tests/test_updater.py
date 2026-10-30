"""Updater commands are scripted; tests never fetch, install or mutate the real checkout."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from filelock import FileLock

from nexus import updater

SHA = 'a' * 40
OLD = 'b' * 40


@pytest.fixture
def setup(monkeypatch, tmp_path):
    root = tmp_path / 'checkout'
    root.mkdir()
    (root / '.git').mkdir()
    monkeypatch.setattr(updater, 'lock_directory', lambda: tmp_path / 'locks')
    monkeypatch.setattr(updater, 'runtime_active', lambda: False)
    calls = []
    replies = {'--show-toplevel': str(root), 'HEAD': OLD, 'FETCH_HEAD': SHA,
               'origin': updater.REPOSITORY, '--short': 'main'}

    def run(cmd, **kwargs):
        calls.append(cmd)
        if 'status' in cmd:
            return ''
        if 'symbolic-ref' in cmd:
            return replies['--short']
        if 'ls-remote' in cmd:
            return SHA + '\trefs/heads/main'
        return replies.get(cmd[-1], '')

    monkeypatch.setattr(updater, 'run', run)
    return root, calls, replies


def test_git_updates_exact_source_in_same_interpreter(setup):
    root, calls, _ = setup
    ok, receipt = updater.update(root, extras='hud,mcp')
    assert ok, receipt
    assert any('fetch' in cmd and updater.REPOSITORY in cmd for cmd in calls)
    assert any(cmd[-4:] == ['merge', '--ff-only', '--no-overwrite-ignore', SHA] for cmd in calls)
    pip = next(cmd for cmd in calls if 'install' in cmd)
    assert pip[:4] == [sys.executable, '-m', 'pip', 'install']
    assert pip[-2:] == ['-e', str(root) + '[hud,mcp]']
    assert any('check' in cmd for cmd in calls)
    assert any('-I' in cmd for cmd in calls)
    assert 'Restart' in receipt[-1]


@pytest.mark.parametrize('branch', ['feature', 'arena/session', 'HEAD'])
def test_non_main_refused(setup, branch):
    root, calls, replies = setup
    replies['--short'] = branch
    assert not updater.update(root)[0]
    assert not any('fetch' in cmd or 'install' in cmd for cmd in calls)


def test_dirty_and_untracked_refused(setup, monkeypatch):
    root, calls, _ = setup
    original = updater.run
    monkeypatch.setattr(updater, 'run', lambda cmd, **kw: '?? app/' if 'status' in cmd else original(cmd, **kw))
    assert not updater.update(root)[0]
    assert not any('fetch' in cmd for cmd in calls)


def test_foreign_remote_refused(setup):
    root, calls, replies = setup
    replies['origin'] = 'https://other.example/user/repo.git'
    assert not updater.update(root)[0]
    assert not any('fetch' in cmd for cmd in calls)


def test_local_ahead_diverged_and_partial_install_failures(setup, monkeypatch):
    root, calls, _ = setup
    original = updater.run
    for step in ['merge-base', 'install', 'check', '-I']:
        calls.clear()
        def run(cmd, step=step, **kwargs):
            if step in cmd:
                raise updater.UpdateError('Scripted failure')
            return original(cmd, **kwargs)
        monkeypatch.setattr(updater, 'run', run)
        ok, receipt = updater.update(root)
        assert not ok
        assert 'incomplete' in receipt[-1]
        if step == 'merge-base':
            assert not any('merge' in cmd or 'install' in cmd for cmd in calls)
        else:
            assert any('Checkout is now' in text for text in receipt)


def test_active_runtime_and_lock_refused(setup, monkeypatch):
    root, calls, _ = setup
    monkeypatch.setattr(updater, 'runtime_active', lambda: True)
    assert not updater.update(root)[0]
    assert not calls
    home = updater.lock_directory()
    with FileLock(str(home / 'self-update.lock')):
        assert not updater.update(root)[0]


def test_check_does_not_fetch_install_or_require_stopping_runtime(setup, monkeypatch):
    root, calls, _ = setup
    monkeypatch.setattr(updater, 'runtime_active', lambda: True)
    assert updater.update(root, check=True)[0]
    assert any('ls-remote' in cmd for cmd in calls)
    assert not any('fetch' in cmd or 'install' in cmd for cmd in calls)


def test_package_archive_pinned_even_same_version(setup, monkeypatch):
    root, calls, _ = setup
    (root / '.git').rmdir()
    monkeypatch.setattr(updater, 'latest_commit', lambda: SHA)
    assert updater.update(root)[0]
    install = next(cmd for cmd in calls if 'install' in cmd)
    assert '--force-reinstall' in install
    assert install[-1].endswith('/archive/' + SHA + '.zip')
    assert not any('git' == cmd[0] for cmd in calls)


def test_package_check_unknown_extras_and_invalid_options(setup, monkeypatch):
    root, calls, _ = setup
    assert not updater.update(root, extras='evil @ url')[0]
    assert not calls
    (root / '.git').rmdir()
    monkeypatch.setattr(updater, 'latest_commit', lambda: SHA)
    assert updater.update(root, check=True)[0]
    assert not calls
    assert not updater.update(root, pull=False)[0]
    assert not updater.update(root, reinstall=False)[0]


def test_maintenance_skips_are_honest(setup):
    root, calls, _ = setup
    ok, receipt = updater.update(root, pull=False, reinstall=False)
    assert ok
    assert 'CODE ONLY' in receipt[-1]
    assert not any('fetch' in cmd or 'install' in cmd for cmd in calls)


def test_safe_subprocess_errors_do_not_echo_secrets(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='private-token', stderr='password'))
    with pytest.raises(updater.UpdateError) as error:
        updater.run(['git'])
    assert 'private-token' not in str(error.value)
    assert 'password' not in str(error.value)


def test_revision_validation(monkeypatch):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, count):
            return json.dumps({'object': {'sha': SHA}}).encode()
    monkeypatch.setattr(updater.urllib.request, 'urlopen', lambda *a, **k: Response())
    assert updater.latest_commit() == SHA
    SHA_INVALID = 'not-a-commit'
    monkeypatch.setattr(Response, 'read', lambda *a: json.dumps({'object': {'sha': SHA_INVALID}}).encode())
    with pytest.raises(updater.UpdateError):
        updater.latest_commit()


def test_runtime_detection(monkeypatch):
    import psutil
    me = updater.os.getpid()
    for argv in [['python', '-m', 'nexus', 'gui'], ['C:/bin/nexus.exe', 'serve'], ['python', '-m', 'nexus.telegram']]:
        monkeypatch.setattr(psutil, 'process_iter', lambda *a, argv=argv: [SimpleNamespace(info={'pid': me + 1, 'cmdline': argv})])
        assert updater.active_nexus()
    monkeypatch.setattr(psutil, 'process_iter', lambda *a: [SimpleNamespace(info={'pid': me, 'cmdline': ['nexus', 'update']})])
    assert not updater.active_nexus()


def test_windows_handoff_serializes_values_and_waits_without_claiming_success(setup, monkeypatch):
    root, _, _ = setup
    launched = []
    monkeypatch.setattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 512, raising=False)
    monkeypatch.setattr(subprocess, 'Popen', lambda argv, **kwargs: launched.append((argv, kwargs)))
    receipt = updater.windows_handoff(root, pull=True, reinstall=True, extras='hud,mcp')
    assert not receipt.exists()  # Scheduling is not completion.
    source = (receipt.parent / 'worker.py').read_text()
    compile(source, 'worker.py', 'exec')
    assert 'WaitForSingleObject' in source
    assert launched[0][0][0] == sys.executable
    assert launched[0][1]['creationflags'] == 512


def test_network_timeout_and_process_timeout_are_safe(monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError('private-url-token')
    monkeypatch.setattr(updater.urllib.request, 'urlopen', fail)
    with pytest.raises(updater.UpdateError) as error:
        updater.latest_commit()
    assert 'private-url-token' not in str(error.value)
    def process_fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(['secret'], 1)
    monkeypatch.setattr(subprocess, 'run', process_fail)
    with pytest.raises(updater.UpdateError):
        updater.run(['git'])


@pytest.mark.parametrize('conflict', [False, True])
def test_real_git_fast_forward_preserves_personal_ignored_files(tmp_path, monkeypatch, conflict):
    upstream = tmp_path / 'upstream'
    client = tmp_path / 'client'
    original = updater.run
    def git(*args):
        return subprocess.check_output(['git', *map(str, args)], text=True, stderr=subprocess.DEVNULL).strip()
    git('init', '-b', 'main', upstream)
    (upstream / '.gitignore').write_text('.env\n')
    (upstream / 'version.txt').write_text('one')
    git('-C', upstream, 'add', '.')
    def commit():
        git('-C', upstream, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'test')
    commit()
    git('clone', upstream, client)
    git('-C', client, 'remote', 'set-url', 'origin', updater.REPOSITORY)
    (client / '.env').write_text('private-local-value')
    (upstream / 'version.txt').write_text('two')
    if conflict:
        (upstream / '.env').write_text('upstream-default')
        git('-C', upstream, 'add', '-f', '.env')
    git('-C', upstream, 'add', 'version.txt')
    commit()
    installs = []
    def run(cmd, **kwargs):
        if 'fetch' in cmd:
            cmd = ['git', '-C', str(client), 'fetch', '--no-tags', str(upstream), 'refs/heads/main']
        if cmd[0] == sys.executable:
            installs.append(cmd)
            return ''
        return original(cmd, **kwargs)
    monkeypatch.setattr(updater, 'run', run)
    monkeypatch.setattr(updater, 'lock_directory', lambda: tmp_path / 'locks')
    monkeypatch.setattr(updater, 'runtime_active', lambda: False)
    ok, receipt = updater.update(client)
    assert ok is not conflict, receipt
    assert (client / '.env').read_text() == 'private-local-value'
    if conflict:
        assert not installs
    else:
        assert git('-C', client, 'rev-parse', 'HEAD') == git('-C', upstream, 'rev-parse', 'HEAD')
        assert installs
