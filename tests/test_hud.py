"""Actual ASGI/Studio contracts; no fake telemetry, inference or live remote actions."""
import json
import threading
import time

import pytest
import yaml

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nexus.hud.app import create_app
from nexus.hud.core import HUD, settings
from nexus.jarvis.runtime import Runtime
from nexus.server import ServerState

TOKEN = 'hud-test-auth-' + 'x' * 32
AUTH = {'Authorization': 'Bearer ' + TOKEN}
ORIGIN = {'Origin': 'http://testserver'}


@pytest.fixture
def studio(repo, monkeypatch):
    monkeypatch.setenv('NEXUS_HUD_TOKEN', TOKEN)
    return ServerState(repo, quiet=True).workbench.studio


@pytest.fixture
def client(studio):
    with TestClient(create_app(studio, TOKEN, allowed_hosts=['testserver'])) as client:
        yield client


@pytest.mark.parametrize('token', ['', 'short', ' ' * 32, 'é' * 32, 'x' * 257])
def test_auth_must_be_explicit_and_bounded(studio, token):
    with pytest.raises(ValueError):
        create_app(studio, token)


@pytest.mark.parametrize('authorization', ['', 'Bearer wrong', 'Basic ' + TOKEN])
def test_private_state_and_controls_require_bearer_auth(client, authorization):
    headers = {'Authorization': authorization}
    assert client.get('/api/state', headers=headers).status_code == 401
    assert client.post('/api/action/control', json={'action': 'start'}, headers=headers).status_code == 401
    assert client.get('/api/state?token=' + TOKEN).status_code == 401


def test_host_origin_and_csrf_boundaries_fail_closed(client):
    assert client.get('/api/state', headers={**AUTH, 'Host': 'evil.invalid'}).status_code == 403
    assert client.get('/api/state', headers={**AUTH, 'Origin': 'https://evil.invalid'}).status_code == 403
    assert client.post('/api/action/control', json={'action': 'kill'}, headers={**AUTH, 'Origin': 'null'}).status_code == 403
    response = client.get('/api/state', headers={**AUTH, **ORIGIN})
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert 'Access-Control-Allow-Origin' not in response.headers


def test_snapshot_contains_real_queue_metrics_and_no_demo(studio, client):
    from nexus.delegation.core import Delegations
    job = Delegations(studio).create('Actual unapproved local task')
    data = client.get('/api/state', headers=AUTH).json()
    assert data['jobs_total'] == 1 and data['jobs'][0]['id'] == job['id']
    assert 0 <= data['hardware']['memory_percent'] <= 100 and data['hardware']['disk_free_bytes'] > 0
    assert data['context']['enabled'] is False and data['usage']['actual_cost_usd'] is None
    assert data['daemon']['state'] == 'stopped' and not data['daemon']['execution_enabled']
    assert TOKEN not in json.dumps(data)


def test_create_review_and_cancel_use_existing_queue(client, studio):
    response = client.post('/api/action/create', headers=AUTH, json={'task': 'Inspect README', 'max_seconds': 10})
    assert response.status_code == 200
    job = response.json()
    assert job['status'] == 'awaiting_approval' and Runtime(studio).claim() is None
    bad = client.post('/api/action/review', headers=AUTH, json={'id': job['id'], 'action': 'approve', 'confirmation': 'yes'})
    assert bad.status_code == 409
    result = client.post('/api/action/review', headers=AUTH, json={'id': job['id'], 'action': 'approve',
                                                          'confirmation': 'APPROVE QUEUED TASK ' + job['id']})
    assert result.json()['status'] == 'approved' and Runtime(studio).claim() is None
    assert client.get('/api/job/' + job['id'], headers=AUTH).json()['state'] == 'blocked'
    assert client.post('/api/action/cancel', headers=AUTH, json={'id': job['id']}).json()['state'] == 'cancelled'


def test_fresh_action_review_is_never_queue_approval(client, studio):
    from nexus.jarvis.approvals import Approvals
    from nexus.jarvis.queue import Queue
    from nexus.types import Risk, ToolSpec
    (studio.root / 'autonomy.yaml').write_text(yaml.safe_dump({'runtime': {'enabled': True, 'local_model': 'fixture'}}))
    queue = Queue(studio)
    job = queue.enqueue('Review actual action')
    queue.review(job['id'], 'approve', 'APPROVE QUEUED TASK ' + job['id'])
    job = Runtime(studio).claim()
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(Approvals(studio).callback(job)(ToolSpec('write_file', 'write', {}),
                        {'path': 'actual.txt', 'content': 'proposed'}, Risk.WRITE, 'actual proposal')))
    thread.start()
    try:
        deadline = time.monotonic() + 3
        rows = []
        while time.monotonic() < deadline:
            rows = client.get('/api/state', headers=AUTH).json()['pending_actions']
            if rows:
                break
            threading.Event().wait(.02)
        identifier = rows[0]['id']
        detail = client.get('/api/approval/' + identifier, headers=AUTH).json()
        assert detail['arguments']['content'] == 'proposed'
        assert client.post('/api/action/decide', headers=AUTH, json={'id': identifier, 'allow': True, 'confirmation': 'yes'}).status_code == 409
        assert client.post('/api/action/decide', headers=AUTH, json={'id': identifier, 'allow': True, 'confirmation': detail['confirmation']}).status_code == 200
        thread.join(3)
        assert outcomes[0].approved
        assert client.post('/api/action/decide', headers=AUTH, json={'id': identifier, 'allow': True, 'confirmation': detail['confirmation']}).status_code == 409
    finally:
        queue.review(job['id'], 'cancel')
        thread.join(3)


def test_controls_are_durable_requests_not_fake_liveness(client, studio):
    assert client.post('/api/action/control', headers=AUTH, json={'action': 'start'}).status_code == 409
    result = client.post('/api/action/control', headers=AUTH, json={'action': 'kill'}).json()
    assert result['confirmed'] is False
    assert result['control']['desired'] == 'killed'
    assert client.post('/api/action/control', headers=AUTH, json={'action': 'shell'}).status_code == 409


def test_autonomy_is_exact_confirmed_and_preserves_runtime_gates(client, studio):
    assert client.post('/api/action/autonomy', headers=AUTH, json={'level': 'L3', 'confirmation': 'yes'}).status_code == 409
    assert client.post('/api/action/autonomy', headers=AUTH, json={'level': 'L3', 'confirmation': 'SET AUTONOMY L3'}).status_code == 200
    from nexus.jarvis.queue import Queue
    policy = Queue(studio).policy()
    assert policy['level'] == 'L3' and not policy['runtime']['enabled'] and not policy['runtime']['allow_safe_writes']


def test_quiet_dnd_and_body_limits(client):
    assert client.post('/api/action/delivery', headers=AUTH, json={'quiet': True, 'minutes': 60}).json()['quiet']
    assert client.post('/api/action/create', headers=AUTH, content='x' * 16001).status_code == 413
    assert client.post('/api/action/create', headers=AUTH, json=[]).status_code == 409
    assert client.post('/api/action/create', headers=AUTH, json={'task': 'x', 'permissions': 'AUTO_ALLOW'}).status_code == 409
    assert client.post('/api/action/decide', headers=AUTH, json={'id': 'x', 'allow': 'true'}).status_code == 409


def test_websocket_requires_same_origin_and_first_message_secret(studio, client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/ws', headers={'Origin': 'https://evil.invalid'}) as socket:
            socket.receive_json()
    with client.websocket_connect('/ws', headers=ORIGIN) as socket:
        socket.send_json({'token': 'wrong'})
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    with client.websocket_connect('/ws', headers=ORIGIN) as socket:
        socket.send_json({'token': TOKEN})
        state = socket.receive_json()
        assert state['context']['enabled'] is False and state['jobs_total'] == 0


def test_real_workflow_dag_and_audit_metadata(studio, client):
    studio.put('workflow-run', {'status': 'running', 'definition': {'nodes': [{'id': 'research', 'role': 'general', 'depends_on': []},
               {'id': 'review', 'role': 'qa', 'depends_on': ['research']}]}, 'nodes': {'research': {'status': 'done'}}})
    value = client.get('/api/state', headers=AUTH).json()['workflows'][0]
    assert value['nodes'][0]['status'] == 'done'
    assert value['nodes'][1]['status'] == 'not_observed' and value['nodes'][1]['depends_on'] == ['research']


def test_hud_policy_protected_and_invalid_yaml_refused(studio):
    from nexus.permissions import Permissions
    from nexus.types import ToolSpec
    (studio.root / 'hud.yaml').write_text('schema: 1\nhost: 0.0.0.0\n')
    with pytest.raises(ValueError):
        settings(studio.root)
    with pytest.raises(ValueError):
        Permissions(studio.root).evaluate(ToolSpec('write_file', 'write', {}), {'path': 'hud.yaml', 'content': ''})


def test_private_state_is_masked_using_known_secrets(studio, client, monkeypatch):
    key = 'actual-private-key-value-for-test'
    monkeypatch.setenv('OPENROUTER_API_KEY', key)
    studio.put('jarvis-notice', {'job_id': 'job', 'state': 'done', 'created': 1, 'summary': key, 'read': False})
    assert key not in json.dumps(HUD(studio).snapshot())


def test_static_assets_have_no_inline_code_or_remote_assets(client):
    html = client.get('/').text
    assert 'NEXUS_CORE@SYSTEM:~' in html and 'No sample data' in html
    assert 'onclick=' not in html
    from html.parser import HTMLParser
    class LocalAssets(HTMLParser):
        def handle_starttag(self, tag, attrs):
            data = dict(attrs)
            if tag == 'script' and data.get('src'):
                assert data['src'].startswith('/')
            if tag == 'link' and data.get('rel') == 'stylesheet':
                assert data['href'].startswith('/')
    LocalAssets().feed(html)  # External attribution links are not fetched assets.
    assert client.get('/hud.js').status_code == 200 and client.get('/hud.css').status_code == 200


def test_websocket_connection_cap_and_binary_frames_fail_closed(studio):
    (studio.root / 'hud.yaml').write_text('schema: 1\nmax_connections: 1\n')
    with TestClient(create_app(studio, TOKEN, allowed_hosts=['testserver'])) as client:
        with client.websocket_connect('/ws', headers=ORIGIN) as first:
            first.send_json({'token': TOKEN})
            first.receive_json()
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect('/ws', headers=ORIGIN) as second:
                    second.receive_json()
    with TestClient(create_app(studio, TOKEN, allowed_hosts=['testserver'])) as client:
        with client.websocket_connect('/ws', headers=ORIGIN) as socket:
            socket.send_bytes(b'not an authentication text frame')
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()


def test_invalid_autonomy_cannot_disable_stop_kill_or_fabricate_state(studio, client):
    (studio.root / 'autonomy.yaml').write_text('invalid: operator-setting\n')
    state = client.get('/api/state', headers=AUTH).json()
    assert 'error' in state['policy'] and not state['daemon']['execution_enabled']
    assert client.post('/api/action/control', headers=AUTH, json={'action': 'kill'}).status_code == 200


def test_authenticated_request_rate_is_bounded(client):
    for _ in range(120):
        assert client.get('/api/unknown', headers=AUTH).status_code == 404
    assert client.get('/api/state', headers=AUTH).status_code == 429


def test_audit_reads_actual_permission_events_not_model_reports(studio):
    from nexus.permissions import Permissions
    from nexus.tools import ApprovalGate
    from nexus.types import Risk, ToolSpec
    gate = ApprovalGate()
    gate.permissions = Permissions(studio.root)
    assert gate.check(ToolSpec('read_file', 'read', {}, risk=Risk.READ), {'path': 'README.md'}).approved
    events = HUD(studio).audit()
    assert events[-1]['tool'] == 'read_file' and events[-1]['approved']
    assert 'args_sha256' in events[-1] and 'args' not in events[-1]


def test_factory_token_is_masked_and_cannot_be_persisted_as_task(studio, monkeypatch):
    monkeypatch.delenv('NEXUS_HUD_TOKEN', raising=False)
    with TestClient(create_app(studio, TOKEN, allowed_hosts=['testserver'])) as client:
        assert client.post('/api/action/create', headers=AUTH, json={'task': TOKEN}).status_code == 409
        assert studio.items('jarvis-job') == []


def test_snapshot_socket_rejects_post_auth_commands_and_releases_slot(studio):
    (studio.root / 'hud.yaml').write_text('schema: 1\nmax_connections: 1\n')
    with TestClient(create_app(studio, TOKEN, allowed_hosts=['testserver'])) as client:
        with client.websocket_connect('/ws', headers=ORIGIN) as socket:
            socket.send_json({'token': TOKEN})
            socket.receive_json()
            socket.send_json({'action': 'cancel', 'id': 'not-authorized-on-this-protocol'})
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()
        with client.websocket_connect('/ws', headers=ORIGIN) as fresh:
            fresh.send_json({'token': TOKEN})
            assert 'jobs' in fresh.receive_json()


def test_remember_browser_survives_restart_and_logout(studio):
    def app(token=TOKEN):
        return create_app(studio, token, allowed_hosts=['localhost'])
    origin = {'Origin': 'http://localhost'}
    with TestClient(app(), base_url='http://localhost') as c:
        assert c.get('/api/session').status_code == 401
        assert c.post('/api/session', headers=AUTH).status_code == 403
        response = c.post('/api/session', headers={**AUTH, **origin})
        assert response.status_code == 200
        cookie = response.headers['set-cookie']
        assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie
        assert TOKEN not in cookie and 'Max-Age=34560000' in cookie
        saved = c.cookies.get('nexus_hud_session')
        assert c.get('/api/state').status_code == 200
        assert c.get('/api/state', headers={'Authorization': 'Bearer wrong'}).status_code == 401
        assert c.post('/api/action/control', json={'action': 'kill'}).status_code == 403
        assert c.get('/api/state', headers={'Origin': 'http://evil.invalid'}).status_code == 403
        with c.websocket_connect('ws://localhost/ws', headers=origin) as ws:
            ws.send_json({'token': ''})
            assert 'daemon' in ws.receive_json()
    with TestClient(app(), base_url='http://localhost') as c:
        c.cookies.set('nexus_hud_session', saved, domain='localhost.local', path='/')
        assert c.get('/api/session').status_code == 200
        assert c.delete('/api/session', headers=origin).status_code == 200
        assert c.get('/api/session').status_code == 401
    with TestClient(app('rotated-token-' + 'z' * 32), base_url='http://localhost') as c:
        c.cookies.set('nexus_hud_session', saved)
        assert c.get('/api/session').status_code == 401
    with TestClient(app(), base_url='http://localhost:9000') as c:
        c.cookies.set('nexus_hud_session', saved)
        assert c.get('/api/session').status_code == 401
    with TestClient(app(), base_url='http://localhost') as c:
        c.cookies.set('nexus_hud_session', saved[:-1] + ('a' if saved[-1] != 'a' else 'b'))
        assert c.get('/api/session').status_code == 401


def test_remember_requires_https_outside_loopback(studio):
    with TestClient(create_app(studio, TOKEN, allowed_hosts=['trusted.example']), base_url='http://trusted.example') as c:
        assert c.post('/api/session', headers={**AUTH, 'Origin': 'http://trusted.example'}).status_code == 403
    with TestClient(create_app(studio, TOKEN, allowed_hosts=['trusted.example']), base_url='https://trusted.example') as c:
        response = c.post('/api/session', headers={**AUTH, 'Origin': 'https://trusted.example'})
        assert response.status_code == 200 and 'Secure' in response.headers['set-cookie']


def test_os_vault_remembers_only_explicitly_and_scopes_workspace(repo, monkeypatch):
    import sys
    from types import SimpleNamespace

    from nexus.hud.auth import launch_token
    entries = {}
    backend = type('Native', (), {'__module__': 'keyring.backends.Windows'})()
    fake = SimpleNamespace(get_keyring=lambda: backend,
                           get_password=lambda service, account: entries.get((service, account)),
                           set_password=lambda service, account, value: entries.__setitem__((service, account), value))
    monkeypatch.setitem(sys.modules, 'keyring', fake)
    monkeypatch.setenv('NEXUS_HUD_TOKEN', TOKEN)
    assert launch_token(repo) == TOKEN and not entries
    assert launch_token(repo, True) == TOKEN and len(entries) == 1
    monkeypatch.delenv('NEXUS_HUD_TOKEN')
    assert launch_token(repo) == TOKEN
    assert launch_token(repo / 'other') == ''
    backend = type('Plaintext', (), {'__module__': 'keyrings.alt.file'})()
    with pytest.raises(ValueError, match='native OS'):
        launch_token(repo)


def test_country_data_is_read_explicitly_as_utf8(repo, monkeypatch):
    from pathlib import Path

    from nexus.hud.world.core import ASSETS, World
    original = Path.read_text
    def windows_default(path, *args, **kwargs):
        if path == ASSETS / 'countries.json':
            assert kwargs.get('encoding') == 'utf-8'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', windows_default)
    assert len(World(repo).countries) == 242


def test_local_runtime_setup_requires_exact_review_and_keeps_gates(client, studio):
    payload = {'enabled': True, 'local_model': 'installed-model:latest', 'confirmation': 'wrong'}
    assert client.post('/api/action/runtime', headers=AUTH, json=payload).status_code == 409
    assert HUD(studio).queue.policy()['runtime']['enabled'] is False
    payload['confirmation'] = 'ENABLE LOCAL RUNTIME'
    payload['local_model'] = 'remote:cloud'
    assert client.post('/api/action/runtime', headers=AUTH, json=payload).status_code == 409
    assert HUD(studio).queue.policy()['runtime']['enabled'] is False
    payload['local_model'] = 'installed-model:latest'
    assert client.post('/api/action/runtime', headers=AUTH, json=payload).status_code == 200
    policy = HUD(studio).queue.policy()
    assert policy['runtime']['enabled'] is True
    assert policy['runtime']['mode'] == 'read_only' and policy['level'] == 'L1'
    job = client.post('/api/action/create', headers=AUTH, json={'task': 'Still requires approval'}).json()
    assert job['status'] == 'awaiting_approval'
    state = client.get('/api/state', headers=AUTH).json()
    assert state['policy']['local_model'] == 'installed-model:latest'
    assert client.post('/api/action/runtime', headers=AUTH, json={**payload, 'enabled': False, 'confirmation': 'DISABLE LOCAL RUNTIME'}).status_code == 200
    assert HUD(studio).queue.policy()['runtime']['enabled'] is False


def test_dashboard_is_one_page_operations_first_and_reports_collapsed(client):
    html = client.get('/').text
    assert html.index('id="execution"') < html.index('id="world-widget"')
    assert 'aria-label="Dashboard sections"' in html
    assert '<section id="diagnostics"><details><summary>' in html
    assert 'id="world-interact"' in html
    assert '<iframe' not in html


def test_public_information_rate_budget_never_starves_controls(client):
    for _ in range(120):
        assert client.get('/api/countries', headers=AUTH).status_code == 200
    assert client.get('/api/countries', headers=AUTH).status_code == 429
    assert client.get('/api/state', headers=AUTH).status_code == 200
    assert client.post('/api/action/delivery', headers=AUTH, json={'quiet': True}).status_code == 200
