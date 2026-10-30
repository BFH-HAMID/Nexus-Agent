"""User-manager commands are scripted; never install/remove a real OS service."""
import json
import os
from types import SimpleNamespace

import pytest

from nexus.jarvis import service_control as control
from nexus.jarvis.services import manifest


@pytest.fixture
def linux_service(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(control.sys, 'platform', 'linux')
    home = tmp_path / 'home'
    monkeypatch.setattr(control.Path, 'home', lambda: home)
    data = manifest(repo, 'systemd')
    path = home / '.config/systemd/user' / data['filename']
    path.parent.mkdir(parents=True)
    path.write_text(data['content'])
    calls = []
    props = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'UnitFileState': 'enabled',
             'FragmentPath': str(path), 'DropInPaths': '', 'Transient': 'no', 'NeedDaemonReload': 'no'}
    def run(argv):
        calls.append(argv)
        if 'show' in argv:
            return 0, '\n'.join(k + '=' + v for k, v in props.items()).encode()
        if 'disable' in argv:
            props['UnitFileState'] = 'disabled'
        return 0, b''
    monkeypatch.setattr(control, '_run', run)
    return SimpleNamespace(repo=repo, path=path, calls=calls, props=props, run=run)


def test_inspection_is_read_only_and_redacts_manager_output(linux_service):
    f = linux_service
    f.props['UnrelatedSecret'] = 'private-do-not-echo'
    data = control.inspect(f.repo, 'systemd')
    assert data['removable'] and data['states']['ActiveState'] == 'inactive'
    assert all('show' in call for call in f.calls)
    assert 'private-do-not-echo' not in json.dumps(data)
    assert f.path.exists()


def test_exact_confirmation_and_stale_manifest_refuse_before_mutation(linux_service):
    f = linux_service
    before = control.inspect(f.repo, 'systemd')
    with pytest.raises(ValueError):
        control.remove(f.repo, 'systemd', confirmation='wrong')
    f.path.write_text(f.path.read_text() + '\n# operator edit\n')
    with pytest.raises(ValueError):
        control.remove(f.repo, 'systemd', confirmation=before['confirmation'])
    assert all('show' in call for call in f.calls)
    assert f.path.exists()


def test_matching_linux_unit_removal_is_bounded_and_verified(linux_service):
    f = linux_service
    before = control.inspect(f.repo, 'systemd')
    result = control.remove(f.repo, 'systemd', confirmation=before['confirmation'])
    assert result['removal_commands_accepted'] and result['needs_manual_review']
    assert not f.path.exists()
    assert ['systemctl', '--user', 'disable', '--now', before['filename']] in f.calls
    assert f.calls[-1] == ['systemctl', '--user', 'daemon-reload']


@pytest.mark.parametrize('property,value', [('FragmentPath', '/different/private/unit'),
    ('DropInPaths', '/private/override.conf'), ('NeedDaemonReload', 'yes'), ('Transient', 'yes')])
def test_foreign_overridden_or_unreloaded_units_are_never_removable(linux_service, property, value):
    f = linux_service
    f.props[property] = value
    assert not control.inspect(f.repo, 'systemd')['removable']
    with pytest.raises(ValueError):
        control.remove(f.repo, 'systemd', confirmation='REMOVE SERVICE anything')
    assert all('show' in call for call in f.calls) and f.path.exists()


def test_symlink_and_partial_manager_failures_preserve_files(linux_service, monkeypatch):
    f = linux_service
    contents = f.path.read_text()
    other = f.path.parent / 'private'
    other.write_text(contents)
    f.path.unlink()
    f.path.symlink_to(other)
    assert not control.inspect(f.repo, 'systemd')['removable']
    f.path.unlink()
    if hasattr(os, 'mkfifo'):
        os.mkfifo(f.path)
        assert not control.inspect(f.repo, 'systemd')['removable']
        f.path.unlink()
    f.path.write_text(contents)
    preview = control.inspect(f.repo, 'systemd')
    def fail(argv):
        if 'disable' in argv:
            return 1, b'private-manager-error'
        return f.run(argv)
    monkeypatch.setattr(control, '_run', fail)
    result = control.remove(f.repo, 'systemd', confirmation=preview['confirmation'])
    assert not result['removal_commands_accepted'] and f.path.exists()
    assert 'private-manager-error' not in json.dumps(result)


def test_changed_file_after_disable_is_not_deleted(linux_service, monkeypatch):
    f = linux_service
    before = control.inspect(f.repo, 'systemd')
    def change(argv):
        result = f.run(argv)
        if 'disable' in argv:
            f.path.write_text('operator changed this during removal')
        return result
    monkeypatch.setattr(control, '_run', change)
    result = control.remove(f.repo, 'systemd', confirmation=before['confirmation'])
    assert not result['removal_commands_accepted'] and f.path.exists()
    assert 'operator changed' in f.path.read_text()


def test_wrong_os_executes_no_manager_commands(repo, monkeypatch):
    monkeypatch.setattr(control.sys, 'platform', 'linux')
    monkeypatch.setattr(control, '_run', lambda argv: pytest.fail('No cross-platform manager command'))
    assert not control.inspect(repo, 'windows', 'DOMAIN\\Operator')['supported']


@pytest.mark.parametrize('running', [False, True])
def test_windows_owned_task_is_disabled_ended_if_needed_and_deleted(repo, monkeypatch, running):
    monkeypatch.setattr(control.sys, 'platform', 'win32')
    xml = manifest(repo, 'windows', 'DOMAIN\\Operator')['content'].encode()
    calls, state = [], [4 if running else 3]
    def run(argv):
        calls.append(argv)
        if '/Query' in argv:
            return 0, xml
        if argv[0] == 'powershell.exe':
            return 0, str(state[0]).encode()
        if '/DISABLE' in argv and not running:
            state[0] = 1
        if '/End' in argv:
            state[0] = 1
        return 0, b''
    monkeypatch.setattr(control, '_run', run)
    before = control.inspect(repo, 'windows', 'DOMAIN\\Operator')
    result = control.remove(repo, 'windows', 'DOMAIN\\Operator', confirmation=before['confirmation'])
    assert result['removal_commands_accepted']
    assert any('/Delete' in call for call in calls)
    assert any('/End' in call for call in calls) is running
    assert all('-ExecutionPolicy' not in call for call in calls)


def test_windows_extra_actions_privileges_or_wrong_identity_are_refused(repo):
    xml = manifest(repo, 'windows', 'DOMAIN\\Operator')['content'].encode()
    assert control._windows_owned(xml, xml)
    assert control._windows_owned(xml.replace(b'"Operator"', b'"Author"'), xml)
    for body in [xml.replace(b'<Exec>', b'<Exec><Unknown>bad</Unknown>'),
                 xml.replace(b'LeastPrivilege', b'HighestAvailable'),
                 xml.replace(b'DOMAIN\\Operator', b'OtherUser'),
                 xml.replace(b'</Actions>', b'<Exec /></Actions>')]:
        assert not control._windows_owned(body, xml)


def test_linux_manager_accepted_stop_but_still_active_does_not_delete(linux_service):
    f = linux_service
    f.props['ActiveState'] = 'active'
    before = control.inspect(f.repo, 'systemd')
    result = control.remove(f.repo, 'systemd', confirmation=before['confirmation'])
    assert not result['removal_commands_accepted'] and f.path.exists()
    assert not any('daemon-reload' in call for call in f.calls)


def test_windows_task_restarted_during_final_inspection_is_not_deleted(repo, monkeypatch):
    monkeypatch.setattr(control.sys, 'platform', 'win32')
    xml = manifest(repo, 'windows', 'DOMAIN\\Operator')['content'].encode()
    state, calls = iter([3, 3, 1, 1, 4]), []
    def run(argv):
        calls.append(argv)
        if '/Query' in argv:
            return 0, xml
        if argv[0] == 'powershell.exe':
            return 0, str(next(state)).encode()
        return 0, b''
    monkeypatch.setattr(control, '_run', run)
    before = control.inspect(repo, 'windows', 'DOMAIN\\Operator')
    result = control.remove(repo, 'windows', 'DOMAIN\\Operator', confirmation=before['confirmation'])
    assert not result['removal_commands_accepted']
    assert not any('/Delete' in call for call in calls)
