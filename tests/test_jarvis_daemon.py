"""Real subprocess lifecycle plus stale/crashed ownership; never calls models."""
import subprocess
import sys
import threading
import time

import pytest

from nexus.jarvis.daemon import Daemon
from nexus.server import ServerState


@pytest.fixture
def daemon(repo):
    return Daemon(ServerState(repo, quiet=True).workbench.studio)


def wait(daemon, state, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = daemon.status()
        if status['state'] == state:
            return status
        threading.Event().wait(.05)
    raise AssertionError(f'Expected {state}, got {daemon.status()}')


def spawn(daemon):
    receipt = daemon.request('start')
    instance = receipt['control']['instance']
    return subprocess.Popen([sys.executable, '-I', '-m', 'nexus', 'jarvis', '-C', str(daemon.studio.root),
                             'run', '--instance', instance], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def cleanup(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    process.communicate(timeout=3)


def test_real_daemon_start_pause_resume_stop_with_no_execution(daemon):
    job = daemon.queue.enqueue('Must remain approval pending')
    process = spawn(daemon)
    try:
        assert wait(daemon, 'running')['lock_held']
        assert not daemon.status()['execution_enabled']
        daemon.request('pause')
        wait(daemon, 'paused')
        daemon.request('resume')
        wait(daemon, 'running')
        daemon.request('stop')
        process.wait(timeout=5)
        assert daemon.status()['state'] == 'stopped'
        assert daemon.queue.status()['jobs'][0]['id'] == job['id']
        assert daemon.queue.status()['jobs'][0]['status'] == 'awaiting_approval'
    finally:
        cleanup(process)


def test_duplicate_daemon_is_refused_and_kill_is_cooperative(daemon):
    process = spawn(daemon)
    try:
        wait(daemon, 'running')
        with pytest.raises(ValueError, match='already'):
            daemon.request('start')
        receipt = daemon.request('kill')
        assert not receipt['confirmed']
        process.wait(timeout=5)
        assert daemon.status()['state'] == 'killed'
    finally:
        cleanup(process)


def test_crashed_process_does_not_get_replayed_or_pid_killed(daemon):
    process = spawn(daemon)
    try:
        wait(daemon, 'running')
        process.kill()  # Owned test Popen, never a persisted PID.
        process.wait(timeout=5)
        assert daemon.status()['state'] == 'orphaned'
        replacement = spawn(daemon)
        try:
            wait(daemon, 'running')
            daemon.request('stop')
            replacement.wait(timeout=5)
        finally:
            cleanup(replacement)
    finally:
        cleanup(process)


def test_l0_and_emergency_stop_deny_start(daemon):
    (daemon.studio.root / 'autonomy.yaml').write_text('level: L0')
    with pytest.raises(ValueError):
        daemon.request('start')
    (daemon.studio.root / 'autonomy.yaml').write_text('level: L1')
    from nexus.computer_control import EmergencyStop
    EmergencyStop().set(True)
    with pytest.raises(ValueError):
        daemon.request('start')
    EmergencyStop().set(False)


def test_policy_revocation_pauses_running_daemon_and_no_jobs_execute(daemon):
    process = spawn(daemon)
    try:
        wait(daemon, 'running')
        (daemon.studio.root / 'autonomy.yaml').write_text('level: L0')
        wait(daemon, 'paused')
        with pytest.raises(ValueError):
            daemon.request('resume')
        daemon.request('stop')
        process.wait(timeout=5)
    finally:
        cleanup(process)


def test_invalid_instance_cannot_own_worker(daemon):
    with pytest.raises(ValueError):
        daemon.run('not-an-instance')
    daemon.request('start')
    with pytest.raises(ValueError):
        daemon.run('a' * 32)


def test_launch_reports_request_not_fake_ready(daemon, monkeypatch):
    captured = []
    class Process:
        pid = 123
    monkeypatch.setattr(subprocess, 'Popen', lambda argv, **kw: captured.append((argv, kw)) or Process())
    receipt = daemon.start()
    assert receipt['confirmed'] is False and receipt['launched_pid'] == 123
    assert '-I' in captured[0][0] and captured[0][1]['stdin'] == subprocess.DEVNULL
    assert daemon.status()['state'] == 'starting'


def test_symlink_lock_fails_closed(daemon, tmp_path):
    (daemon.queue.directory() / 'daemon.lock').symlink_to(tmp_path / 'outside')
    with pytest.raises(ValueError):
        daemon.status()


def test_user_service_manifests_are_review_only_and_escape_values(daemon):
    from pathlib import Path
    from xml.etree import ElementTree

    from nexus.jarvis.services import manifest
    linux = manifest(Path('/tmp/project with space % $'), 'systemd')
    assert not linux['installed'] and '%%' in linux['content'] and '$$' in linux['content']
    assert 'supervise' in linux['content'] and 'KillMode=control-group' in linux['content']
    windows = manifest(Path(r'C:\Users\User\Project & docs'), 'windows', r'DOMAIN\User')
    root = ElementTree.fromstring(windows['content'])
    ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    assert root.find('.//t:LogonType', ns).text == 'InteractiveToken'
    assert root.find('.//t:RunLevel', ns).text == 'LeastPrivilege'
    assert 'supervise' in root.find('.//t:Arguments', ns).text
    assert not windows['installed']


def test_service_manifest_refuses_injection_and_missing_windows_identity(daemon):
    from nexus.jarvis.services import manifest
    with pytest.raises(ValueError):
        manifest('project\nExecStart=evil', 'systemd')
    with pytest.raises(ValueError):
        manifest('project', 'windows')


def test_emergency_latch_exits_real_daemon(daemon):
    from nexus.computer_control import EmergencyStop
    process = spawn(daemon)
    try:
        wait(daemon, 'running')
        EmergencyStop().set(True)
        process.wait(timeout=5)
        assert daemon.status()['state'] == 'killed'
    finally:
        EmergencyStop().set(False)
        cleanup(process)


def test_safety_controls_survive_invalid_autonomy_policy(daemon):
    daemon.request('start')
    (daemon.studio.root / 'autonomy.yaml').write_text('invalid: [yaml')
    assert daemon.request('stop')['control']['desired'] == 'stopped'
    assert daemon.request('kill')['control']['desired'] == 'killed'
    with pytest.raises(ValueError):
        daemon.request('start')
