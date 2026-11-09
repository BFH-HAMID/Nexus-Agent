"""CLI, literal voice, fresh Studio challenges and existing live approval sessions."""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
import yaml

from nexus import computerstudio as studio
from nexus.computer_actions import registry_for
from nexus.computer_sessions import review, start
from nexus.server import ServerState, make_handler
from nexus.tools import ApprovalDecision, ApprovalGate

from .computer_helpers import FakeDesktop, desktop, home, macro, policy


@pytest.fixture()
def state(repo):
    policy(repo, files={'enabled': True, 'roots': ['.']})
    created = ServerState(repo, token='test-environment-operator-token', quiet=True)
    studio.save_settings(created, {'profile': 'review', 'levels': {'files': 'ask', 'browser': 'off'},
        'scopes': {'roots': ['.'], 'origins': []}, 'revision': studio.load_settings(repo)['revision'],
        'confirmation': 'SAVE COMPUTER POLICY'})
    try:
        yield created
    finally:
        for session in list(created.sessions.values()):
            session.cancel.set()
            session.approval_event.set()
            if session.thread:
                session.thread.join(timeout=5)
        created.mission.close()


def proof(state, operation, payload, *, client='local'):
    result = review(state, {'operation': operation, 'payload': payload, '_client': client})
    return {**payload, 'approval_id': result['id'], 'approval': result['confirmation'], '_client': client}


def test_studio_static_phrase_no_longer_bypasses_fresh_shared_gate(state, repo):
    args = {'path': 'new.txt', 'text': 'hello', 'revision': 'missing', 'confirmation': 'WRITE new.txt'}
    with pytest.raises(ValueError, match='Fresh one-use'):
        studio.write(state, args)
    approved = proof(state, 'file/write', args)
    assert studio.write(state, approved)['snapshot']
    assert (repo / 'new.txt').read_text() == 'hello'
    # Replay fails even for same desired body, and cannot be reused on another action.
    with pytest.raises(ValueError):
        studio.write(state, approved)
    with pytest.raises(ValueError):
        studio.trash(state, {**approved, 'confirmation': 'TRASH new.txt'})


@pytest.mark.parametrize('change', ['payload', 'file', 'policy', 'scope', 'client', 'expiry'])
def test_studio_review_binds_payload_target_policy_scope_client_and_expiry(state, repo, change):
    (repo / 'old.txt').write_text('before')
    args = {'path': 'old.txt', 'confirmation': 'TRASH old.txt'}
    approved = proof(state, 'file/trash', args)
    if change == 'payload':
        (repo / 'other.txt').write_text('other')
        approved.update(path='other.txt', confirmation='TRASH other.txt')
    elif change == 'file':
        (repo / 'old.txt').write_text('new edit')
    elif change == 'policy':
        value = yaml.safe_load((repo / 'permissions.yaml').read_text())
        value['tools'] = {'file_action': 'READ_ONLY'}
        (repo / 'permissions.yaml').write_text(yaml.safe_dump(value))
    elif change == 'scope':
        (repo / 'narrow').mkdir()
        store = studio.load_settings(repo)
        studio.save_settings(state, {'profile': 'review', 'levels': store['levels'], 'scopes': {'roots': ['narrow'], 'origins': []},
                                    'revision': store['revision'], 'confirmation': 'SAVE COMPUTER POLICY'})
    elif change == 'client':
        approved['_client'] = 'different-client'
    else:
        state._computer_reviews._pending[approved['approval_id']]['expires'] = 0
    with pytest.raises(ValueError):
        studio.trash(state, approved)
    assert (repo / 'old.txt').exists()


def test_studio_per_tool_readonly_and_authority_files_are_not_bypassed(state, repo):
    cfg = yaml.safe_load((repo / 'permissions.yaml').read_text())
    cfg['tools'] = {'write_file': 'READ_ONLY'}
    (repo / 'permissions.yaml').write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match='READ_ONLY'):
        proof(state, 'file/write', {'path': 'new.txt', 'text': 'hello', 'revision': 'missing', 'confirmation': 'WRITE new.txt'})
    with pytest.raises(ValueError):
        proof(state, 'file/trash', {'path': 'permissions.yaml', 'confirmation': 'TRASH permissions.yaml'})
    assert not (repo / 'new.txt').exists()


def test_studio_tree_filters_children_not_only_root_and_undo_respects_scope(state, repo):
    (repo / '.env').write_text('not to be listed')
    (repo / 'link').symlink_to(repo / '.env')
    (repo / 'computer-macros').mkdir()
    (repo / 'computer-macros/authority.yaml').write_text('operator only')
    names = {r['name'] for r in studio.tree(state, {'path': '.', 'depth': 4})['entries']}
    assert not names & {'.git', '.env', '.nexus', 'link', 'permissions.yaml', 'computer-macros'}
    args = {'path': 'new.txt', 'text': 'hello', 'revision': 'missing', 'confirmation': 'WRITE new.txt'}
    snapshot = studio.write(state, proof(state, 'file/write', args))['snapshot']
    policy(repo, files={'enabled': True, 'roots': ['app']})
    with pytest.raises(ValueError):
        proof(state, 'file/undo', {'id': snapshot, 'confirmation': 'RESTORE ' + snapshot})
    assert (repo / 'new.txt').read_text() == 'hello'


def test_studio_shared_gate_enforces_second_factor_and_audit_failure(state, repo, monkeypatch):
    args = {'path': 'old.txt', 'confirmation': 'TRASH old.txt'}
    (repo / 'old.txt').write_text('data')
    value = yaml.safe_load((repo / 'permissions.yaml').read_text())
    value['second_factor'] = {'enabled': True, 'method': 'pin'}
    (repo / 'permissions.yaml').write_text(yaml.safe_dump(value))
    with pytest.raises(ValueError):
        studio.trash(state, proof(state, 'file/trash', args))
    assert (repo / 'old.txt').exists()
    value.pop('second_factor')
    (repo / 'permissions.yaml').write_text(yaml.safe_dump(value))
    approved = proof(state, 'file/trash', args)
    monkeypatch.setattr('nexus.securitycenter.append_event', lambda *_a, **_kw: (_ for _ in ()).throw(OSError('audit unavailable')))
    with pytest.raises(ValueError):
        studio.trash(state, approved)
    assert (repo / 'old.txt').exists()


def test_new_dashboard_actions_use_existing_queue_and_live_unique_approvals(state, repo, monkeypatch):
    policy(repo, desktop=desktop(repo))
    native = FakeDesktop(repo)
    monkeypatch.setattr('nexus.desktop_control.backend', lambda *_: native)
    monkeypatch.setattr(state.workbench, 'fingerprint', lambda **_: pytest.fail('No hidden Git/shell probes for a computer task'))
    queued = start(state, {'tool': 'desktop_action', 'arguments': {'app': 'editor', 'action': 'focus'}})
    session = state.get(queued['task_id'])
    with session.changed:
        assert session.changed.wait_for(lambda: session.pending is not None or session.status == 'error', timeout=5)
    assert session.status == 'awaiting_approval' and not native.effects
    assert state.decide_approval(session.task_id, True)[0] == 409
    identifier = session.pending['approval_id']
    assert state.decide_approval(session.task_id, True, approval_id='stale')[0] == 409
    assert state.decide_approval(session.task_id, True, approval_id=identifier)[0] == 200
    session.thread.join(timeout=5)
    assert not session.thread.is_alive() and session.status == 'done', session.result
    assert len(native.effects) == 1 and session.result['input_tokens'] == 0
    assert state.decide_approval(session.task_id, True, approval_id=identifier)[0] == 409


def test_dashboard_macro_has_outer_and_separate_live_step_approvals(state, repo, monkeypatch):
    macro(repo, 'routine', [{'tool': 'write_file', 'args': {'path': 'result.txt', 'content': 'reviewed'}}])
    seen = []
    # Same live callback surface, deterministic offline decisions (no model calls).
    monkeypatch.setattr(state, '_approval', lambda session: lambda spec, args, risk, preview:
                        seen.append(spec.name) or ApprovalDecision(True, scope='session'))
    queued = start(state, {'tool': 'workflow_run', 'arguments': {'name': 'routine'}})
    session = state.get(queued['task_id'])
    session.thread.join(timeout=5)
    assert session.status == 'done', session.result
    assert seen == ['workflow_run', 'write_file'] and (repo / 'result.txt').read_text() == 'reviewed'


def test_studio_cannot_enable_yaml_features_or_write_during_demo(state, repo):
    policy(repo, files={'enabled': False})
    assert studio.effective(state, 'files') == 'off'
    with pytest.raises(ValueError):
        start(state, {'tool': 'home_action', 'arguments': {'adapter': 'mqtt', 'device': 'desk', 'action': 'off'}})
    state.demo = True
    policy(repo, files={'enabled': True, 'roots': ['.']})
    with pytest.raises(ValueError, match='demo'):
        start(state, {'tool': 'file_action', 'arguments': {'action': 'delete', 'path': 'README.md'}})


def test_http_computer_routes_require_token_origin_and_bounded_body(state):
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(state))
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f'http://127.0.0.1:{httpd.server_port}'
    try:
        with httpx.Client(trust_env=False) as client:
            assert client.get(base + '/api/computer/state').status_code == 401
            auth = {'X-Nexus-Token': state.token}
            assert client.post(base + '/api/computer/action', headers={**auth, 'Origin': 'https://evil.example'}, json={}).status_code == 403
            assert client.post(base + '/api/computer/action', headers=auth, json={'padding': 'x' * 550001}).status_code == 413
            assert client.get(base + '/api/computer/state', headers=auth).status_code == 200
            state.token = ''
            assert client.get(base + '/api/computer/state').status_code == 401
            assert client.post(base + '/api/computer/action', json={}).status_code == 401
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=5)


def test_cli_new_tools_dry_run_and_exact_current_target_confirmation(repo, monkeypatch, capsys):
    import hashlib

    from nexus.cli import main
    from nexus.computer_actions import prepare
    cfg = desktop(repo)
    policy(repo, desktop=cfg)
    fake = FakeDesktop(repo)
    monkeypatch.setattr('nexus.desktop_control.backend', lambda *_: fake)
    args = {'app': 'editor', 'action': 'focus'}
    cli = ['agent', '-C', str(repo), 'tool', 'desktop_action', json.dumps(args)]
    assert main(cli) == 0 and not fake.effects
    monkeypatch.setattr('builtins.input', lambda _: 'yes')
    assert main([*cli, '--execute']) == 2 and not fake.effects
    prepared = prepare(registry_for(repo).ctx, 'desktop_action', args)
    phrase = 'RUN desktop_action ' + hashlib.sha256(json.dumps(prepared, sort_keys=True).encode()).hexdigest()[:12]
    monkeypatch.setattr('builtins.input', lambda _: phrase)
    assert main([*cli, '--execute']) == 0 and len(fake.effects) == 1
    assert 'policy_sha256' in capsys.readouterr().out


def test_voice_reuses_role_registry_and_shared_speaker_gate(repo, monkeypatch):
    from nexus.voice.pipeline import build_agent_handler
    from nexus.voice.stt import TranscriptResult
    policy(repo, smart_home=home())
    effects, authorized = [], []
    class Backend:
        def execute(self, device, action, message, guard):
            effects.append(action)
            return {'requested': True}
    monkeypatch.setattr('nexus.smart_home.backend', lambda *_: Backend())
    class Agent:
        def __init__(self, *args, **kwargs):
            self.gate = ApprovalGate(callback=kwargs['approval_callback'])
            self.active_role = SimpleNamespace()
        def tools_for(self, role, **kwargs):
            reg = registry_for(repo, self.gate)
            return reg.ctx, reg, reg.specs()
        def run(self, *args, **kwargs):
            pytest.fail('Literal device alias must not enter a model/tool planning loop')
    monkeypatch.setattr('nexus.agent.NexusAgent', Agent)
    from nexus.voice.persona import load_persona_config
    pipeline = SimpleNamespace(persona=load_persona_config(repo), llm_cancel_event=threading.Event(),
        config=SimpleNamespace(conversation=SimpleNamespace(spoken_approvals=False)),
        make_approval_gate_check=lambda: lambda *_: (False, 'speaker mismatch'),
        authorize_action=lambda risk: authorized.append(risk) or (True, 'verified'))
    handler = build_agent_handler(repo, pipeline=pipeline, approval_callback=lambda *_: ApprovalDecision(True), stream=False)
    with pytest.raises(ValueError, match='speaker mismatch'):
        handler(TranscriptResult('lights off', language='en'))
    assert not effects
    pipeline.make_approval_gate_check = lambda: lambda *_: (True, 'verified')
    assert 'device request sent' in handler(TranscriptResult('lights off', language='en'))
    assert effects == ['turn_off'] and authorized
    handler = build_agent_handler(repo, pipeline=pipeline, read_only=True,
                                  approval_callback=lambda *_: ApprovalDecision(True), stream=False)
    with pytest.raises(ValueError):
        handler(TranscriptResult('lights off', language='en'))
    assert effects == ['turn_off']


def test_live_exact_review_is_not_copied_into_durable_history_or_evidence(state, repo, monkeypatch):
    policy(repo, desktop=desktop(repo))
    native = FakeDesktop(repo)
    monkeypatch.setattr('nexus.desktop_control.backend', lambda *_: native)
    raw = 'private ordinary text belonging only in the live review'
    queued = start(state, {'tool': 'desktop_action', 'arguments': {'app': 'editor', 'action': 'type', 'text': raw}})
    session = state.get(queued['task_id'])
    with session.changed:
        assert session.changed.wait_for(lambda: session.pending is not None or session.status == 'error', timeout=5)
    assert session.pending['args']['text'] == raw and raw in session.pending['preview']
    state.workbench.save_run(session)
    assert raw not in json.dumps(state.workbench.history(task_id=session.task_id))
    assert state.decide_approval(session.task_id, True, approval_id=session.pending['approval_id'])[0] == 200
    session.thread.join(timeout=5)
    assert session.status == 'done' and len(native.effects) == 1
    assert raw in json.dumps(session.log), 'live authenticated review must remain inspectable'
    assert raw not in json.dumps(state.workbench.history(task_id=session.task_id))
    assert raw not in json.dumps(state.workbench.studio.items('evidence'))
    assert state.workbench.history(task_id=session.task_id)[0]['fingerprint'] == ''


def test_computer_durable_output_projection_omits_echoes_process_arguments_and_diffs(state):
    from nexus.computer_actions import history_event
    raw = 'ordinary private draft body echoed in a form result'
    session = state.create('Computer: browser_action', None, False)
    session.computer_request = {'tool': 'browser_action', 'arguments': {}}
    session.result = {'ok': True, 'summary': 'Reviewed action completed', 'text': raw, 'changed_files': ['work/a.txt']}
    for event in (
        {'type': 'tool_result', 'tool': 'browser_action', 'output': raw, 'preview': raw, 'is_error': False},
        {'type': 'process', 'command': ['worker', raw]},
        {'type': 'file_change', 'path': 'work/a.txt', 'diff': raw},
        {'type': 'tool_output', 'tool': 'run_command', 'text': raw},
        {'type': 'done', 'result': session.result},
    ):
        session.push(event)
        assert raw not in json.dumps(history_event(event, computer=True))
        assert raw in json.dumps(event), 'projection must not mutate live data'
    state.workbench.studio.finished(session)
    state.workbench.save_run(session)
    persisted = state.workbench.history(task_id=session.task_id)[0]
    assert persisted['result']['changed_files'] == ['work/a.txt']
    assert raw not in json.dumps(persisted)
    assert raw not in json.dumps(state.workbench.studio.items('evidence'))
    assert raw in session.result['text']


def test_prepared_trace_events_are_metadata_not_replay_payloads(repo):
    from nexus.evaluation.trace import Trace
    from nexus.types import Event, EventType
    raw = 'ordinary private text in a computer request'
    store = SimpleNamespace(save_observation=lambda *_: None)
    trace = Trace(store, repo, 'Review desktop action', lambda: {})
    event = Event(EventType.TOOL_CALL, {'tool': 'desktop_action', 'call_id': 'reviewed', 'args': {'text': raw}, 'preview': raw})
    trace.event(event)
    trace.event(Event(EventType.TOOL_RESULT, {'tool': 'desktop_action', 'call_id': 'reviewed', 'output': raw, 'preview': raw}))
    assert raw not in json.dumps(trace.body)
    assert event.data['args']['text'] == raw
    assert not trace.body['events'][0]['data']['replayable']
    assert trace.body['events'][0]['data']['args_sha256']


def test_approval_center_keeps_complete_bounded_computer_arguments(state):
    from nexus.approvalcenter import MAX_COMPUTER_ARGS, _live_items, _truncate
    raw = 'a' * 30000 + 'END OF EXACT REVIEW'
    session = state.create('Computer: workflow_run', None, False)
    session.computer_request = {'tool': 'workflow_run', 'arguments': {}}
    session.pending = {'approval_id': 'test-review', 'tool': 'workflow_run',
                       'args': {'_review': {'target': raw}}, 'preview': 'Review exact steps'}
    item = _truncate(_live_items(state)[0])
    assert item['args'] == session.pending['args'] and item['scopes'] == ['once']
    assert 'approve' in item['decisions']
    item['args'] = {'too_large': 'x' * MAX_COMPUTER_ARGS}
    assert _truncate(item)['decisions'] == ['deny']
