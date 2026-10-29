"""Desktop policy setup reuses validators; no ambient network/collection/install."""
import json
import threading

import pytest
import yaml

from nexus.gui_desktop import dispatch
from nexus.server import ServerState


def test_readiness_is_local_defaults_and_credentials_are_presence_only(repo, monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'private-telegram-token')
    data = dispatch(ServerState(repo, quiet=True))
    assert all(not row['settings']['enabled'] for row in data['policies'].values())
    assert data['credentials']['TELEGRAM_BOT_TOKEN'] is True
    assert 'private-telegram-token' not in json.dumps(data)


@pytest.mark.parametrize('kind', ['context', 'telegram', 'mcp'])
def test_policy_saves_are_confirmed_atomic_and_cas_guarded(repo, kind):
    state = ServerState(repo, quiet=True)
    before = dispatch(state)['policies'][kind]
    cfg = before['settings']
    if kind == 'context':
        cfg.update(enabled=True, signals={**cfg['signals'], 'project': True})
    elif kind == 'telegram':
        cfg.update(user_ids=[1234567])
    else:
        cfg['servers'] = {'local': {'transport': 'http', 'url': 'http://localhost:8080/mcp', 'tools': [
            {'name': 'lookup', 'description': 'Look up a record', 'inputSchema': {'type': 'object'}}]}}
    payload = {'operation': 'save', 'kind': kind, 'revision': before['revision'],
               'settings': cfg, 'confirmation': 'SAVE ' + kind.upper() + ' POLICY'}
    with pytest.raises(ValueError):
        dispatch(state, {**payload, 'confirmation': 'wrong'})
    assert not (repo / (kind + '.yaml')).exists()
    assert dispatch(state, payload)['saved']
    saved = (repo / (kind + '.yaml')).read_bytes()
    assert yaml.safe_load(saved)['enabled'] is cfg['enabled']
    with pytest.raises(ValueError):
        dispatch(state, {**payload, 'revision': 'stale'})
    assert (repo / (kind + '.yaml')).read_bytes() == saved


@pytest.mark.parametrize('kind,cfg', [('context', {'enabled': 'yes'}), ('telegram', {'enabled': True}),
                                      ('mcp', {'enabled': False, 'servers': {'bad': {'transport': 'http', 'url': 'https://x/?key=private'}}})])
def test_invalid_disabled_or_enabled_policies_never_write(repo, kind, cfg):
    state = ServerState(repo, quiet=True)
    row = dispatch(state)['policies'][kind]
    with pytest.raises(ValueError):
        dispatch(state, {'operation': 'save', 'kind': kind, 'settings': cfg, 'revision': row['revision'],
                         'confirmation': 'SAVE ' + kind.upper() + ' POLICY'})
    assert not (repo / (kind + '.yaml')).exists()


def test_symlinks_and_bad_yaml_do_not_disclose_private_contents(repo, tmp_path):
    private = tmp_path / 'private'
    private.write_text('private: [secret-never-echo\n')
    (repo / 'context.yaml').symlink_to(private)
    (repo / 'telegram.yaml').write_text(private.read_text())
    data = dispatch(ServerState(repo, quiet=True))
    assert 'error' in data['policies']['context'] and 'error' in data['policies']['telegram']
    assert 'secret-never-echo' not in json.dumps(data)


def test_service_preview_does_not_install_and_install_reuses_existing_review(repo, monkeypatch):
    from nexus.jarvis import services
    state = ServerState(repo, quiet=True)
    calls = []
    monkeypatch.setattr(services, 'install', lambda *args, **kwargs: calls.append((args, kwargs)) or {'installed': True})
    data = dispatch(state, {'operation': 'service-preview', 'platform': 'windows', 'user': 'DOMAIN\\Operator'})
    assert not calls and data['installed'] is False and 'InteractiveToken' in data['content']
    payload = {'operation': 'service-install', 'platform': 'windows', 'user': 'DOMAIN\\Operator'}
    with pytest.raises(ValueError):
        dispatch(state, {**payload, 'confirmation': 'wrong'})
    assert not calls
    assert dispatch(state, {**payload, 'confirmation': data['confirmation']})['installed']
    assert calls[0][1]['confirmation'] == data['confirmation']
    with pytest.raises(ValueError):
        dispatch(state, {**payload, 'user': 'OtherUser', 'confirmation': data['confirmation']})


def test_demo_cannot_modify_policies(repo):
    state = ServerState(repo, quiet=True, demo=True)
    with pytest.raises(ValueError):
        dispatch(state, {'operation': 'service-install'})


def test_existing_confined_absolute_plugin_path_remains_readable(repo):
    (repo / 'mcp.yaml').write_text(yaml.safe_dump({'schema': 1, 'enabled': True,
                                                'plugin_directory': str(repo / 'plugins'), 'servers': {}}))
    row = dispatch(ServerState(repo, quiet=True))['policies']['mcp']
    assert row['settings']['plugin_directory'] == str(repo / 'plugins')


def test_service_removal_route_reuses_controller_with_fixed_workspace(repo, monkeypatch):
    from nexus.jarvis import service_control
    state = ServerState(repo, quiet=True)
    calls = []
    monkeypatch.setattr(service_control, 'inspect', lambda *args: {'removable': False, 'reason': 'test manager unavailable'})
    assert not dispatch(state, {'operation': 'service-inspect', 'platform': 'systemd'})['removable']
    monkeypatch.setattr(service_control, 'remove', lambda *args, **kw: calls.append((args, kw)) or {'removal_commands_accepted': True})
    result = dispatch(state, {'operation': 'service-remove', 'platform': 'systemd', 'confirmation': 'explicit test consent', 'workspace': '/not-the-target'})
    assert result['removal_commands_accepted']
    assert calls[0][0] == (repo, 'systemd', '')
    assert calls[0][1]['confirmation'] == 'explicit test consent'
    with pytest.raises(ValueError):
        dispatch(state, {'operation': 'service-remove', 'platform': 'arbitrary-command'})


def test_parallel_service_changes_are_refused_without_global_state_lock_held(repo):
    from nexus.gui_desktop import service_change
    state = ServerState(repo, quiet=True)
    with service_change(state):
        acquired = threading.Event()
        def check():
            if state.lock.acquire(blocking=False):
                acquired.set()
                state.lock.release()
        thread = threading.Thread(target=check)
        thread.start()
        thread.join(2)
        assert acquired.is_set()
        with pytest.raises(ValueError):
            with service_change(state):
                pytest.fail('No parallel service mutation')
