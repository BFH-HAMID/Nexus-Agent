"""Studio regressions use temporary workspaces and no external model services."""
import time
from types import SimpleNamespace

import pytest

from nexus.agent import NexusAgent
from nexus.config import load_config
from nexus.server import ServerState
from nexus.studio import digest, redact
from nexus.types import Message, Risk, ToolSpec
from nexus.workbench import proposal


@pytest.fixture()
def studio(repo):
    return ServerState(repo, quiet=True).workbench.studio


def session(studio):
    return studio.state.create('Review task', None, False, options=studio.wb.prepare({}))


def test_workspace_persistence_branch_search(studio):
    c = studio.post('conversation', {'title':'Original'})['data']
    c['messages'] = [{'role':'user', 'text':'original'}, {'role':'assistant', 'text':'reply'}]
    studio.put('conversation', c, c['id'])
    branch = studio.post('conversation', {'branch_from':c['id'], 'index':0, 'text':'edited'})['data']
    assert branch['id'] != c['id']
    assert studio.find('conversation', c['id'])['messages'][0]['text'] == 'original'
    assert studio.get('items', {'kind':['conversation'], 'q':['edited']})['data'][0]['id'] == branch['id']
    other = studio.root / 'different'
    other.mkdir()
    assert ServerState(other, quiet=True).workbench.studio.items('conversation') == []


@pytest.mark.parametrize('payload', [ {'max_tools':0}, {'timeout_s':True}, {'role_permissions':[]},
    {'secret_scan':'yes'}, {'fallbacks':{'no-agent':'missing'}}, {'escalate_tools':'write_file'}])
def test_policy_validation(studio, payload):
    with pytest.raises(ValueError):
        studio.save_settings(payload)


def test_context_guard_redacts_blocks_and_captures(studio):
    s = session(studio)
    guard = studio.context_guard(s)
    guard('test', 'system', [Message('user', 'hello')], [])
    assert studio.find('context', s.task_id)['payload']['messages'][0]['role'] == 'user'
    secret = 'ghp_' + 'a' * 32
    with pytest.raises(ValueError, match='secret'):
        guard('test', 'system', [Message('user', secret)], [])
    record = studio.find('context', s.task_id)
    assert record['blocked'] and secret not in str(record)
    assert redact({'nested':[secret]}) == {'nested':['[REDACTED_SECRET]']}
    s.cancel.set()
    with pytest.raises(ValueError, match='stopped'):
        guard('test', '', [], [])


def test_checkpoint_rollback_refuses_manual_changes(studio):
    s = session(studio)
    path = studio.root / 'rollback.txt'
    path.write_text('before\n')
    studio.checkpoint(s, {'type':'tool_call', 'tool':'write_file', 'args':{'path':'rollback.txt', 'content':'after\n'}})
    path.write_text('after\n')
    studio.checkpoint(s, {'type':'tool_result', 'tool':'write_file', 'is_error':False})
    record = studio.items('checkpoint')[0]
    assert 'before' in studio.rollback({'id':record['id']})['data']['diff']
    path.write_text('manual\n')
    with pytest.raises(ValueError, match='changed'):
        studio.rollback({'id':record['id'], 'confirm':'RESTORE ' + record['id']}, True)
    assert path.read_text() == 'manual\n'
    path.write_text('after\n')
    studio.rollback({'id':record['id'], 'confirm':'RESTORE ' + record['id']}, True)
    assert path.read_text() == 'before\n'


def test_selective_hunks_changes_proposal_not_file(studio):
    s = session(studio)
    path = studio.root / 'hunks.txt'
    path.write_text('a\nb\nc\n')
    args = {'path':'hunks.txt', 'content':'A\nb\nC\n'}
    s.pending = {'approval_id':'original', 'tool':'write_file', 'args':args,
                 'proposal':proposal(studio.root, 'write_file', args)}
    changes = studio.hunks({'task_id':s.task_id, 'approval_id':'original'})['data']['changes']
    studio.hunks({'task_id':s.task_id, 'approval_id':'original', 'selected':[changes[0]['id']]})
    assert args['content'] == 'A\nb\nc\n'
    assert path.read_text() == 'a\nb\nc\n'
    assert s.pending['approval_id'] != 'original'
    with pytest.raises(ValueError, match='Stale'):
        studio.hunks({'task_id':s.task_id, 'approval_id':'original'})


def test_memory_refusal_keeps_draft_pending(studio, monkeypatch):
    draft = studio.post('memory-draft', {'text':'A useful preference'})['data']
    monkeypatch.setattr('nexus.memory.MemoryStore.add', lambda *a, **k: (False, 'store full'))
    with pytest.raises(ValueError, match='full'):
        studio.post('memory-review', {'id':draft['id'], 'action':'approve'})
    assert studio.find('memory-draft', draft['id'])['status'] == 'draft'
    studio.post('memory-review', {'id':draft['id'], 'action':'reject'})
    assert studio.find('memory-draft', draft['id'])['status'] == 'reject'


def test_auto_learning_stages_without_active_writes(studio):
    agent = NexusAgent(load_config(studio.root), workspace=studio.root)
    studio.attach(agent, session(studio))
    agent.learning.review_sink({'memory':['Remember this'], 'skill':{'name':'draft-only', 'body':'inactive'}})
    assert studio.items('memory-draft')[0]['status'] == 'draft'
    assert studio.items('skill-draft')[0]['status'] == 'draft'
    assert agent.providers.request_guard


def test_permissions_budget_and_plan_do_not_bypass(studio):
    s = session(studio)
    studio.save_settings({'max_tools':1, 'role_permissions':{'backend':{'shell':False}}})
    agent = SimpleNamespace(active_role=SimpleNamespace(name='backend'))
    policy = studio.policy(agent, s, lambda *a: '')
    shell = ToolSpec('run_command', '', {}, risk=Risk.WRITE)
    assert 'Shell refused' in policy(shell, {'command':'echo test'})
    write = ToolSpec('write_file', '', {}, risk=Risk.WRITE)
    assert not policy(write, {})
    assert 'budget' in policy(write, {})
    s.read_only = True
    policy = studio.policy(agent, s, lambda *a: '')
    assert 'Plan mode' in policy(write, {})


def test_restore_validates_all_before_writing(studio):
    bad = {'schema':1, 'studio':{'template':[{'text':'valid'}], 'profile':[{'temperature':100}]}}
    with pytest.raises(ValueError):
        studio.post('restore', {'backup':bad, 'confirm':'IMPORT REVIEWED BACKUP'})
    assert studio.items('template') == []
    good = {'schema':1, 'studio':{'template':[{'text':'safe template'}],
        'memory-draft':[{'text':'inactive', 'status':'approve', 'expires':0}]}}
    studio.post('restore', {'backup':good, 'confirm':'IMPORT REVIEWED BACKUP'})
    draft = studio.items('memory-draft')[0]
    assert draft['status'] == 'draft' and draft['expires'] > time.time()
    assert studio.items('template')[0]['text'] == 'safe template'


def test_api_dispatch_and_truthful_security(studio):
    assert studio.wb.get('studio/settings', {})['data']['secret_scan']
    assert studio.wb.post('studio/template', {'text':'task'})['data']['text'] == 'task'
    security = studio.get('availability', {})['data']
    assert security['worktree_is_os_sandbox'] is False
    assert security['encrypted_live_state_enabled'] is False
    assert 'activation required' in security['encrypted_live_state']
    assert digest(None) != digest('')


def test_human_checklist_is_not_automatic_test_proof(studio):
    record = studio.put('evidence', {'task_id':'evidence-run', 'criteria':['tests pass'], 'criteria_state':'unverified'})
    result = studio.post('criteria-review', {'id':record['id'], 'checked':[0]})['data']
    assert result['human_checked'] == [0]
    assert 'not automatic' in result['criteria_state']
    with pytest.raises(ValueError):
        studio.post('criteria-review', {'id':record['id'], 'checked':[2]})


def test_provider_guard_blocks_real_registry_call_and_circuit_cooldown(studio):
    from nexus.providers import ProviderRegistry
    from nexus.types import ProviderError

    from .conftest import FakeProvider, text_completion

    agent = NexusAgent(load_config(studio.root), workspace=studio.root)
    name = next(iter(agent.config.agents))
    fake = FakeProvider(name, [text_completion('response')])
    agent.providers = ProviderRegistry(agent.config.agents, factories={name:fake})
    s = session(studio)
    studio.attach(agent, s)
    with pytest.raises(ValueError, match='secret'):
        agent.providers.call_with_fallback(name, '', [Message('user', 'ghp_' + 'z' * 32)], [])
    assert not fake.calls
    # Circuits open after failures, but a rejected open circuit does not extend cooldown.
    for _ in range(3):
        agent.providers.error_observer(name, ProviderError('unreachable'))
    circuit = studio.items('circuit')[0]
    with pytest.raises(ProviderError, match='Circuit open'):
        agent.providers.request_guard(name, '', [], [])
    agent.providers.error_observer(name, ProviderError('Circuit open'))
    assert studio.items('circuit')[0]['updated_at'] == circuit['updated_at']
