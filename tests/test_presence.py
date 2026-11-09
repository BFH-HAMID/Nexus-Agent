"""Shared HUD contracts: offline fakes and a real loopback HTTP/SSE transport."""
from __future__ import annotations

import base64
import http.client
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from nexus.hud.devices import COOKIE, Devices, cookie_token
from nexus.hud.presence import Presence, exact_approvals
from nexus.hud.presence_api import PresenceAPI
from nexus.hud.presence_policy import load
from nexus.hud.presence_store import directory
from nexus.server import ServerState, make_handler

MASTER = 'presence-test-owner-credential'
OWNER = {'kind': 'operator', 'id': 'operator', 'scopes': []}


def enable(root, **changes):
    cfg = {'enabled': True, 'devices_enabled': True, 'conversation_enabled': True,
           'voice_enabled': True, 'handoff_enabled': True,
           'device_scopes': ['read', 'chat', 'approve', 'voice', 'handoff', 'push'], **changes}
    (root / 'hud.yaml').write_text(yaml.safe_dump({'presence': cfg}))


@pytest.fixture
def api(repo):
    enable(repo)
    state = ServerState(repo, token=MASTER, quiet=True)
    result = PresenceAPI(state)
    state.presence = result
    yield result
    result.closed.set()
    state.mission.close()


def execute(api, operation, args, principal=OWNER):
    review = api.review(principal, operation, args)
    return api.execute(principal, {'id': review['id'], 'confirmation': review['confirmation']})


def test_disabled_is_inert_and_never_creates_private_store(repo):
    assert not load(repo)['presence']['enabled']
    assert not load(repo)['service']['enabled']
    hub = Presence(repo)
    hub.voice('listening', transcript='private')
    assert not directory(repo).exists()
    with pytest.raises(ValueError, match='OFF'):
        hub.begin('hello')
    with pytest.raises(ValueError):
        Devices(repo, MASTER).enroll('phone')
    assert not directory(repo).exists()


@pytest.mark.parametrize('change', [dict(enabled='true'), dict(device_scopes=['read', 'admin']),
    dict(max_devices=True), dict(history_turns=10000), dict(push_enabled=True),
    dict(push_origins=['https://127.0.0.1']), dict(default_speaker='any'), dict(extra=True)])
def test_operator_policy_rejects_ambiguous_or_unbounded_settings(repo, change):
    enable(repo, **change)
    with pytest.raises(ValueError):
        load(repo)


def test_hud_policy_and_runtime_state_are_not_agent_editable(repo):
    from nexus.permissions import Permissions
    assert Permissions(repo).operator_control(repo / 'hud.yaml')
    with pytest.raises(ValueError):
        Permissions(repo).path(str(directory(repo)))


def test_device_credentials_are_hashed_expiring_scoped_and_revocable(api):
    enrolled = execute(api, 'enroll', {'name': 'My phone'})
    token, ident = enrolled['token'], enrolled['id']
    principal = api.devices.authenticate(token)
    assert principal['kind'] == 'device' and 'approve' in principal['scopes']
    assert token not in api.devices.store.path.read_text()
    assert MASTER not in api.devices.store.path.read_text()
    assert 'hash' not in json.dumps(api.devices.items())
    assert not Devices(api.state.workspace, 'rotated-master').authenticate(token)
    assert not api.devices.authenticate(token[:-1] + ('a' if token[-1] != 'a' else 'b'))
    assert Devices(api.state.workspace, MASTER, clock=lambda: enrolled['expires'] + 1).authenticate(token) is None
    api.devices.revoke(ident)
    assert api.devices.authenticate(token) is None
    assert api.devices.store.path.stat().st_mode & 0o077 == 0 if os.name == 'posix' else True


def test_device_cannot_enroll_or_expand_yaml_scopes(api):
    row = execute(api, 'enroll', {'name': 'phone'})
    identity = api.devices.authenticate(row['token'])
    with pytest.raises(ValueError, match='scope'):
        api.review(identity, 'enroll', {'name': 'escalated'})
    enable(api.state.workspace, device_scopes=['read'])
    assert api.devices.authenticate(row['token'])['scopes'] == ['read']
    with pytest.raises(ValueError):
        api.review(identity, 'chat', {'text': 'blocked'})


def test_exact_review_is_one_use_policy_bound_and_actor_bound(api):
    row = execute(api, 'enroll', {'name': 'phone'})
    identity = api.devices.authenticate(row['token'])
    review = api.review(OWNER, 'handoff', {'speaker': 'none'})
    with pytest.raises(ValueError):
        api.execute(identity, {'id': review['id'], 'confirmation': review['confirmation']})
    with pytest.raises(ValueError):
        api.execute(OWNER, {'id': review['id'], 'confirmation': 'yes'})
    with pytest.raises(ValueError):
        api.execute(OWNER, {'id': review['id'], 'confirmation': review['confirmation'], 'arguments': {'speaker': row['id']}})
    assert api.execute(OWNER, {'id': review['id'], 'confirmation': review['confirmation']})['speaker'] == 'none'
    with pytest.raises(ValueError):
        api.execute(OWNER, {'id': review['id'], 'confirmation': review['confirmation']})
    review = api.review(OWNER, 'enroll', {'name': 'second'})
    enable(api.state.workspace, history_turns=3)
    with pytest.raises(ValueError):
        api.execute(OWNER, {'id': review['id'], 'confirmation': review['confirmation']})


def test_shared_history_and_reconnect_are_data_not_grants(api, monkeypatch):
    records = []
    def start(session):
        records.append(session)
        session.push({'type': 'text_delta', 'text': 'বাংলা answer'})
        session.push({'type': 'usage', 'input_tokens': 10, 'output_tokens': 20})
        session.push({'type': 'tool_call', 'args': {'private-body': 'never durable'}})
        session.push({'type': 'done', 'status': 'done', 'result': {'text': 'বাংলা answer'}})
    monkeypatch.setattr(api.state, 'start', start)
    result = execute(api, 'chat', {'text': 'First question'})
    assert result['queued'] and not records[0].auto_yes
    assert records[0].presence_history == []
    execute(api, 'chat', {'text': 'Follow up from another device'})
    history = records[1].presence_history
    assert [(m.role, m.content) for m in history] == [('user', 'First question'), ('assistant', 'বাংলা answer')]
    fresh = Presence(api.state.workspace)
    snapshot = fresh.events_since(99999)
    assert snapshot['reset'] and len(snapshot['state']['turns']) == 2
    raw = fresh.store.path.read_text()
    assert 'private-body' not in raw and 'never durable' not in raw
    assert snapshot['state']['turns'][0]['usage']['input_tokens'] == 10
    assert snapshot['state']['turns'][0]['usage']['cost_usd'] is None
    assert not records[0].approval_event.is_set()


def test_busy_turn_does_not_start_second_worker(api, monkeypatch):
    calls = []
    monkeypatch.setattr(api.state, 'start', lambda session: calls.append(session))
    execute(api, 'chat', {'text': 'first'})
    with pytest.raises(ValueError, match='busy'):
        execute(api, 'chat', {'text': 'second'})
    assert len(calls) == 1


def test_cross_process_objects_share_bounded_state_and_stale_markers(api):
    first = api.hub
    second = Presence(api.state.workspace)
    ident = first.begin('hello')
    second.agent_event(ident, {'type': 'text_delta', 'text': 'world '})
    assert first.snapshot()['turns'][0]['assistant'] == 'world '
    second.finish(ident, 'finished')
    first.voice('listening', source='phone')
    future = Presence(api.state.workspace, clock=lambda: time.time() + 1000)
    assert future.snapshot()['voice']['stale']
    assert future.snapshot()['voice']['state'] == 'unknown'
    assert all(set(e) == {'seq', 'kind', 'at'} for e in first.snapshot()['events'])


def test_speech_claim_is_selected_device_once_only_and_handoff_revokes(api):
    phone = execute(api, 'enroll', {'name': 'phone'})
    tablet = execute(api, 'enroll', {'name': 'tablet'})
    result = execute(api, 'handoff', {'speaker': phone['id']})
    assert result['approval_grants_transferred'] is False
    speech = api.hub.say('A local speech reply')
    with pytest.raises(ValueError):
        api.hub.claim(speech['id'], tablet['id'])
    assert api.hub.claim(speech['id'], phone['id'])['text'] == 'A local speech reply'
    with pytest.raises(ValueError):
        api.hub.claim(speech['id'], phone['id'])
    speech = api.hub.say('not replayed')
    execute(api, 'handoff', {'speaker': tablet['id']})
    with pytest.raises(ValueError):
        api.hub.claim(speech['id'], phone['id'])
    assert not api.hub.snapshot()['speech']


def pending(api, args):
    session = api.state.create('Review this exact action', None, False)
    session.pending = {'approval_id': 'fresh-id', 'tool': 'run_command', 'args': args,
                       'preview': 'Review exact arguments', 'requested': time.time(), 'timeout': 60}
    return session


def test_hud_approval_shows_exact_arguments_and_uses_original_once_gate(api):
    session = pending(api, {'command': 'echo exact; printf "বাংলা"'})
    rows, errors = exact_approvals(api.state)
    row = rows[0]
    assert not errors and row['complete']
    assert json.loads(row['action_text'])['arguments'] == session.pending['args']
    payload = {'source': 'live', 'id': session.task_id, 'approval_id': 'fresh-id',
               'decision': 'approve', 'confirmation': 'APPROVE ACTION fresh-id'}
    with pytest.raises(ValueError):
        api.post('approval', OWNER, {**payload, 'scope': 'session'})
    api.post('approval', OWNER, payload)
    assert session.approval_decision.approved and session.approval_decision.scope == 'once'
    with pytest.raises(ValueError):
        api.post('approval', OWNER, payload)


def test_partial_or_redacted_approval_is_deny_only(api, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-secret-text')
    session = pending(api, {'command': 'echo fixture-secret-text'})
    rows, _ = exact_approvals(api.state)
    assert not rows[0]['complete'] and rows[0]['decisions'] == ['deny']
    assert 'fixture-secret-text' not in json.dumps(rows)
    session.pending['args'] = {'command': 'a' * 30_000}
    assert exact_approvals(api.state)[0][0]['decisions'] == ['deny']


def test_private_store_refuses_links_and_permissions(api, tmp_path):
    api.hub.begin('private state')
    path = api.hub.store.path
    saved = path.read_bytes()
    path.unlink()
    target = tmp_path / 'elsewhere'
    target.write_bytes(saved)
    path.symlink_to(target)
    with pytest.raises(ValueError):
        api.hub.snapshot()


@pytest.fixture
def live(api):
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(api.state))
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    worker.start()
    yield api, server.server_address[1]
    api.closed.set()
    server.shutdown()
    server.server_close()
    worker.join(3)


def request(port, path, token=MASTER, body=None, *, headers=None):
    client = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    hs = {'Content-Type': 'application/json', **({'X-Nexus-Token': token} if token else {}), **(headers or {})}
    client.request('POST' if body is not None else 'GET', path, json.dumps(body) if body is not None else None, hs)
    response = client.getresponse()
    result = response.status, json.loads(response.read()), dict(response.getheaders())
    client.close()
    return result


def test_http_auth_device_cookie_and_admin_separation(live):
    api, port = live
    assert request(port, '/api/presence/state', None)[0] == 401
    assert request(port, '/api/presence/state', headers={'Origin': 'https://evil.example'})[0] == 403
    enrolled = execute(api, 'enroll', {'name': 'mobile'})
    token = enrolled['token']
    assert request(port, '/api/security/vault', token)[0] == 403
    assert request(port, '/api/task', token, {'task': 'bypass'})[0] == 403
    assert request(port, '/api/session?token=' + token, None)[0] == 401
    status, data, headers = request(port, '/api/session', token, {'remember': True})
    assert status == 200 and data['authenticated']
    # Two cookies are emitted, including removal of the old owner session. Inspect full response separately.
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    connection.request('POST', '/api/session', '{}', {'X-Nexus-Token': token, 'Content-Type': 'application/json'})
    response = connection.getresponse()
    cookies = [v for k, v in response.getheaders() if k.lower() == 'set-cookie']
    response.read()
    connection.close()
    device = next(v for v in cookies if v.startswith(COOKIE + '='))
    assert 'HttpOnly' in device and 'SameSite=Strict' in device
    assert MASTER not in device and cookie_token(device.split(';')[0])[0] == token
    cookie_header = {'Cookie': device.split(';')[0]}
    assert request(port, '/api/presence/state', None, headers=cookie_header)[0] == 200
    assert request(port, '/api/presence/voice/state', None, {'state': 'listening'}, headers=cookie_header)[0] == 403
    api.devices.revoke(enrolled['id'])
    assert request(port, '/api/presence/state', None, headers=cookie_header)[0] == 401


def read_frame(response):
    name, data = '', None
    while True:
        line = response.readline().decode().strip()
        if not line and data is not None:
            return name, json.loads(data)
        if not line and response.isclosed():
            return '', {}
        if line.startswith('event: '):
            name = line[7:]
        if line.startswith('data: '):
            data = line[6:]


def test_sse_devices_receive_same_conversation_and_revocation_closes_existing_stream(live):
    api, port = live
    phone = execute(api, 'enroll', {'name': 'phone'})
    tablet = execute(api, 'enroll', {'name': 'tablet'})
    ident = api.hub.begin('shared hello')
    api.hub.finish(ident, 'shared reply')
    connections, responses = [], []
    try:
        for device in (phone, tablet):
            client = http.client.HTTPConnection('127.0.0.1', port, timeout=6)
            client.request('GET', '/api/presence/events', headers={'X-Nexus-Token': device['token']})
            response = client.getresponse()
            assert response.status == 200
            connections.append(client)
            responses.append(response)
        one, two = [read_frame(r) for r in responses]
        assert one[0] == two[0] == 'presence.state'
        assert one[1]['state']['turns'] == two[1]['state']['turns']
        assert one[1]['state']['conversation_id'] == two[1]['state']['conversation_id']
        api.devices.revoke(phone['id'])
        name, data = read_frame(responses[0])
        assert name == 'presence.revoked' and data == {'revoked': True}
        assert responses[0].read() == b''
        assert read_frame(responses[1])[0] == 'presence.state'
    finally:
        for response in responses:
            response.close()
        for client in connections:
            client.close()


def test_snapshot_includes_mission_dag_capture_and_unknown_cost(api):
    from nexus.chief import Mission, MissionStep, save_mission
    mission = Mission(id='chief-' + 'a' * 12, goal='Observed DAG', success_criteria=['reviewed'], steps=[
        MissionStep(id='one', title='Plan', role='general', agent='fixture', status='done'),
        MissionStep(id='two', title='Review', role='general', agent='fixture', depends_on=['one'], status='pending')])
    save_mission(api.state.workspace, mission)
    data = api.snapshot(OWNER)['state']
    assert data['missions'][0]['nodes'][1]['depends_on'] == ['one']
    assert data['missions'][0]['nodes'][0]['status'] == 'done'
    assert data['vision']['enabled'] is False
    assert data['approvals'] == []


def subscription():
    def key(raw):
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')
    return {'endpoint': 'https://fcm.googleapis.com/fcm/send/opaque-private-endpoint',
            'keys': {'p256dh': key(b'\x04' + b'p' * 64), 'auth': key(b'a' * 16)}}


def test_push_off_and_endpoint_key_validation(api):
    from nexus.hud.push import validate_subscription
    with pytest.raises(ValueError):
        api.review(OWNER, 'push', {'device': 'anything'})
    cfg = {'push_origins': ['https://fcm.googleapis.com']}
    assert validate_subscription(subscription(), cfg) == subscription()
    for endpoint in ('http://fcm.googleapis.com/foo', 'https://127.0.0.1/foo',
                     'https://fcm.googleapis.com.evil.example/foo', 'https://x@fcm.googleapis.com/foo',
                     'https://fcm.googleapis.com:444/foo', 'https://fcm.googleapis.com/foo#fragment'):
        with pytest.raises(ValueError):
            validate_subscription({**subscription(), 'endpoint': endpoint}, cfg)


def test_service_generation_backoff_and_health_are_offline(repo, monkeypatch):
    from nexus.always_on import backoff, health, require_service
    from nexus.jarvis.services import manifest
    cfg = load(repo)['service']
    assert [backoff(i, cfg) for i in (1, 2, 3, 20)] == [2, 4, 8, 60]
    with pytest.raises(ValueError):
        require_service(repo)
    unit = manifest(repo, 'systemd', target='presence')
    assert '"service"' in unit['content'] and '"run"' in unit['content']
    assert 'Restart=on-failure' in unit['content'] and 'UMask=0077' in unit['content']
    assert MASTER not in unit['content'] and not unit['installed']
    windows = manifest(repo, 'windows', 'DOMAIN\\Operator', target='presence')
    assert 'LeastPrivilege' in windows['content'] and 'InteractiveToken' in windows['content']
    assert '<RestartOnFailure>' in windows['content'] and '<Interval>PT1M</Interval>' in windows['content']
    assert health(repo)['state'] == 'not_observed'
    assert not directory(repo).exists()


def test_doctor_offline_never_runs_provider_or_shell(repo, monkeypatch, capsys):
    from nexus.cli import cmd_doctor
    monkeypatch.setattr('nexus.cli.run_git', lambda *args: pytest.fail('No shell in offline health'))
    monkeypatch.setattr('nexus.providers.build_providers', lambda *args: pytest.fail('No network health probes'))
    assert cmd_doctor(SimpleNamespace(workspace=str(repo), offline=True, json=True)) == 0
    values = json.loads(capsys.readouterr().out)
    assert {r['name'] for r in values} >= {'shared presence', 'always-on service'}
    assert not directory(repo).exists()


def test_pwa_manifest_is_installable_and_has_no_credentials():
    panel = Path(__file__).resolve().parents[1] / 'nexus' / 'panel'
    manifest = json.loads((panel / 'manifest.webmanifest').read_text())
    assert manifest['display'] == 'standalone' and manifest['scope'] == '/'
    assert manifest['start_url'].startswith('/dashboard#') and '?' not in manifest['start_url']
    for icon in manifest['icons']:
        assert (panel / icon['src'].rsplit('/', 1)[-1]).is_file()
    sw = (panel / 'assistant-sw.js').read_text()
    assert "url.pathname.startsWith('/api/')" in sw
    assert 'token=' not in sw


def test_empty_identity_is_shared_and_retention_really_prunes_disk(api):
    one = api.hub.snapshot()['conversation_id']
    assert one == Presence(api.state.workspace).snapshot()['conversation_id']
    enable(api.state.workspace, retention_hours=1)
    hub = Presence(api.state.workspace, clock=lambda: 100_000)
    turn = hub.begin('expires from disk')
    hub.finish(turn, 'not archived')
    hub.voice('idle', transcript='also expires')
    future = Presence(api.state.workspace, clock=lambda: 104_000)
    assert future.snapshot()['turns'] == []
    assert future.snapshot()['voice'] == {}
    raw = future.store.path.read_text()
    assert 'expires from disk' not in raw and 'also expires' not in raw
    assert future.snapshot()['conversation_id'] == one


def test_repeated_completion_never_queues_duplicate_speech(api):
    phone = execute(api, 'enroll', {'name': 'phone'})
    execute(api, 'handoff', {'speaker': phone['id']})
    turn = api.hub.begin('one question')
    api.hub.finish(turn, 'one reply')
    api.hub.finish(turn, 'duplicate must be ignored')
    assert len(api.hub.snapshot()['speech']) == 1
    assert api.hub.snapshot()['speech'][0]['text'] == 'one reply'
    turn = api.hub.begin('failed question')
    api.hub.finish(turn, 'not spoken', ok=False)
    assert len(api.hub.snapshot()['speech']) == 1


def test_streamed_credentials_and_device_tokens_never_become_shared_text(api, monkeypatch):
    secret = 'opaque-account-credential-for-stream-fixture'
    monkeypatch.setenv('TEST_PRIVATE_KEY', secret)
    turn = api.hub.begin('ordinary question')
    api.hub.agent_event(turn, {'type': 'text_delta', 'text': 'Reply ' + secret[:19]})
    assert secret[:19] not in api.hub.store.path.read_text()
    api.hub.agent_event(turn, {'type': 'text_delta', 'text': secret[19:] + ' done'})
    api.hub.finish(turn)
    assert secret not in api.hub.store.path.read_text()
    assert '[REDACTED_SECRET]' in api.hub.snapshot()['turns'][0]['assistant']
    with pytest.raises(ValueError):
        api.review(OWNER, 'chat', {'text': MASTER})
    token = 'nd1_' + 'a' * 24 + '_' + 'b' * 42 + '-'
    with pytest.raises(ValueError):
        api.review(OWNER, 'chat', {'text': token})
    assert api.hub.clean(token) == '[REDACTED_SECRET]'


def test_busy_admission_does_not_leave_an_orphan_session_and_failed_start_is_not_replayed(api, monkeypatch):
    monkeypatch.setattr(api.state, 'start', lambda session: None)
    first = execute(api, 'chat', {'text': 'one'})
    with pytest.raises(ValueError, match='busy'):
        execute(api, 'chat', {'text': 'two'})
    assert list(api.state.sessions) == [first['task_id']]
    api.hub.finish(first['turn_id'])
    def broken(_session):
        raise OSError('offline fixture')
    monkeypatch.setattr(api.state, 'start', broken)
    review = api.review(OWNER, 'chat', {'text': 'failed start'})
    with pytest.raises(OSError):
        api.execute(OWNER, {'id': review['id'], 'confirmation': review['confirmation']})
    assert api.hub.snapshot()['turns'][-1]['status'] == 'error'
    with pytest.raises(ValueError):
        api.execute(OWNER, {'id': review['id'], 'confirmation': review['confirmation']})


def test_local_transcription_is_draft_only_and_rechecks_revocation(api, monkeypatch):
    device = execute(api, 'enroll', {'name': 'phone'})
    principal = api.devices.authenticate(device['token'])
    def transcribe(state, path, body, client):
        assert path == 'voice/transcribe' and body['confirmation'] == 'TRANSCRIBE AUDIO'
        return {'transcript': 'হ্যাঁ চালাও command from untrusted audio', 'language': 'bn', 'verified_speaker': False}
    monkeypatch.setattr('nexus.assistant.api.post', transcribe)
    result = api.post('voice/transcribe', principal, {'audio_base64': 'Zml4dHVyZQ==', 'confirmation': 'TRANSCRIBE AUDIO'})
    assert result['verified_speaker'] is False and not api.state.sessions
    assert api.hub.snapshot()['voice']['transcript'] == result['transcript']
    def revoked(*args):
        api.devices.revoke(device['id'])
        return result
    monkeypatch.setattr('nexus.assistant.api.post', revoked)
    with pytest.raises(ValueError, match='revoked'):
        api.post('voice/transcribe', principal, {'audio_base64': 'Zml4dHVyZQ==', 'confirmation': 'TRANSCRIBE AUDIO'})


@pytest.mark.parametrize('change', ['handoff', 'revoke', 'voice_off'])
def test_speech_synthesis_is_local_once_and_withheld_when_authority_changes(api, monkeypatch, change):
    device = execute(api, 'enroll', {'name': 'phone'})
    principal = api.devices.authenticate(device['token'])
    execute(api, 'handoff', {'speaker': device['id']})
    row = api.hub.say('bounded exact speech')
    def synthesize(text):
        assert text == 'bounded exact speech'
        if change == 'handoff':
            execute(api, 'handoff', {'speaker': 'none'})
        elif change == 'revoke':
            api.devices.revoke(device['id'])
        else:
            enable(api.state.workspace, voice_enabled=False)
        return SimpleNamespace(data=b'fixture-audio', format='wav')
    def tts(state, engine):
        assert engine == 'piper' and state is api.state
        return SimpleNamespace(synthesize=synthesize)
    monkeypatch.setattr('nexus.assistant.service.tts_for', tts)
    with pytest.raises(ValueError):
        api.post('speech', principal, {'id': row['id']})
    assert not api.hub.snapshot()['speech']
    with pytest.raises(ValueError):
        api.hub.claim(row['id'], device['id'])


def test_voice_reference_context_and_active_mission_follow_shared_handoff(api):
    from nexus.chief import Mission, MissionStep, save_mission
    from nexus.voice.conversation import ConversationState
    mission = Mission(id='chief-deadbeefabcd', goal='Selected shared mission', success_criteria=['review'],
                      steps=[MissionStep(id='one', title='Plan', role='general', agent='fixture')])
    save_mission(api.state.workspace, mission)
    execute(api, 'handoff', {'speaker': 'none', 'mission': mission.id})
    turn = api.hub.begin('Which files?')
    api.hub.finish(turn, '1. first.py\n2. second.py')
    voice = ConversationState(workspace=api.state.workspace)
    assert voice.active_mission()['mission_id'] == mission.id
    reference = voice.resolve_reference('Explain the second one')
    assert 'second.py' in reference.resolved_text and not reference.needs_clarification
    enable(api.state.workspace, voice_enabled=False)
    voice.resolve_reference('Hello')
    assert not voice.turns  # Revoked sharing must not leave imported references active.


def test_cached_world_reuses_existing_widget_cache_without_any_network(api, monkeypatch):
    from nexus.hud.presence_world import cached
    from nexus.hud.world.core import World
    with pytest.raises(ValueError, match='OFF'):
        cached(api.state, 'countries', {})
    enable(api.state.workspace, world_enabled=True)
    monkeypatch.setattr(World, 'fetch', lambda *a, **k: pytest.fail('Cache-only HUD never fetches'))
    (api.state.workspace / 'widget.yaml').write_text(yaml.safe_dump({'sources': ['bbc'], 'weather_provider': 'open-meteo'}))
    world = api.state.gui.world()
    assert cached(api.state, 'countries', {})['items'] == world.countries
    assert cached(api.state, 'crypto', {})['data'] is None
    world.cache.rows['crypto'] = {'at': time.time(), 'data': {'provider': 'fixture', 'items': [{'symbol': 'TEST', 'price': 3}]}}
    assert cached(api.state, 'crypto', {})['data']['items'][0]['price'] == 3
    assert cached(api.state, 'weather', {'latitude': ['23'], 'longitude': ['90']})['data'] is None
    assert cached(api.state, 'cities', {'q': ['Maijdi']})['data'] is None
    assert cached(api.state, 'news', {})['cache_only']
    for path, query in [('fetch', {}), ('weather', {'latitude': ['nan'], 'longitude': ['90']}),
                        ('news', {'url': ['https://evil.example']})]:
        with pytest.raises(ValueError):
            cached(api.state, path, query)
    assert not api.hub.snapshot()['turns']


def test_unavailable_first_sse_snapshot_returns_error_instead_of_hanging(live, monkeypatch):
    api, port = live
    def broken(*args):
        raise ValueError('private failure not reflected')
    monkeypatch.setattr(api, 'snapshot', broken)
    status, data, _ = request(port, '/api/presence/events')
    assert status == 409 and 'private failure' not in json.dumps(data)
    assert api.streams == 0


def test_new_master_login_clears_device_cookie_and_explicit_bad_token_cannot_fall_back(live):
    api, port = live
    device = execute(api, 'enroll', {'name': 'phone'})
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    connection.request('POST', '/api/session', '{}', {'X-Nexus-Token': MASTER, 'Cookie': COOKIE + '=' + device['token']})
    response = connection.getresponse()
    assert response.status == 200
    cookies = [v for k, v in response.getheaders() if k.lower() == 'set-cookie']
    response.read()
    connection.close()
    assert any(v.startswith(COOKIE + '=; Max-Age=0') for v in cookies)
    owner_cookie = next(v for v in cookies if not v.startswith(COOKIE + '='))
    assert request(port, '/api/presence/state', 'nd1_invalid', headers={'Cookie': owner_cookie.split(';')[0]})[0] == 401


def test_presence_http_posts_reject_oversized_unknown_and_unscoped_controls(live):
    api, port = live
    assert request(port, '/api/presence/review', body={'x': 'x' * 33_000})[0] == 413
    assert request(port, '/api/presence/review', body={'operation': 'system', 'arguments': {}})[0] == 409
    enable(api.state.workspace, device_scopes=['read'])
    device = execute(api, 'enroll', {'name': 'read-only phone'})
    assert request(port, '/api/presence/review', device['token'], {'operation': 'chat', 'arguments': {'text': 'no'}})[0] == 409
    assert request(port, '/api/presence/voice/state', device['token'], {'state': 'listening'})[0] == 409
    assert request(port, '/api/world/refresh', device['token'], {'confirmation': 'REFRESH PUBLIC SOURCES'})[0] == 403


def test_push_review_scopes_subscription_binding_and_exactly_once(api, monkeypatch):
    enable(api.state.workspace, push_enabled=True, push_origins=['https://fcm.googleapis.com'])
    device = execute(api, 'enroll', {'name': 'phone'})
    principal = api.devices.authenticate(device['token'])
    execute(api, 'subscribe', {'subscription': subscription()}, principal)
    review = api.review(OWNER, 'push', {'device': device['id']})
    calls = []
    monkeypatch.setattr('nexus.hud.push.send', lambda root, sub: calls.append(sub) or {'submitted': True, 'delivered': False})
    body = {'id': review['id'], 'confirmation': review['confirmation']}
    assert api.execute(OWNER, body)['delivered'] is False
    with pytest.raises(ValueError):
        api.execute(OWNER, body)
    assert len(calls) == 1
    review = api.review(OWNER, 'push', {'device': device['id']})
    api.devices.subscribe(device['id'], {**subscription(), 'endpoint': subscription()['endpoint'] + '-changed'})
    with pytest.raises(ValueError, match='changed'):
        api.execute(OWNER, {'id': review['id'], 'confirmation': review['confirmation']})
    assert len(calls) == 1
    assert 'opaque-private-endpoint' not in json.dumps(api.snapshot(OWNER))


def test_push_transport_pins_endpoint_timeout_redirects_logs_and_generic_payload(api, monkeypatch, caplog):
    import logging
    import sys

    from nexus.hud.push import send
    enable(api.state.workspace, push_enabled=True, push_origins=['https://fcm.googleapis.com'])
    monkeypatch.setenv('NEXUS_VAPID_PRIVATE_KEY', 'private-key-fixture-never-print')
    monkeypatch.setenv('NEXUS_VAPID_SUBJECT', 'mailto:operator@example.invalid')
    captured = []
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def send(self, request, **kwargs):
            assert self.trust_env is False and kwargs['allow_redirects'] is False and kwargs['timeout'] == 10
            return SimpleNamespace(status_code=201)
    def webpush(**kwargs):
        captured.append(kwargs)
        session = kwargs['requests_session']
        with pytest.raises(ValueError):
            session.send(SimpleNamespace(url='https://evil.example'))
        logger = logging.getLogger('urllib3.connectionpool')
        logger.warning('secret %s %s', subscription()['endpoint'], os.environ['NEXUS_VAPID_PRIVATE_KEY'])
        try:
            raise RuntimeError(subscription()['endpoint'])
        except RuntimeError:
            logger.exception('POST /opaque-private-endpoint HTTP/1.1')
        logging.getLogger('urllib3.late.created').warning('POST /opaque-private-endpoint HTTP/1.1')
        return session.send(SimpleNamespace(url=kwargs['subscription_info']['endpoint']))
    monkeypatch.setitem(sys.modules, 'requests', SimpleNamespace(Session=Session))
    monkeypatch.setitem(sys.modules, 'pywebpush', SimpleNamespace(webpush=webpush))
    result = send(api.state.workspace, subscription())
    assert result['submitted'] and not result['delivered']
    assert json.loads(captured[0]['data']) == {'title': 'Nexus', 'body': 'Open Nexus to review an update.'}
    assert captured[0]['ttl'] == 60 and captured[0]['verbose'] is False
    assert 'opaque-private-endpoint' not in caplog.text and 'private-key-fixture-never-print' not in caplog.text
    def refused(**kwargs):
        raise RuntimeError(subscription()['endpoint'])
    monkeypatch.setitem(sys.modules, 'pywebpush', SimpleNamespace(webpush=refused))
    with pytest.raises(ValueError, match='no.*automatically') as exc:
        send(api.state.workspace, subscription())
    assert 'opaque-private-endpoint' not in str(exc.value)


def test_png_install_icons_have_actual_sizes_and_maskable_purpose():
    import struct
    panel = Path(__file__).resolve().parents[1] / 'nexus' / 'panel'
    value = json.loads((panel / 'manifest.webmanifest').read_text())
    assert value['start_url'] == '/dashboard#presence'
    assert value['id'] == '/dashboard#assistant'  # Preserve the installed PWA identity.
    assert {r['sizes'] for r in value['icons']} >= {'192x192', '512x512'}
    for row in value['icons']:
        raw = (panel / row['src'].rsplit('/', 1)[-1]).read_bytes()
        assert raw[:8] == b'\x89PNG\r\n\x1a\n'
        width, height = struct.unpack('!II', raw[16:24])
        assert f'{width}x{height}' == row['sizes'] and row['type'] == 'image/png'
    assert any('maskable' in row['purpose'] for row in value['icons'])


@pytest.mark.parametrize('host', ['192.0.2.42', '0.0.0.0', '2001:db8::1'])
def test_no_tokenless_nonloopback_binding_even_on_an_explicit_address(repo, monkeypatch, host):
    from nexus.server import serve
    monkeypatch.delenv('NEXUS_SERVE_TOKEN', raising=False)
    monkeypatch.setattr('nexus.server.ThreadingHTTPServer', lambda *args: pytest.fail('Must refuse before binding'))
    with pytest.raises(ValueError, match='requires'):
        serve(workspace=repo, host=host, quiet=True)


def test_unknown_split_device_credentials_never_publish_a_partial_token(api):
    token = 'nd1_' + 'c' * 24 + '_' + 'd' * 42 + '-'
    turn = api.hub.begin('Mask each streamed word before sharing')
    api.hub.agent_event(turn, {'type': 'text_delta', 'text': 'prefix '})
    for char in token:
        api.hub.agent_event(turn, {'type': 'text_delta', 'text': char})
        assert api.hub.snapshot()['turns'][-1]['assistant'] == 'prefix '
    api.hub.finish(turn)
    assert api.hub.snapshot()['turns'][-1]['assistant'] == 'prefix [REDACTED_SECRET]'
    assert not api.hub._stream_masks


def test_abandoned_stream_buffers_are_discarded_on_timeout_retention_and_off(api):
    hub = api.hub
    start = time.time()
    hub.clock = lambda: start
    ident = hub.begin('incomplete stream')
    hub.agent_event(ident, {'type': 'text_delta', 'text': 'unreleased'})
    assert len(hub._stream_masks) == 1
    hub.clock = lambda: start + 901
    assert hub.snapshot()['turns'][-1]['status'] == 'interrupted'
    assert not hub._stream_masks
    ident = hub.begin('new stream')
    hub.agent_event(ident, {'type': 'text_delta', 'text': 'unreleased'})
    enable(api.state.workspace, conversation_enabled=False)
    hub.agent_event(ident, {'type': 'text_delta', 'text': 'ignored'})
    assert not hub._stream_masks and not hub.snapshot()['turns']
    enable(api.state.workspace)
    ident = hub.begin('third stream')
    hub.agent_event(ident, {'type': 'text_delta', 'text': 'unreleased'})
    enable(api.state.workspace, enabled=False)
    with pytest.raises(ValueError):
        hub.snapshot()
    assert not hub._stream_masks


def test_rotated_secrets_are_pruned_from_voice_speech_and_shared_references(api, monkeypatch):
    from nexus.voice.conversation import ConversationState, PendingClarification
    device = execute(api, 'enroll', {'name': 'phone'})
    execute(api, 'handoff', {'speaker': device['id']})
    text = 'newly-recognized-private-fixture-value'
    first = api.hub.begin('older turn')
    api.hub.finish(first, 'older-file.py')
    last = api.hub.begin('current turn')
    api.hub.finish(last, text)
    api.hub.voice('idle', transcript=text)
    voice = ConversationState(workspace=api.state.workspace)
    voice.resolve_reference('hello')
    assert len(voice.turns) == 2 and voice.turns[-1].assistant_text == text
    monkeypatch.setenv('ROTATED_PRIVATE_KEY', text)
    enable(api.state.workspace, history_turns=1)
    voice.resolve_reference('hello')
    assert len(voice.turns) == 1 and text not in voice.turns[-1].assistant_text
    assert text not in api.hub.store.path.read_text()
    voice.pending_clarification = PendingClarification('repeat it', 'file', ['older-file.py'], 'Which?')
    enable(api.state.workspace, voice_enabled=False)
    voice.resolve_reference('hello')
    assert not voice.turns and voice.pending_clarification is None


def test_local_playback_checks_speaker_again_after_synthesis(api):
    from nexus.voice import VoicePipeline
    from nexus.voice.tts import PlaybackQueue
    execute(api, 'handoff', {'speaker': 'local'})
    played = []
    def synthesize(text, **kwargs):
        execute(api, 'handoff', {'speaker': 'none'})
        return SimpleNamespace(data=b'fixture')
    provider = SimpleNamespace(workspace=api.state.workspace, extra_secrets=(), synthesize=synthesize)
    player = SimpleNamespace(play=lambda *a: played.append(a))
    pipeline = VoicePipeline(workspace=api.state.workspace, tts=provider, player=player)
    pipeline._speak_immediate('safe fixture')
    assert not played
    execute(api, 'handoff', {'speaker': 'local'})
    queue = PlaybackQueue(provider, player, workspace=api.state.workspace, playback_allowed=lambda: api.hub.speaker() == 'local')
    queue.enqueue('safe fixture')
    assert queue.drain_sync() == [] and not played


def test_transcription_withheld_if_voice_switch_is_turned_off_during_inference(api, monkeypatch):
    def transcribe(*args):
        enable(api.state.workspace, voice_enabled=False)
        return {'transcript': 'not returned', 'verified_speaker': False}
    monkeypatch.setattr('nexus.assistant.api.post', transcribe)
    with pytest.raises(ValueError, match='OFF'):
        api.post('voice/transcribe', OWNER, {'audio_base64': 'Zml4dHVyZQ==', 'confirmation': 'TRANSCRIBE AUDIO'})
    assert not api.hub.snapshot()['voice']


def test_all_static_service_worker_precache_urls_are_served_without_personal_data(live):
    import re
    _, port = live
    code = (Path(__file__).resolve().parents[1] / 'nexus/panel/assistant-sw.js').read_text()
    shell = re.findall(r"'(/[^']+)'", code.split('const SHELL = [')[1].split('];')[0])
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        for path in shell:
            assert '?' not in path and '/api/' not in path and '/world/' not in path
            connection.request('GET', path)
            response = connection.getresponse()
            body = response.read()
            assert response.status == 200, path
            assert MASTER.encode() not in body
    finally:
        connection.close()
