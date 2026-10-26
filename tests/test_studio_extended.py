"""New workflow/runtime/security tests. External providers/Docker are scripted only in tests."""
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from nexus.server import ServerState
from nexus.studio_execution import fetch_allowed, validate_domains
from nexus.types import Message, Risk, ToolSpec


@pytest.fixture()
def studio(repo):
    return ServerState(repo, quiet=True).workbench.studio


def definition(nodes=None):
    return {'name': 'Reviewed workflow', 'nodes': nodes or [node('research')]}


def node(ident, kind='research', depends=None, condition='success'):
    return {'id': ident, 'kind': kind, 'role': 'general', 'task': 'Inspect code',
            'depends_on': depends or [], 'condition': condition}


@pytest.mark.parametrize('nodes', [[node('a', depends=['missing'])], [node('a', depends=['a'])],
    [node('a', depends=['b']), node('b', depends=['a'])], [node('a'), node('a')],
    [node('a', kind='invalid')], [node('a', condition='shell')]])
def test_workflow_validation_fails_without_execution(studio, nodes):
    with pytest.raises(ValueError):
        studio.post('workflow-dry-run', {'definition': definition(nodes)})
    assert studio.items('workflow') == []
    assert studio.items('workflow-run') == []


def test_workflow_versions_immutable_and_dryrun(studio):
    first = studio.post('workflow-save', {'definition': definition()})['data']
    changed = definition([node('writer', 'agent')])
    second = studio.post('workflow-save', {'definition': changed, 'family': first['family']})['data']
    assert second['version'] == 2 and second['id'] != first['id']
    assert studio.find('workflow', first['id'])['definition']['nodes'][0]['id'] == 'research'
    dry = studio.post('workflow-dry-run', {'definition': changed})['data']
    assert dry['writes'] == ['writer']


def test_parallel_readonly_execution_conditions_and_handoffs(studio, monkeypatch):
    barrier = threading.Barrier(2)
    called = []
    def scripted(run_id, n, handoff):
        called.append(n['id'])
        if n['kind'] == 'research':
            assert n['read_only'] is True
            barrier.wait(timeout=3)
        return {'ok': n['id'] != 'r2', 'status': 'done', 'handoff_keys': list(handoff)}
    monkeypatch.setattr(studio.workflows, 'node', scripted)
    nodes = [node('r1'), node('r2'), node('recovery', 'agent', ['r1', 'r2'], 'failure'),
             node('skip', 'agent', ['r2']), node('review', 'reviewer', ['recovery'], 'always')]
    normalized, _ = studio.workflows.validate(definition(nodes))
    record = studio.put('workflow-run', {'definition': normalized, 'nodes': {}, 'status': 'running'})
    result = studio.workflows.execute(record)
    assert set(called) == {'r1', 'r2', 'recovery', 'review'}
    assert result['nodes']['skip']['status'] == 'skipped'
    assert result['nodes']['review']['handoff_keys'] == ['recovery']
    assert result['status'] == 'failed'  # A real failed research node isn't hidden by recovery.


def test_workflow_start_requires_confirmation_and_idle(studio):
    saved = studio.workflows.save({'definition': definition()})['data']
    with pytest.raises(ValueError):
        studio.workflows.run({'id': saved['id']})
    studio.state.active_task = 'busy'
    with pytest.raises(ValueError, match='Stop'):
        studio.workflows.run({'id': saved['id'], 'confirm': 'RUN REVIEWED WORKFLOW'})


def test_clarification_wait_answer_and_stale_rejection(studio):
    result = []
    thread = threading.Thread(target=lambda: result.append(studio.ask('test-run', 'Which API?', 5)))
    thread.start()
    deadline = time.monotonic() + 3
    while not studio.items('clarification') and time.monotonic() < deadline:
        time.sleep(.01)
    question = studio.items('clarification')[0]
    # Wait until the waiter has been registered (the store is intentionally durable first).
    while question['id'] not in studio.questions and time.monotonic() < deadline:
        time.sleep(.01)
    studio.post('clarification-answer', {'id': question['id'], 'answer': 'Use the local API'})
    thread.join(timeout=3)
    assert result == ['Use the local API']
    with pytest.raises(ValueError, match='expired|waiter'):
        studio.post('clarification-answer', {'id': question['id'], 'answer': 'again'})


@pytest.mark.parametrize('domains', [['*.github.com'], ['http://github.com'], ['localhost'], ['127.0.0.1'], ['GitHub.com']])
def test_exact_domain_validation(domains):
    with pytest.raises(ValueError):
        validate_domains(domains)


@pytest.mark.parametrize('url', ['http://github.com', 'https://evil.example', 'https://github.com:444', 'https://u:p@github.com'])
def test_egress_rejects_nonallowlisted_urls_before_dns(url, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('DNS must not be reached'))
    with pytest.raises(ValueError):
        fetch_allowed(url, ['github.com'])


def test_egress_rejects_private_or_mixed_dns(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(None, None, None, None, ('127.0.0.1', 443)), (None, None, None, None, ('8.8.8.8', 443))])
    with pytest.raises(ValueError, match='DNS'):
        fetch_allowed('https://github.com', ['github.com'])


def test_egress_pins_resolved_address_and_does_not_follow_redirect(monkeypatch):
    seen = []
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(None, None, None, None, ('8.8.8.8', 443))])
    class Connection:
        def __init__(self, host, address):
            seen.append((host, address))
        def request(self, *args, **kwargs):
            seen.append(args)
        def getresponse(self):
            chunks = iter([b'untrusted', b''])
            return SimpleNamespace(status=302, read1=lambda size: next(chunks), getheader=lambda key: 'https://127.0.0.1/secret')
        def close(self):
            pass
    monkeypatch.setattr('nexus.studio_execution.PinnedHTTPS', Connection)
    result = fetch_allowed('https://github.com/path?q=1', ['github.com'])
    assert seen[0] == ('github.com', '8.8.8.8')
    assert result['redirect_followed'] is False and result['status'] == 302


def test_missing_container_runtime_cannot_enable_enforcement(studio, monkeypatch):
    monkeypatch.setattr('nexus.studio_execution.shutil.which', lambda name: None)
    with pytest.raises(ValueError, match='no host fallback'):
        studio.save_settings({'isolation': {'enabled': True, 'image': 'python:3.12-slim', 'domains': ['github.com']}})
    assert studio.settings()['isolation']['enabled'] is False


def test_docker_execution_uses_no_network_or_host_secrets(studio, monkeypatch, tmp_path):
    from nexus.config import nexus_home
    checkout = nexus_home() / 'worktrees' / 'test-checkout'
    checkout.mkdir(parents=True)
    record = studio.put('checkout', {'path': str(checkout)})
    calls = []
    monkeypatch.setattr('nexus.studio_execution.docker_available', lambda: '/usr/bin/docker')
    monkeypatch.setattr('nexus.studio_execution.image_ready', lambda *a: None)
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, 'actual fixture output', '')
    monkeypatch.setattr('nexus.studio_execution.subprocess.run', run)
    studio.execution.isolated({'checkout_id': record['id'], 'command': 'echo ok'})
    argv = calls[0]
    assert '--network=none' in argv and '--cap-drop=ALL' in argv and '--pull=never' in argv
    assert '--security-opt=no-new-privileges' in argv and '--read-only' in argv
    assert not any('docker.sock' in part or 'api_key' in part.lower() for part in argv)
    assert calls[-1][1:3] == ['rm', '-f']


def test_managed_process_real_start_logs_and_stop(studio):
    payload = {'argv': [sys.executable, '-u', '-c', 'import time; print("ready",flush=True); time.sleep(20)'],
               'port': 39123, 'confirm': 'START TRUSTED DEV APP'}
    records = studio.execution.start(payload)
    ident = records[0]['id']
    try:
        deadline = time.monotonic() + 3
        while 'ready' not in studio.execution.list()[0]['log'] and time.monotonic() < deadline:
            time.sleep(.02)
        assert 'ready' in studio.execution.list()[0]['log']
        assert studio.execution.list()[0]['running']
    finally:
        result = studio.execution.stop(ident)
    assert not result[0]['running']


def enable_encryption_with_capability_check(studio):
    """Exercise the fail-closed contract on Python/SQLite without serialize."""
    from nexus import studio_crypto

    before = studio.wb.path.read_bytes()
    if not hasattr(studio_crypto.sqlite3.Connection, 'serialize'):
        with pytest.raises(ValueError, match='lacks serialize'):
            studio.post('encryption-enable', {'confirm': 'ENCRYPT DASHBOARD STATE', 'single_server': True})
        assert studio.wb.path.read_bytes() == before
        assert not studio_crypto.enabled(studio.wb.path)
        assert not studio.encrypted()
        return None
    return studio.post('encryption-enable', {'confirm': 'ENCRYPT DASHBOARD STATE', 'single_server': True})


def test_actual_encrypted_database_roundtrip_and_tamper_failure(studio, monkeypatch):
    from nexus import studio_crypto
    monkeypatch.setattr(studio_crypto, 'os_key', lambda *a, **k: b'x' * 32)
    studio.post('template', {'text': 'private template text'})
    result = enable_encryption_with_capability_check(studio)
    if result is None:
        return  # Unsupported builds have asserted refusal and unchanged plaintext.
    assert result['data']['enabled']
    data = studio.wb.path.read_bytes()
    assert data.startswith(studio_crypto.MAGIC) and b'private template' not in data
    assert studio.items('template')[0]['text'] == 'private template text'
    studio.post('template', {'text': 'another encrypted record'})
    assert len(studio.items('template')) == 2
    tampered = bytearray(studio.wb.path.read_bytes())
    tampered[-1] ^= 1
    studio.wb.path.write_bytes(tampered)
    with pytest.raises(ValueError, match='authentication'):
        studio.items('template')


def test_encryption_missing_os_key_fails_closed(studio, monkeypatch):
    from nexus import studio_crypto
    studio.items('template')
    before = studio.wb.path.read_bytes()
    def unavailable(*a, **k):
        raise ValueError('OS key store locked')
    monkeypatch.setattr(studio_crypto, 'os_key', unavailable)
    with pytest.raises(ValueError, match='locked'):
        studio.post('encryption-enable', {'confirm': 'ENCRYPT DASHBOARD STATE', 'single_server': True})
    assert studio.wb.path.read_bytes() == before


def test_token_budget_blocks_before_model_request(studio):
    s = studio.state.create('bounded task', None, False, options=studio.wb.prepare({}))
    s.options['step_budgets'] = {'tokens': 10}
    with pytest.raises(ValueError, match='token budget'):
        studio.context_guard(s)('test', '', [Message('user', 'hello')], [])


def test_strict_research_policy_denies_shell_git_and_learning(studio):
    s = studio.state.create('research', None, True, options=studio.wb.prepare({}))
    s.options['strict_readonly'] = True
    agent = SimpleNamespace(active_role=SimpleNamespace(name='general'))
    check = studio.policy(agent, s, lambda *a: '')
    assert 'non-executing' in check(ToolSpec('git', '', {}, risk=Risk.READ, read_only=True), {'args': ['status']})
    assert not check(ToolSpec('read_file', '', {}, risk=Risk.READ, read_only=True), {'path': 'README.md'})


def test_schema_migration_and_roundtrip_workflows(studio):
    studio.workflows.save({'definition': definition()})
    backup = studio.post('backup', {})['data']
    assert backup['schema'] == 2 and len(backup['studio']['workflow']) == 1
    studio.post('restore', {'backup': backup, 'confirm': 'IMPORT REVIEWED BACKUP'})
    assert len(studio.items('workflow')) == 2
    legacy = {'schema': 0, 'templates': [{'text': 'legacy task'}]}
    studio.post('restore', {'backup': legacy, 'confirm': 'IMPORT REVIEWED BACKUP'})
    assert studio.items('template')[0]['text'] == 'legacy task'


def test_expiry_and_revocation_never_remove_manually_modified_memory(studio, monkeypatch):
    from nexus.memory import MemoryStore
    store = MemoryStore()
    store.add('original owned entry')
    draft = studio.put('memory-draft', {'status': 'approve', 'approved_text': 'original owned entry', 'active_expires': time.time() - 1})
    studio.expire_memory()
    assert 'original owned entry' not in MemoryStore().entries()
    assert studio.find('memory-draft', draft['id'])['status'] == 'expired-active'
    store = MemoryStore()
    store.add('manually modified value')
    owned = studio.put('memory-draft', {'status': 'approve', 'approved_text': 'old value'})
    studio.post('memory-revoke', {'id': owned['id'], 'confirm': 'REVOKE ' + owned['id']})
    assert 'manually modified value' in MemoryStore().entries()


def test_observed_token_accounting_and_output_cap_with_real_registry(studio):
    from nexus.agent import NexusAgent
    from nexus.config import load_config
    from nexus.providers import ProviderRegistry

    from .conftest import FakeProvider, text_completion
    agent = NexusAgent(load_config(studio.root), workspace=studio.root)
    name = next(iter(agent.config.agents))
    completion = text_completion('real scripted completion')
    completion.input_tokens = 100
    completion.output_tokens = 20
    fake = FakeProvider(name, [completion])
    agent.providers = ProviderRegistry(agent.config.agents, factories={name: fake})
    s = studio.state.create('bounded', None, False, options=studio.wb.prepare({}))
    s.options['step_budgets'] = {'tokens': 2000}
    studio.attach(agent, s)
    _, result = agent.providers.call_with_fallback(name, '', [Message('user', 'hello')], [])
    assert result.text == 'real scripted completion'
    assert 0 < fake.calls[0]['max_tokens'] < 1000
    assert studio.items('circuit')[0]['failures'] == 0


def test_actual_provider_body_captured_after_tool_conversion(studio):
    from nexus.agent import NexusAgent
    from nexus.config import AgentConfig, load_config
    from nexus.providers import OpenAICompatProvider, ProviderRegistry

    from .test_providers import FakeHttp, FakeResponse
    cfg = AgentConfig(name='wire-test', provider='openai-compatible', model='local', base_url='http://127.0.0.1:11434/v1', tool_mode='text')
    transport = FakeHttp([FakeResponse({'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]})])
    provider = OpenAICompatProvider(cfg, transport=transport)
    agent = NexusAgent(load_config(studio.root), workspace=studio.root)
    agent.providers = ProviderRegistry({cfg.name: cfg}, factories={cfg.name: provider})
    s = studio.state.create('wire', None, False, options=studio.wb.prepare({}))
    studio.attach(agent, s)
    agent.providers.call_with_fallback(cfg.name, 'base instructions', [Message('user', 'hi')], [ToolSpec('echo', 'Echo text', {})])
    record = studio.find('context', s.task_id)
    assert record['wire_payload'] == transport.requests[0]['json']
    assert 'echo' in record['wire_payload']['messages'][0]['content']
    assert 'actual provider JSON' in record['source']


def test_plaintext_cli_recording_not_used_for_encrypted_dashboard(studio, monkeypatch):
    from nexus import studio_crypto
    from nexus.agent import NexusAgent, RunResult
    from nexus.config import load_config
    monkeypatch.setattr(studio_crypto, 'os_key', lambda *a, **k: b'y' * 32)
    studio.items('template')
    if enable_encryption_with_capability_check(studio) is None:
        return  # No encrypted session can exist on an unsupported SQLite build.
    agent = NexusAgent(load_config(studio.root), workspace=studio.root)
    s = studio.state.create('secure', None, False, options=studio.wb.prepare({}))
    studio.attach(agent, s)
    result = RunResult(text='private result', summary='private summary', role='general')
    agent.sessions.record(result, 'private task')
    assert agent.sessions.recent()[0]['text'] == 'private result'
    assert b'private result' not in studio.wb.path.read_bytes()
    draft = studio.post('memory-draft', {'text': 'secure durable note'})['data']
    studio.post('memory-review', {'id': draft['id'], 'action': 'approve'})
    assert 'secure durable note' in agent.memory_store.prompt_sections()
    from nexus.memory import MemoryStore
    assert 'secure durable note' not in MemoryStore().entries()


def test_actual_junit_cases_and_reviewed_skill_lifecycle(studio):
    from nexus import skills
    (studio.root / 'test_skill_coverage.py').write_text('def test_actual_example():\n    assert 2 + 2 == 4\n')
    draft = studio.post('skill-draft', {'proposal': {'name': 'reviewed-addition', 'description': 'Apply reviewed addition.',
        'body': skills.default_body('reviewed-addition', 'Apply reviewed addition.')}})['data']
    studio.post('skill-preview', {'id': draft['id']})
    with pytest.raises(ValueError, match='runtime check'):
        studio.post('skill-promote', {'id': draft['id'], 'confirm': 'PROMOTE REVIEWED SKILL'})
    runtime = studio.post('skill-runtime-test', {'id': draft['id'], 'confirm': 'RUN DRAFT SKILL TEST'})['data']
    assert runtime['exit_code'] == 0
    verification = studio.find('verification', runtime['verification_id'])
    assert verification['cases'][0]['status'] == 'passed'
    promoted = studio.post('skill-promote', {'id': draft['id'], 'confirm': 'PROMOTE REVIEWED SKILL'})['data']
    assert promoted['status'] == 'promoted'
    existing = skills.get('reviewed-addition', studio.root)
    delete = studio.post('skill-draft', {'proposal': {'action': 'delete', 'name': existing.name}})['data']
    studio.post('skill-preview', {'id': delete['id']})
    existing.path.write_text(existing.path.read_text() + '\nmanual edit\n')
    with pytest.raises(ValueError, match='stale'):
        studio.post('skill-promote', {'id': delete['id'], 'confirm': 'PROMOTE REVIEWED SKILL'})
    studio.post('skill-preview', {'id': delete['id']})
    studio.post('skill-promote', {'id': delete['id'], 'confirm': 'PROMOTE REVIEWED SKILL'})
    assert not existing.path.exists()


def test_final_body_secret_refusal_is_not_retried_or_fallen_back(studio):
    from nexus.agent import NexusAgent
    from nexus.config import AgentConfig, load_config
    from nexus.providers import OpenAICompatProvider, ProviderRegistry, RequestBlocked

    from .conftest import FakeProvider
    from .test_providers import FakeHttp
    cfg = AgentConfig(name='wire-secret', provider='openai-compatible', model='local', base_url='http://127.0.0.1:11434/v1',
                      fallback='fallback', extra={'password': 'private-secret-in-extra'})
    transport = FakeHttp([])
    first = OpenAICompatProvider(cfg, transport=transport)
    fallback = FakeProvider('fallback')
    agent = NexusAgent(load_config(studio.root), workspace=studio.root)
    agent.providers = ProviderRegistry({cfg.name: cfg, 'fallback': AgentConfig(name='fallback', model='scripted')},
                                      factories={cfg.name: first, 'fallback': fallback})
    s = studio.state.create('guard', None, False, options=studio.wb.prepare({}))
    studio.attach(agent, s)
    with pytest.raises(RequestBlocked, match='secret'):
        agent.providers.call_with_fallback(cfg.name, 'safe', [Message('user', 'safe task')], [])
    assert transport.requests == [] and fallback.calls == []
    assert studio.find('context', s.task_id)['blocked']


def test_escalation_rules_force_human_approval_even_auto_yes(studio, monkeypatch):
    from nexus.tools import ApprovalDecision, ApprovalGate
    studio.save_settings({'escalation_rules': [{'tool':'write_file', 'role':'general', 'path_prefix':'app'}]})
    s = studio.state.create('write', None, False, options=studio.wb.prepare({}))
    seen = []
    def callback(*args):
        seen.append(args)
        return ApprovalDecision(False, reason='human rejected escalation')
    monkeypatch.setattr(studio.state, '_approval', lambda session: callback)
    policy = studio.policy(SimpleNamespace(active_role=SimpleNamespace(name='general')), s, lambda *a: '')
    gate = ApprovalGate(mode='auto')
    gate.policy = policy
    decision = gate.check(ToolSpec('write_file', '', {}, risk=Risk.WRITE), {'path':'app/main.py', 'content':'unsafe'})
    assert not decision.approved and len(seen) == 1


def test_enforced_verification_has_no_host_fallback(studio, monkeypatch):
    studio.put('settings', {**studio.settings(), 'isolation': {'enabled':True, 'image':'test', 'domains':[]}})
    monkeypatch.setattr('nexus.studio.subprocess.run', lambda *a, **k: pytest.fail('Host execution must not occur'))
    with pytest.raises(ValueError, match='no host fallback'):
        studio.run_check('tests')


def test_backup_restores_history_as_archived_and_only_safe_ui_preferences(studio):
    backup = {'schema':2, 'studio':{}, 'preferences':{'language':'bn','layout':{'focus':True},
              'local_only':False,'write_paths':[]}, 'runs':[{'task':'Old task', 'status':'running', 'result':{'text':'Old result'}, 'options':{'auto_yes':True}}]}
    studio.wb.save_settings({'local_only':True,'write_paths':['app']})
    studio.post('restore', {'backup':backup,'confirm':'IMPORT REVIEWED BACKUP'})
    history = studio.wb.history()
    assert history[0]['status'] == 'archived' and history[0]['imported'] is True
    assert history[0]['options'] == {}
    assert studio.state.get(history[0]['task_id']) is None
    assert studio.wb.settings()['language'] == 'bn'
    assert studio.wb.settings()['local_only'] is True
    assert studio.wb.settings()['write_paths'] == ['app']


def test_missing_sqlite_serialization_refuses_without_changing_database(studio, monkeypatch):
    from nexus import studio_crypto

    studio.post('template', {'text': 'must remain unchanged on refusal'})
    monkeypatch.setattr(studio_crypto, 'os_key', lambda *a, **k: b'z' * 32)
    monkeypatch.setattr(studio_crypto.sqlite3, 'Connection', type('WithoutSerialization', (), {}))
    assert enable_encryption_with_capability_check(studio) is None
