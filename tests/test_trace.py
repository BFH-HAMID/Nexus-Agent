"""Real agent/gates/SQLite/files/process; model/HTTP usage is explicitly scripted."""
import base64
import json
from types import SimpleNamespace

import pytest

from nexus.evaluation.replay import inspect, reexecute, why
from nexus.evaluation.trace import Trace, sanitize
from nexus.sessions import SessionStore
from nexus.tools import ApprovalDecision
from nexus.types import Completion, TextBlock
from tests.conftest import finish_completion, text_completion, tool_completion
from tests.test_agent import make_agent


def run_file(repo, name='artifact.txt', text='original'):
    agent = make_agent(repo, [tool_completion([('write_file', {'path': name, 'content': text})]), finish_completion('done')])
    result = agent.run('implement ' + name, role='general', learn=False)
    body = agent.sessions.observation(result.trace_id, repo)
    step = next(e['step'] for e in body['events'] if e['kind'] == 'tool_call' and e['data']['tool'] == 'write_file')
    return agent, result, body, step


def test_real_run_has_complete_prompts_tools_results_route_and_matching_session(repo):
    agent, result, body, step = run_file(repo)
    assert result.session_id == result.trace_id and body['state'] == 'finished' and body['complete']
    kinds = {e['kind'] for e in body['events']}
    assert {'role', 'context', 'model_request', 'model_response', 'tool_call', 'tool_result', 'approval_decision'} <= kinds
    assert body['events'][step]['data']['replayable']
    assert agent.providers.request_guard is None and agent.providers.wire_guard is None
    assert not hasattr(agent.fake, 'payload_guard')


def test_inspect_why_and_default_replay_never_execute(repo, monkeypatch):
    agent, result, body, step = run_file(repo)
    monkeypatch.setattr('nexus.evaluation.replay.build_tools', lambda *a: pytest.fail('inspection cannot build executable tools'))
    assert inspect(agent.sessions, repo, result.trace_id, step)['effects'] is False
    assert why(agent.sessions, repo, result.trace_id)['effects'] is False
    assert (repo / 'artifact.txt').read_text() == 'original'


def test_reexecute_is_new_fresh_reviewed_file_operation(repo):
    agent, result, body, step = run_file(repo)
    (repo / 'artifact.txt').write_text('current target to review')
    reviews = []
    def approve(spec, args, *other):
        reviews.append((spec.name, args))
        return ApprovalDecision(True, scope='always')
    output = reexecute(agent.sessions, repo, result.trace_id, step, approve)
    assert output['effects'] and not output['is_error']
    assert reviews[0][0] == 'trace_reexecute' and reviews[0][1]['current_target_sha256']
    assert (repo / 'artifact.txt').read_text() == 'original'
    assert agent.sessions.observation(result.trace_id, repo) == body
    assert len(reviews) >= 2  # Outer consent plus current workspace write gate.


def test_replay_cannot_inherit_original_or_always_scope(repo):
    agent, result, body, step = run_file(repo)
    (repo / 'artifact.txt').write_text('do not overwrite')
    with pytest.raises(ValueError, match='Fresh'):
        reexecute(agent.sessions, repo, result.trace_id, step, lambda *a: ApprovalDecision(False))
    assert (repo / 'artifact.txt').read_text() == 'do not overwrite'


def test_target_changed_during_review_is_not_overwritten(repo):
    agent, result, body, step = run_file(repo)
    def race(*args):
        (repo / 'artifact.txt').write_text('changed during review')
        return ApprovalDecision(True)
    with pytest.raises(ValueError, match='changed during review'):
        reexecute(agent.sessions, repo, result.trace_id, step, race)
    assert (repo / 'artifact.txt').read_text() == 'changed during review'


def test_replay_obeys_current_read_only_policy(repo):
    agent, result, body, step = run_file(repo)
    (repo / 'permissions.yaml').write_text('default: READ_ONLY\n')
    with pytest.raises(ValueError):
        reexecute(agent.sessions, repo, result.trace_id, step, lambda *a: ApprovalDecision(True))


@pytest.mark.parametrize('state,complete', [('running', True), ('error', True), ('finished', False)])
def test_uncertain_or_incomplete_trace_refuses_execution(repo, state, complete):
    agent, result, body, step = run_file(repo)
    body.update(state=state, complete=complete)
    agent.sessions.save_observation(result.trace_id, repo, body)
    with pytest.raises(ValueError):
        reexecute(agent.sessions, repo, result.trace_id, step, lambda *a: ApprovalDecision(True))


def test_cross_workspace_trace_cannot_be_read_or_replayed(repo, tmp_path):
    agent, result, body, step = run_file(repo)
    with pytest.raises(ValueError, match='workspace'):
        inspect(agent.sessions, tmp_path / 'other', result.trace_id)


def test_terminal_or_external_action_cannot_be_replayed_even_with_consent(repo):
    agent, result, body, step = run_file(repo)
    body['events'][step]['data']['tool'] = 'run_command'
    agent.sessions.save_observation(result.trace_id, repo, body)
    with pytest.raises(ValueError, match='not replayable'):
        reexecute(agent.sessions, repo, result.trace_id, step, lambda *a: ApprovalDecision(True))


def test_changed_capability_or_redacted_argument_refuses_replay(repo):
    agent, result, body, step = run_file(repo)
    body['events'][step]['data']['spec_sha256'] = '0' * 64
    agent.sessions.save_observation(result.trace_id, repo, body)
    with pytest.raises(ValueError, match='capability changed'):
        reexecute(agent.sessions, repo, result.trace_id, step, lambda *a: ApprovalDecision(True))
    body['events'][step]['data']['args']['content'] = '[REDACTED_SECRET]'
    agent.sessions.save_observation(result.trace_id, repo, body)
    with pytest.raises(ValueError, match='redactions'):
        reexecute(agent.sessions, repo, result.trace_id, step, lambda *a: ApprovalDecision(True))


def test_known_escaped_encoded_secrets_and_images_not_persisted(repo, monkeypatch):
    secret = 'sensitive"key\\value1234'
    monkeypatch.setenv('TRACE_API_KEY', secret)
    trace = Trace(SessionStore(), repo, secret, lambda: {})
    trace.add('untrusted', {'body': json.dumps(secret), 'encoded': base64.b64encode(secret.encode()).decode(), 'image': {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,PRIVATEPIXELS'}}})
    trace.finish()
    stored = json.dumps(trace.store.observation(trace.id, repo))
    assert secret not in stored and 'PRIVATEPIXELS' not in stored and 'sensitive' not in stored
    assert '[IMAGE_OMITTED]' in stored


def test_unknown_usage_and_unknown_prices_are_not_free(repo):
    trace = Trace(SessionStore(), repo, 'task', lambda: {})
    trace.request('local', 'system', [], [])
    trace.response('local', Completion(model='model', content=[TextBlock('done')]))
    trace.finish()
    report = trace.store.evaluation_report(repo)['measured']
    assert report['calculated_model_cost_usd'] is None and report['unknown_cost_calls'] == 1
    assert report['models_by_utc_day'][0]['unknown_usage_calls'] == 1


def test_exact_reported_tokens_use_immutable_price_snapshot_and_group_by_model_day(repo):
    rates = {'model': {'source': 'operator test rate', 'input_per_million': 1, 'output_per_million': 2}}
    trace = Trace(SessionStore(), repo, 'task', lambda: rates)
    trace.request('local', 'system', [], [])
    rates['model']['input_per_million'] = 999
    trace.wire('local', {'model': 'model'})
    trace.response('local', Completion(model='model', input_tokens=100, output_tokens=50))
    trace.finish()
    report = trace.store.evaluation_report(repo)['measured']
    assert report['calculated_model_cost_usd'] == pytest.approx(.0002)
    assert report['actual_cost_usd'] is None and report['models_by_utc_day'][0]['known_input_tokens'] == 100


def test_wire_retry_can_have_unknown_charges_and_is_counted(repo):
    trace = Trace(SessionStore(), repo, 'task', lambda: {'model': {'source': 'test', 'input_per_million': 0, 'output_per_million': 0}})
    trace.request('local', 's', [], [])
    trace.wire('local', {})
    trace.wire('local', {})
    trace.response('local', Completion(model='model', input_tokens=10, output_tokens=2))
    trace.finish()
    report = trace.store.evaluation_report(repo)['measured']
    assert report['totals']['wire_retries'] == 1 and report['calculated_model_cost_usd'] is None


def test_measured_human_review_changes_existing_quality_without_role_deletion(repo):
    agent, result, body, step = run_file(repo)
    before = agent.sessions.role_quality(repo)['general']['score']
    agent.sessions.assess(result.trace_id, repo, False, 'Actual operator rejected change')
    assert agent.sessions.role_quality(repo)['general']['score'] < before
    assert agent.sessions.evaluation_report(repo)['measured']['totals']['human_reviews'] == 1


def test_observation_bounds_mark_trace_incomplete_not_silently_full(repo):
    trace = Trace(SessionStore(), repo, 'task', lambda: {})
    trace.add('oversize', {'text': 'x' * 65000, 'values': list(range(1002))})
    trace.finish()
    assert trace.store.observation(trace.id, repo)['complete'] is False
    assert '[TRUNCATED_LIST]' in json.dumps(trace.store.observation(trace.id, repo))


def test_peer_review_requires_distinct_finished_trace_and_evidence(repo):
    agent, result, body, step = run_file(repo)
    with pytest.raises(ValueError):
        agent.sessions.peer_review(result.trace_id, repo, result.trace_id, True, 'self review', ['evidence'])


def test_session_record_redacts_task_summary_and_transcript(repo, monkeypatch):
    monkeypatch.setenv('TRACE_API_KEY', 'a-real-secret-value')
    store = SessionStore()
    store.record(SimpleNamespace(text='a-real-secret-value', summary='a-real-secret-value'), 'a-real-secret-value', workspace=repo)
    assert 'a-real-secret-value' not in json.dumps(store.recent())


def test_cli_default_replay_is_only_inspection(repo, capsys):
    from nexus.evaluation.cli import command
    agent, result, body, step = run_file(repo)
    assert command(SimpleNamespace(workspace=str(repo), days=7, operation='replay', identifier=result.trace_id, step=step, execute=False)) == 0
    assert json.loads(capsys.readouterr().out)['effects'] is False


def test_sanitize_preserves_sha_but_not_credentials_and_card_numbers(repo):
    digest = 'abcdef' + '1' * 16 + 'abcdef' * 7
    assert len(digest) == 64 and sanitize(digest, repo) == digest
    assert '4111' not in sanitize('4111 1111 1111 1111', repo)


def test_usage_object_without_actual_token_fields_does_not_invent_zero(repo):
    trace = Trace(SessionStore(), repo, 'task', lambda: {'model': {'source': 'test', 'input_per_million': 1, 'output_per_million': 1}})
    trace.request('local', 's', [], [])
    trace.response('local', Completion(model='model', raw={'usage': {'cost': 99}}))
    trace.finish()
    assert trace.store.evaluation_report(repo)['measured']['calculated_model_cost_usd'] is None


def test_explicit_zero_token_usage_at_known_zero_price_is_not_unknown(repo):
    trace = Trace(SessionStore(), repo, 'task', lambda: {'model': {'source': 'test', 'input_per_million': 0, 'output_per_million': 0}})
    trace.request('local', 's', [], [])
    trace.response('local', Completion(model='model', raw={'usage': {'prompt_tokens': 0, 'completion_tokens': 0}}))
    trace.finish()
    assert trace.store.evaluation_report(repo)['measured']['calculated_model_cost_usd'] == 0


def test_peer_review_is_labelled_peer_not_human_and_affects_quality(repo):
    agent, result, body, step = run_file(repo)
    reviewer = make_agent(repo, [text_completion('review verdict')])
    review_result = reviewer.run('review only', role='general', learn=False)
    before = agent.sessions.role_quality(repo)['general']['score']
    agent.sessions.peer_review(result.trace_id, repo, review_result.trace_id, False, 'actual evidence rejected', ['actual-task-id'])
    report = agent.sessions.evaluation_report(repo)['measured']
    assert report['totals']['peer_reviews'] == 1 and report['totals']['human_reviews'] == 0
    assert agent.sessions.role_quality(repo)['general']['score'] < before


def test_trace_summary_budget_avoids_loading_full_prompts_for_hud(repo):
    trace = Trace(SessionStore(), repo, 'task', lambda: {})
    trace.add('model_request', {'system': 'PRIVATE PROMPT TEXT'})
    trace.finish()
    rows, truncated = trace.store.observations(repo, summaries=True)
    assert not truncated and 'events' not in rows[0]
    assert 'PRIVATE PROMPT TEXT' not in json.dumps(rows)


def test_crash_before_finish_remains_running_unknown_and_cannot_execute(repo):
    trace = Trace(SessionStore(), repo, 'interrupted worker', lambda: {})
    trace.wire('local', {'model': 'unknown'})
    report = trace.store.evaluation_report(repo)['measured']
    assert report['totals']['unfinished_runs'] == 1 and report['calculated_model_cost_usd'] is None


def test_hud_trace_endpoint_auth_workspace_and_no_execute_surface(repo):
    from fastapi.testclient import TestClient

    from nexus.hud.app import create_app
    from nexus.server import ServerState
    agent, result, body, step = run_file(repo)
    token = 'test-only-trace-hud-private-bearer-token'
    client = TestClient(create_app(ServerState(repo, quiet=True).workbench.studio, token), base_url='http://localhost')
    path = '/api/trace/' + result.trace_id
    assert client.get(path).status_code == 401
    headers = {'Authorization': 'Bearer ' + token}
    assert client.get(path + '?operation=why', headers=headers).json()['effects'] is False
    assert client.get(path + '?operation=replay&step=' + str(step), headers=headers).json()['effects'] is False
    assert client.get(path + '?operation=execute', headers=headers).status_code == 400
    assert client.get('/api/trace/not-this-workspace', headers=headers).status_code == 404


def test_scope_composes_and_restores_existing_policy_and_response_observers(repo):
    agent = make_agent(repo, [text_completion('done')])
    seen = []
    def guard(*args):
        seen.append('guard')
    def observer(*args):
        seen.append('response')
    agent.providers.request_guard = guard
    agent.providers.response_observer = observer
    agent.run('explain', role='general', learn=False)
    assert seen == ['guard', 'response']
    assert agent.providers.request_guard is guard and agent.providers.response_observer is observer


def test_numeric_pin_and_card_cannot_hide_in_untrusted_json(repo, monkeypatch):
    monkeypatch.setenv('TELEGRAM_APPROVAL_PIN', '86420975')
    value = sanitize({'untrusted': 86420975, 'card': 4111111111111111}, repo)
    assert '86420975' not in json.dumps(value) and '4111' not in json.dumps(value)


def test_replay_requires_a_selected_step_before_building_tools(repo):
    agent, result, body, step = run_file(repo)
    with pytest.raises(ValueError, match='Explicit recorded'):
        reexecute(agent.sessions, repo, result.trace_id, None, lambda *a: ApprovalDecision(True))


def test_explicit_reexecution_gets_its_own_observed_journal(repo):
    agent, result, body, step = run_file(repo)
    reply = reexecute(agent.sessions, repo, result.trace_id, step, lambda *a: ApprovalDecision(True))
    new = agent.sessions.observation(reply['replay_trace'], repo)
    assert new['kind'] == 'reexecution' and new['parent_trace'] == result.trace_id
    assert new['approvals']['allowed'] >= 2 and new['state'] == 'finished'


def test_migrates_existing_session_database_without_replacing_learning(repo, tmp_path):
    import sqlite3
    path = tmp_path / 'old-learning.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE session_observations (id TEXT PRIMARY KEY, workspace TEXT, body TEXT)')
    store = SessionStore(path)
    trace = Trace(store, repo, 'new scope', lambda: {})
    trace.finish()
    assert store.observation(trace.id, repo)['state'] == 'finished'
    assert store.trace_summaries(repo)['items'][0]['id'] == trace.id


@pytest.mark.parametrize('reason,evidence', [
    ('reviewed', 'not-a-list'), ('reviewed', {'id': 'evidence'}),
    ('reviewed', [None]), ('reviewed', ['']), ('reviewed', ['x' * 201]),
    (None, ['evidence']), ('   ', ['evidence']), ('x' * 4001, ['evidence']),
])
def test_peer_review_rejects_malformed_evidence_without_changing_learning(repo, reason, evidence):
    store = SessionStore()
    target = Trace(store, repo, 'target', lambda: {})
    reviewer = Trace(store, repo, 'reviewer', lambda: {})
    target.finish()
    reviewer.finish()
    before = store.observation(target.id, repo)
    with pytest.raises(ValueError):
        store.peer_review(target.id, repo, reviewer.id, True, reason, evidence)
    assert store.observation(target.id, repo) == before


def test_workflow_review_phase_links_actual_agent_traces_to_existing_learning_store(repo):
    from nexus.server import ServerState
    from tests.test_orchestration import reviewed_definition
    agent, result, body, step = run_file(repo)
    verdict = {'approved': True, 'reason': 'Read actual changed artifact in a distinct scripted reviewer run',
               'evidence_ids': [result.session_id]}
    reviewer = make_agent(repo, [tool_completion([('read_file', {'path': 'artifact.txt'})]),
                                 text_completion(json.dumps(verdict))])
    reviewed = reviewer.run('Review artifact.txt against the supplied run evidence', role='general', read_only=True, learn=False)
    assert reviewed.ok and reviewed.trace_id != result.trace_id
    studio = ServerState(repo, quiet=True).workbench.studio
    definition = reviewed_definition(studio)
    node = next(n for n in definition['nodes'] if n['id'] == 'review')
    record = {'id': 'explicit-scripted-review-integration', 'definition': definition}
    value = {'ok': reviewed.ok, 'task_id': reviewed.session_id,
             'result': {'text': reviewed.text, 'trace_id': reviewed.trace_id}}
    targets = {'build': {'ok': result.ok, 'task_id': result.session_id,
                         'result': {'trace_id': result.trace_id}}}
    outcome = studio.workflows.review_phase(record, node, value, targets)
    assert outcome['review_approved'] is True
    saved = agent.sessions.observation(result.trace_id, repo)['reviews']
    assert len(saved) == 1 and saved[0]['reviewer_trace'] == reviewed.trace_id
    assert saved[0]['approved'] is True and 'not independent test certification' in saved[0]['source']
    totals = agent.sessions.evaluation_report(repo)['measured']['totals']
    assert totals['peer_reviews'] == 1 and totals['human_reviews'] == 0
