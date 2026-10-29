"""Real local FAISS + scripted LLMs; no live models or privileged tool execution."""
import json

import numpy as np
import pytest

from nexus.config import ConfigError, load_config
from nexus.memory import MemoryStore
from nexus.orchestration.index import RoleIndex, metadata, plan_dir, runtime_dir, settings
from nexus.orchestration.planner import Orchestrator
from nexus.server import ServerState
from nexus.sessions import SessionStore
from nexus.types import Role
from tests.conftest import FakeProvider, fake_registry, text_completion


def index_for(repo, config=None):
    config = config or load_config(repo)
    options = settings(repo)
    options['encoder'] = 'lexical-hash'
    index = RoleIndex(config, repo, options)
    index.ensure()
    return index


def choices(call, confidence=.9):
    payload = json.loads(call['messages'][-1].content)
    return text_completion(json.dumps({'roles': [
        {'id': c['id'], 'reason': 'Matches supplied metadata', 'confidence': confidence}
        for c in payload['candidates'][:3]], 'confidence': confidence,
        'question': 'Which deployment target should this use?' if confidence < .65 else ''}))


def service_for(repo, tmp_path, scripts=None):
    index = index_for(repo)
    config = index.config
    name = config.settings.router_agent or config.default_agent
    script = scripts or [text_completion(json.dumps({'task_type': 'web project', 'phase': 'requirements', 'complexity': 'complex'})), choices]
    provider = FakeProvider(name, script)
    service = Orchestrator(config, repo, index, fake_registry(provider, config=config),
        sessions=SessionStore(tmp_path / 'learning.db'), memory=MemoryStore(tmp_path / 'memories'))
    return service, provider


def test_metadata_is_lightweight_not_a_second_role_registry(repo):
    config = load_config(repo)
    original = config.role('backend').persona
    config.role('backend').persona = 'Backend specialist. ' + 'x' * 500 + 'FULL_PERSONA_TAIL_NOT_INDEXED'
    records = metadata(config)
    assert len(records) == len(config.roles)
    assert all(set(r) == {'id', 'name', 'category', 'tags', 'description', 'skills', 'tools_needed'} for r in records)
    assert 'FULL_PERSONA_TAIL_NOT_INDEXED' not in json.dumps(records)
    assert 'FULL_PERSONA_TAIL_NOT_INDEXED' in config.role('backend').persona
    config.role('backend').persona = original


def test_real_faiss_hybrid_index_and_cache_invalidation(repo):
    index = index_for(repo)
    assert index.vector.ntotal == len(index.config.roles)
    candidates = index.search('backend python api endpoints and postgres database')
    assert len(candidates) == 15 and all('embedding_score' in c and 'keyword_score' in c for c in candidates)
    assert index.ensure()['cached'] is True
    index.config.roles.add(Role(name='brand-new-specialist', persona='Quantum physics tutoring', category='study'))
    updated = index_for(repo, index.config)
    assert updated.signature != index.signature
    assert any(r['id'] == 'brand-new-specialist' for r in updated.records)
    assert updated.vector.ntotal == len(updated.records)


def test_corrupt_cache_is_not_silently_trusted(repo):
    index = index_for(repo)
    (index.root / 'vectors.npy').write_bytes(b'not a trusted vector array')
    with pytest.raises(ConfigError, match='Corrupt'):
        index.ensure()
    assert index.ensure(force=True)['cached'] is False


@pytest.mark.parametrize('vectors', [[[0., 0.]], [[float('nan'), 1.]], [[float('inf'), 1.]], [1., 2.]])
def test_bad_embedding_vectors_fail_closed(repo, vectors):
    index = index_for(repo)
    with pytest.raises(ConfigError):
        index.normalize(vectors, 1)


def test_embedding_dimension_drift_is_detected(repo):
    index = index_for(repo)
    index.embedder = lambda texts: np.ones((len(texts), 9), dtype='float32')
    with pytest.raises(ConfigError, match='dimensions changed'):
        index.search('backend api')


def test_embedding_config_refuses_cloud_and_credentials(repo):
    for url in ('https://example.com', 'http://user:pass@127.0.0.1:11434'):
        (repo / 'role-index.yaml').write_text('base_url: ' + url)
        with pytest.raises(ConfigError, match='loopback'):
            settings(repo)


def test_existing_learning_database_feedback_affects_quality_and_is_workspace_scoped(repo, tmp_path):
    store = SessionStore(tmp_path / 'existing-learning.db')
    store.record_routing_feedback('build API', ['backend'], False, workspace=repo)
    assert store.role_quality(repo)['backend']['score'] < .5
    store.record_routing_feedback('build UI', ['frontend'], True, workspace=repo)
    assert store.role_quality(repo)['frontend']['score'] > .5
    assert store.role_quality(tmp_path / 'different-project') == {}
    assert store.stats()['sessions'] == 0  # Feedback is not fabricated successful task history.


def test_quality_is_a_small_ranking_factor_and_new_roles_keep_exploration(repo):
    index = index_for(repo)
    hits = index.search('backend api', quality={'backend': {'score': .1, 'observations': 100}})
    assert len(hits) == 15
    allowed = {'frontend', 'backend', 'qa'}
    scoped = index.search('frontend UI', allowed=allowed)
    assert {h['id'] for h in scoped} == allowed


def test_router_sends_only_top15_metadata_and_selects_three_roles(repo, tmp_path):
    service, provider = service_for(repo, tmp_path)
    routed = service.route('Build an e-commerce website with login, admin panel and payment')
    assert len(provider.calls) == 2 and len(routed['candidates']) == 15 and len(routed['roles']) == 3
    payload = json.loads(provider.calls[1]['messages'][-1].content)
    assert len(payload['candidates']) == 15
    assert all('persona' not in r for r in payload['candidates'])
    assert not routed['needs_clarification']


def test_low_confidence_asks_one_question_and_cannot_plan(repo, tmp_path):
    classifier = text_completion(json.dumps({'task_type': 'project', 'phase': 'requirements', 'complexity': 'complex'}))
    service, _ = service_for(repo, tmp_path, [classifier, lambda call: choices(call, .2)])
    route = service.route('Build a web application')
    assert route['needs_clarification'] and route['question'].count('?') == 1
    with pytest.raises(ConfigError, match='clarifying question'):
        service.plan(route)


@pytest.mark.parametrize('bad', ['not JSON', '{"phase":"unknown","complexity":"complex","task_type":"x"}'])
def test_malformed_classifier_never_becomes_a_plan(repo, tmp_path, bad):
    service, _ = service_for(repo, tmp_path, [text_completion(bad)])
    with pytest.raises(ConfigError):
        service.route('Build the project')


def test_unknown_selected_roles_are_rejected(repo, tmp_path):
    classifier = text_completion(json.dumps({'task_type': 'project', 'phase': 'requirements', 'complexity': 'complex'}))
    unknown = text_completion(json.dumps({'roles': [{'id': 'invented', 'reason': 'x', 'confidence': .9}] * 3, 'confidence': .9}))
    service, _ = service_for(repo, tmp_path, [classifier, unknown])
    with pytest.raises(ConfigError, match='unknown'):
        service.route('Build a web application')


def test_shared_project_plan_uses_existing_workflow_schema_with_reviews(repo, tmp_path):
    service, _ = service_for(repo, tmp_path)
    routed = service.route('Build an e-commerce website with login, admin panel and payment')
    plan = service.plan(routed)
    studio = ServerState(repo, quiet=True).workbench.studio
    normalized, order = studio.workflows.validate(plan)
    assert len(normalized['nodes']) == 20 and len(order) == 20
    nodes = {n['id']: n for n in normalized['nodes']}
    assert nodes['design']['depends_on'] == nodes['database']['depends_on'] == ['review_architecture']
    assert nodes['review_backend']['review_of'] == 'backend' and nodes['review_backend']['max_retries'] == 3
    assert nodes['review_backend']['read_only'] and nodes['review_backend']['condition'] == 'always'
    assert 'write_file' not in nodes['review_backend']['allowed_tools']
    assert nodes['backend']['inputs'] and nodes['backend']['acceptance_criteria']
    root = service.save(routed, normalized)
    assert json.loads((root / 'plan.json').read_text())['status'] == 'planned-not-executed'
    assert (root / 'PLAN.md').exists() and studio.items('workflow-run') == []


def test_plan_directory_cannot_escape_workspace_through_symlink(repo, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    root = runtime_dir(repo)
    (root / 'plans').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigError, match='inside the workspace'):
        plan_dir(repo, 'a' * 32)
    assert not list(outside.iterdir())


def reviewed_definition(studio):
    return studio.workflows.validate({'name': 'Phase review', 'project_plan_id': 'b' * 32, 'nodes': [
        {'id': 'build', 'kind': 'agent', 'role': 'general', 'task': 'Implement actual code'},
        {'id': 'review', 'kind': 'reviewer', 'role': 'qa', 'task': 'Review evidence',
         'condition': 'always', 'depends_on': ['build'], 'review_of': 'build', 'max_retries': 3},
    ]})[0]


def test_failed_review_retries_responsible_role_and_then_passes(repo, monkeypatch):
    studio = ServerState(repo, quiet=True).workbench.studio
    counts = {'build': 0, 'review': 0}
    def node(run_id, task, handoff):
        counts[task['id']] += 1
        if task['id'] == 'build':
            return {'ok': True, 'task_id': f"build-{counts['build']}", 'result': {'text': 'actual tool evidence'}}
        approved = counts['review'] > 1
        return {'ok': True, 'task_id': 'review-task', 'result': {'text': json.dumps({'approved': approved,
            'reason': 'Verified evidence' if approved else 'Needs repair', 'evidence_ids': [handoff['build']['task_id']]})}}
    monkeypatch.setattr(studio.workflows, 'node', node)
    record = studio.put('workflow-run', {'definition': reviewed_definition(studio), 'nodes': {}, 'status': 'running'})
    result = studio.workflows.execute(record)
    assert result['status'] == 'done' and counts == {'build': 2, 'review': 2}
    assert result['nodes']['review']['repair_retries'] == 1
    status = json.loads((plan_dir(repo, 'b' * 32) / 'status.json').read_text())
    assert status['status'] == 'done'


def test_review_cannot_accept_fake_evidence_and_escalates_after_three_repairs(repo, monkeypatch):
    studio = ServerState(repo, quiet=True).workbench.studio
    calls, escalations = [], []
    def node(run_id, task, handoff):
        calls.append(task['id'])
        if task['id'] == 'build':
            return {'ok': True, 'task_id': 'real-build', 'result': {'text': 'inspect'}}
        return {'ok': True, 'result': {'text': json.dumps({'approved': True, 'reason': 'fake pass', 'evidence_ids': ['invented-evidence']})}}
    monkeypatch.setattr(studio.workflows, 'node', node)
    monkeypatch.setattr(studio, 'ask', lambda *a, **k: escalations.append(a) or 'Stop for manual investigation')
    record = studio.put('workflow-run', {'definition': reviewed_definition(studio), 'nodes': {}, 'status': 'running'})
    result = studio.workflows.execute(record)
    assert calls.count('build') == 4 and calls.count('review') == 4
    assert len(escalations) == 1 and result['status'] == 'failed'
    assert result['nodes']['review']['escalated'] and not result['nodes']['review']['ok']


def test_workflow_allowed_tools_does_not_expand_role_permissions(repo):
    from nexus.agent import NexusAgent
    studio = ServerState(repo, quiet=True).workbench.studio
    session = studio.state.create('Inspect code', 'general', False, options=studio.wb.prepare({}))
    session.options['allowed_tools'] = ['read_file', 'nonexistent_privileged_tool']
    agent = NexusAgent(workspace=repo, auto_yes=True)
    studio.attach(agent, session)
    _, registry, _ = agent.tools_for(agent.config.role('general'))
    assert registry.has('read_file') and registry.has('finish')
    assert not registry.has('write_file') and not registry.has('run_command') and not registry.has('nonexistent_privileged_tool')


def test_cli_index_has_no_llm_or_project_execution(repo, capsys):
    from nexus.cli import main
    assert main(['orchestrate', '--workspace', str(repo), '--encoder', 'lexical-hash', 'index']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['roles'] == len(load_config(repo).roles) and output['encoder'] == 'lexical-hash'


def test_task_secrets_redacted_before_embedding_routing_and_artifacts(repo, tmp_path):
    service, provider = service_for(repo, tmp_path)
    secret = 'ghp_' + 'a' * 36
    inputs = []
    original = service.index.embedder
    def capture(texts):
        inputs.extend(texts)
        return original(texts)
    service.index.embedder = capture
    routed = service.route('Build an API using token ' + secret)
    assert secret not in json.dumps(inputs) and '[REDACTED_SECRET]' in json.dumps(inputs)
    assert all(secret not in json.dumps(call['messages'][-1].content) for call in provider.calls)
    root = service.save(routed, service.plan(routed))
    assert secret not in (root / 'plan.json').read_text() and secret not in (root / 'PLAN.md').read_text()


def test_cli_obeys_persisted_local_only_before_constructing_providers(repo, monkeypatch, capsys):
    from nexus.cli import main
    from nexus.orchestration import cli
    studio = ServerState(repo, quiet=True).workbench.studio
    studio.wb.save_settings({'local_only': True, 'default_local_model': 'test:8b'})
    seen = []
    def providers(agents):
        seen.append(agents)
        provider = FakeProvider('selected-ollama', [text_completion(json.dumps({'task_type': 'project', 'phase': 'requirements', 'complexity': 'complex'})), choices])
        return fake_registry(provider)
    monkeypatch.setattr(cli, 'build_providers', providers)
    assert main(['orchestrate', '-C', str(repo), '--encoder', 'lexical-hash', 'plan', 'Build a web application']) == 0
    assert set(seen[0]) == {'selected-ollama'}
    assert all(a.fallback is None for a in seen[0].values())
    output = json.loads(capsys.readouterr().out)
    assert not output['executed']
    assert {n['agent'] for n in output['definition']['nodes']} == {'selected-ollama'}


def test_simple_task_requires_local_agent_or_explicit_model_choice(repo, tmp_path):
    service, _ = service_for(repo, tmp_path)
    # Remove configured local adapters; never quietly invent a local model.
    service.config.agents = {n: a for n, a in service.config.agents.items() if a.provider != 'openai-compatible'}
    with pytest.raises(ConfigError, match='configured local adapter'):
        service.agent_for('backend', 'simple')
    service.index.options['simple_agent'] = service.config.default_agent
    assert service.agent_for('backend', 'simple') == service.config.default_agent
    service.index.options['complex_agent'] = 'unavailable'
    with pytest.raises(ConfigError, match='unavailable'):
        service.agent_for('backend', 'complex')


def test_feedback_redacted_and_follows_existing_retention(repo, tmp_path):
    import sqlite3
    store = SessionStore(tmp_path / 'existing.db')
    secret = 'ghp_' + 'z' * 36
    store.record_routing_feedback(secret, ['backend'], True, workspace=repo, reason=secret)
    with sqlite3.connect(store.path) as conn:
        row = conn.execute('SELECT task,reason FROM routing_feedback').fetchone()
        assert secret not in str(row)
        conn.execute('UPDATE routing_feedback SET created=0')
    store.purge(before_days=90)
    assert store.role_quality(repo) == {}


def test_cancelled_review_never_repairs_or_opens_new_question(repo, monkeypatch):
    studio = ServerState(repo, quiet=True).workbench.studio
    definition = reviewed_definition(studio)
    record = studio.put('workflow-run', {'definition': definition, 'nodes': {}, 'status': 'running'})
    studio.workflows.cancel(record['id'])
    monkeypatch.setattr(studio, 'ask', lambda *a, **k: pytest.fail('cancelled run cannot request decisions'))
    monkeypatch.setattr(studio.workflows, 'node', lambda *a, **k: pytest.fail('cancelled run cannot repair'))
    result = studio.workflows.review_phase(record, definition['nodes'][1], {'ok': False}, {'build': {'ok': False}})
    assert not result['ok'] and result['status'] == 'cancelled'


def test_review_timeout_is_a_blocked_result_not_workflow_crash(repo, monkeypatch):
    studio = ServerState(repo, quiet=True).workbench.studio
    definition = reviewed_definition(studio)
    definition['nodes'][1]['max_retries'] = 0
    record = studio.put('workflow-run', {'definition': definition, 'nodes': {}, 'status': 'running'})
    def timeout(*a, **k):
        raise ValueError('Clarification cancelled or timed out')
    monkeypatch.setattr(studio, 'ask', timeout)
    value = {'ok': True, 'result': {'text': json.dumps({'approved': True, 'reason': 'wrong', 'evidence_ids': ['failed-task']})}}
    result = studio.workflows.review_phase(record, definition['nodes'][1], value, {'build': {'ok': False, 'task_id': 'failed-task'}})
    assert result['status'] == 'blocked' and not result['ok'] and result['escalated']


def test_invalid_workflow_model_tools_and_review_contracts_are_rejected(repo):
    studio = ServerState(repo, quiet=True).workbench.studio
    definition = reviewed_definition(studio)
    for mutation in ({'agent': 'unavailable'}, {'allowed_tools': 'run_command'}, {'review_of': 'unknown'}, {'max_retries': 4}):
        changed = json.loads(json.dumps(definition))
        changed['nodes'][1].update(mutation)
        with pytest.raises(ValueError):
            studio.workflows.validate(changed)


@pytest.mark.parametrize('value', [True, None, '0.9', -.1, 1.1, float('nan'), float('inf')])
def test_router_confidence_never_accepts_nonfinite_or_coerced_values(value):
    with pytest.raises(ConfigError):
        Orchestrator.confidence(value)


def test_watch_reloads_settings_and_force_applies_only_once(repo, monkeypatch, capsys):
    from nexus.cli import main
    from nexus.orchestration import cli
    polls = []
    original = cli.RoleIndex.ensure
    def ensure(index, force=False):
        polls.append((force, index.options['dimensions']))
        return original(index, force=force)
    def poll(seconds):
        if len(polls) == 1:
            (repo / 'role-index.yaml').write_text('encoder: lexical-hash\ndimensions: 64\n')
        else:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli.RoleIndex, 'ensure', ensure)
    monkeypatch.setattr(cli.time, 'sleep', poll)
    assert main(['orchestrate', '-C', str(repo), '--encoder', 'lexical-hash', 'index', '--force', '--watch']) == 130
    assert polls == [(True, 384), (False, 64)]
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{')]
    assert len(outputs) == 2 and outputs[0]['signature'] != outputs[1]['signature']
