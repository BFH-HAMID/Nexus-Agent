"""Always-on runtime uses owned fake children, fake waits and temporary private state."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from filelock import FileLock, Timeout

from nexus.always_on import health, run
from nexus.hud.presence_store import Store, directory

TOKEN = 'service-operator-token-offline-fixture'


@pytest.fixture
def enabled(repo, monkeypatch):
    (repo / 'hud.yaml').write_text(yaml.safe_dump({'service': {
        'enabled': True, 'log_bytes': 16384, 'log_backups': 2,
        'restart_initial_seconds': 2, 'restart_max_seconds': 8}}))
    monkeypatch.setenv('NEXUS_SERVE_TOKEN', TOKEN)
    monkeypatch.setenv('NEXUS_ASSISTANT_SCHEDULER', '1')
    return repo


class ClockStop:
    def __init__(self, hook=None):
        self.at = 0
        self.stopped = False
        self.waits = []
        self.hook = hook

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        self.at += seconds
        if self.hook:
            self.hook(self)
        assert self.at < 1000, 'Supervisor failed to bound the offline test'
        return self.stopped


class Child:
    def __init__(self, code):
        self.returncode = code

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def test_crash_backoff_log_rotation_and_no_unattended_task_replay(enabled):
    stop = ClockStop()
    codes, calls, started = iter([7, 8, 0]), [], []
    folder = directory(enabled)
    folder.mkdir(parents=True, mode=0o700)
    log = folder / 'service.log'
    log.write_text('prior lifecycle metadata\n' * 650)
    log.chmod(0o600)
    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        started.append(stop.at)
        assert kwargs['env']['NEXUS_SERVE_TOKEN'] == TOKEN
        assert 'NEXUS_ASSISTANT_SCHEDULER' not in kwargs['env']
        assert TOKEN not in ' '.join(argv) and '--yes' not in argv
        assert kwargs['stdout'] == kwargs['stderr'] == kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['close_fds']
        return Child(next(codes))
    assert run(enabled, popen=spawn, stop_event=stop, clock=lambda: stop.at) == 0
    assert started == [0, 2, 6] and len(calls) == 3
    assert all(argv[-1] == '--quiet' and 'serve' in argv for argv, _ in calls)
    assert (folder / 'service.log.1').exists()
    for path in folder.glob('service.log*'):
        assert path.stat().st_size <= 16384
        assert TOKEN not in path.read_text()
        assert path.stat().st_mode & 0o077 == 0 if os.name == 'posix' else True
    data = health(enabled)
    assert data['state'] == 'stopped' and not data['child_running']
    with FileLock(str(folder / 'supervisor.lock'), timeout=0):
        pass  # All handles and the singleton lock were released.


def test_stop_terminates_only_owned_child_and_restores_signal_handlers(enabled, monkeypatch):
    child, killed = Child(None), []
    before = signal.getsignal(signal.SIGTERM)
    def terminate(process):
        assert process is child
        killed.append(process)
        process.returncode = -15
    monkeypatch.setattr('nexus.live_process.terminate_group', terminate)
    stop = ClockStop(lambda event: event.set())
    assert run(enabled, popen=lambda *a, **kw: child, stop_event=stop, clock=lambda: stop.at) == 0
    assert killed == [child] and stop.waits == [2]
    assert signal.getsignal(signal.SIGTERM) is before
    assert not health(enabled)['child_running']


def test_policy_disable_during_runtime_stops_child_without_restart(enabled, monkeypatch):
    child, spawned = Child(None), []
    def disable(_event):
        (enabled / 'hud.yaml').write_text('service:\n  enabled: false\n')
    def terminate(process):
        assert process is child
        process.returncode = -15
    monkeypatch.setattr('nexus.live_process.terminate_group', terminate)
    def spawn(*args, **kwargs):
        spawned.append(True)
        return child
    stop = ClockStop(disable)
    with pytest.raises(ValueError, match='OFF'):
        run(enabled, popen=spawn, stop_event=stop, clock=lambda: stop.at)
    assert len(spawned) == 1 and child.returncode == -15
    assert health(enabled)['state'] == 'stopped'


def test_second_supervisor_and_unsafe_logs_refuse_without_child(enabled, tmp_path):
    folder = directory(enabled)
    folder.mkdir(parents=True, mode=0o700)
    def forbidden(*args, **kwargs):
        pytest.fail('No child may be launched')
    with FileLock(str(folder / 'supervisor.lock'), timeout=0, mode=0o600), pytest.raises(Timeout):
        run(enabled, popen=forbidden)
    log = folder / 'service.log'
    log.unlink()
    target = tmp_path / 'foreign-private-log'
    target.write_text('foreign log')
    target.chmod(0o600)
    log.symlink_to(target)
    with pytest.raises(ValueError, match='Unsafe'):
        run(enabled, popen=forbidden)
    assert target.read_text() == 'foreign log'


def test_missing_authentication_or_disabled_service_has_no_children_or_state(repo, monkeypatch):
    monkeypatch.delenv('NEXUS_SERVE_TOKEN', raising=False)
    monkeypatch.setattr('nexus.gui_session.launch_credential', lambda *args: '')
    def forbidden(*args, **kwargs):
        pytest.fail('No child may be launched')
    with pytest.raises(ValueError, match='OFF'):
        run(repo, popen=forbidden)
    (repo / 'hud.yaml').write_text('service:\n  enabled: true\n')
    with pytest.raises(ValueError, match='requires'):
        run(repo, popen=forbidden)
    assert not directory(repo).exists()


def test_health_uses_fresh_metadata_not_untrusted_pid_and_doctor_reports_stopped(enabled, monkeypatch, capsys):
    from nexus.cli import cmd_doctor
    monkeypatch.setattr('os.kill', lambda *args: pytest.fail('Health never signals a recorded PID'))
    monkeypatch.setattr('nexus.cli.run_git', lambda *args: pytest.fail('Offline doctor never runs a shell'))
    store = Store(enabled, 'service', lambda: {})
    now = time.time()
    store.update(lambda row: row.update(at=now, state='running', child_running=True, pid=1))
    assert health(enabled, now=now + 1)['child_running']
    assert not health(enabled, now=now + 16)['heartbeat_fresh']
    assert not health(enabled, now=now - 1)['heartbeat_fresh']
    store.update(lambda row: row.update(at=time.time(), state='stopped', child_running=False))
    assert cmd_doctor(SimpleNamespace(workspace=str(enabled), offline=True, json=True)) == 1
    result = json.loads(capsys.readouterr().out)
    assert next(r for r in result if r['name'] == 'always-on service')['ok'] is False


def test_presence_installer_requires_yaml_and_exact_manifest_before_manager_commands(repo, monkeypatch, tmp_path):
    from nexus.jarvis.services import install, manifest
    calls = []
    monkeypatch.setattr('nexus.jarvis.services.subprocess.run', lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path / 'user-home')
    unit = manifest(repo, 'systemd', target='presence')
    import hashlib
    exact = 'INSTALL SERVICE ' + unit['filename'] + ' ' + hashlib.sha256(unit['content'].encode()).hexdigest()[:16]
    with pytest.raises(ValueError, match='OFF'):
        install(repo, 'systemd', target='presence', confirmation=exact)
    (repo / 'hud.yaml').write_text('service:\n  enabled: true\n')
    with pytest.raises(ValueError, match='Exact'):
        install(repo, 'systemd', target='presence', confirmation='yes')
    assert not calls
    monkeypatch.setattr('nexus.jarvis.services.sys.platform', 'linux')
    result = install(repo, 'systemd', target='presence', confirmation=exact)
    assert result['installed'] and result['running_confirmed'] is False
    assert len(calls) == 2 and calls[1][0][:4] == ['systemctl', '--user', 'enable', '--now']
    with pytest.raises(FileExistsError):
        install(repo, 'systemd', target='presence', confirmation=exact)
    assert len(calls) == 2  # No overwrite or duplicate manager operation.
