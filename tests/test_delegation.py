"""Real persisted delegation/gate/checkpoint contracts; no hosted inference."""
import json
import threading
import time
from types import SimpleNamespace

import pytest
import yaml

from nexus.delegation.core import Delegations, specification
from nexus.jarvis.runtime import Runtime, execute
from nexus.server import ServerState
from nexus.types import Event, EventType, Role


@pytest.fixture
def service(repo):
    (repo / 'autonomy.yaml').write_text(yaml.safe_dump({'runtime': {'enabled': True, 'local_model': 'fixture-local'}}))
    return Delegations(ServerState(repo, quiet=True).workbench.studio)


def approved(service, seconds=10):
    row = service.create('Inspect the local README without changing files', max_seconds=seconds)
    service.queue.review(row['id'], 'approve', 'APPROVE QUEUED TASK ' + row['id'])
    return Runtime(service.studio).claim()


@pytest.mark.parametrize('value', [False, [], {}, {'schema': True, 'max_seconds': 10}, {'schema': 1, 'max_seconds': True},
                                 {'schema': 1, 'max_seconds': 0}, {'schema': 1, 'max_seconds': 601},
                                 {'schema': 1, 'max_seconds': 10, 'cost': 0}])
def test_invalid_delegation_specs_fail_closed(value):
    with pytest.raises(ValueError):
        specification(value)


def test_delegation_is_an_existing_queue_job_never_an_implicit_worker(service):
    job = service.create('Review local documentation')
    assert job['status'] == 'awaiting_approval'
    assert Runtime(service.studio).claim() is None
    recovered = Delegations(service.studio).view(job['id'])
    assert recovered['state'] == 'waiting_approval' and recovered['actual_cost_usd'] is None
    assert not recovered['cost_limit_enforced']
    assert service.studio.items('jarvis-job')[0]['id'] == job['id']
    assert service.studio.items('delegation') == []


def test_per_task_deadline_is_approved_and_reserved_before_launch(service):
    row = approved(service, 10)
    runtime = Runtime(service.studio)
    assert row['runtime']['max_seconds'] == 10 and runtime.budget()['seconds_reserved'] == 10
    assert service.queue.policy()['runtime']['max_seconds'] == 120
    assert service.view(row['id'])['checkpoints'][0]['event'] == 'claimed'


def test_time_spec_tamper_invalidates_exact_job_approval(service):
    job = service.create('Review bounded task', max_seconds=10)
    service.queue.review(job['id'], 'approve', 'APPROVE QUEUED TASK ' + job['id'])
    job = service.studio.find('jarvis-job', job['id'])
    job['delegation_spec']['max_seconds'] = 120
    service.studio.put('jarvis-job', job, job['id'])
    with pytest.raises(ValueError, match='approval'):
        Runtime(service.studio).claim()


def test_worker_uses_real_tool_events_as_metadata_only_checkpoints(service, monkeypatch):
    from nexus.agent import NexusAgent, RunResult
    private = 'private file text never copied to checkpoint'
    (service.queue.root / 'README.md').write_text(private)
    def run(agent, task, **kwargs):
        _, tools, _ = agent.tools_for(Role(name='general'))
        text, failed = tools.call('read_file', {'path': 'README.md'})
        assert not failed and private in text
        return RunResult(text='Inspected README; no tests executed')
    monkeypatch.setattr(NexusAgent, 'run', run)
    job = approved(service)
    result = execute(service.studio, job['id'], job['lease'])
    assert result['status'] == 'done'
    view = service.view(job['id'])
    checkpoints = view['checkpoints']
    assert [c['event'] for c in checkpoints] == ['claimed', 'tool_call', 'tool_result', 'finished']
    assert checkpoints[1]['call_id'] == checkpoints[2]['call_id']
    assert len(checkpoints[1]['arguments_sha256']) == 64 and not checkpoints[2]['reported_error']
    assert private not in json.dumps(checkpoints) and 'README.md' not in json.dumps(checkpoints)
    assert view['result']['summary'] == 'Inspected README; no tests executed'
    assert 'not independent' in view['evidence']


def test_cancellation_revokes_checkpoint_authority_and_cannot_resume_running(service):
    job = approved(service)
    with pytest.raises(ValueError):
        service.resume(job['id'])
    assert service.cancel(job['id'])['state'] == 'cancelled'
    with pytest.raises(ValueError, match='lease'):
        service.checkpoint(job, Event(EventType.ROLE, {'role': 'general'}))
    assert len(service.view(job['id'])['checkpoints']) == 1


def test_interrupted_resume_never_inherits_approval_or_replays_checkpoint(service):
    job = approved(service)
    service.checkpoint(job, Event(EventType.TOOL_CALL, {'tool': 'write_file', 'args': {'path': 'README.md'}, 'call_id': 'a' * 32}))
    Runtime(service.studio).finish(job, 'interrupted', {'summary': 'Simulated process loss; effect uncertain'})
    resumed = service.resume(job['id'])
    assert resumed['state'] == 'waiting_approval' and Runtime(service.studio).claim() is None
    assert len(resumed['checkpoints']) == 3
    service.queue.review(job['id'], 'approve', 'APPROVE QUEUED TASK ' + job['id'])
    fresh = Runtime(service.studio).claim()
    assert fresh['lease'] != job['lease'] and fresh['attempts'] == 2
    assert fresh['delegation_checkpoints'][-1]['attempt'] == 2
    with pytest.raises(ValueError, match='lease'):
        service.checkpoint(job, Event(EventType.ROLE, {'role': 'general'}))


def test_80_percent_alert_is_durable_deduplicated_and_not_a_cost_claim(service):
    job = approved(service, 10)
    service.progress(job, 7.99)
    assert service.view(job['id'])['alerts'] == []
    service.progress(job, 8)
    Delegations(service.studio).progress(job, 9)
    assert len(service.view(job['id'])['alerts']) == 1
    assert service.studio.items('jarvis-notice')[0]['state'] == 'budget_warning'
    assert len(service.studio.items('jarvis-notice')) == 1
    assert service.view(job['id'])['actual_cost_usd'] is None


def test_smaller_deadline_does_not_look_like_a_policy_change_in_parent_supervisor(service, monkeypatch):
    import psutil
    runtime = Runtime(service.studio)
    job = approved(service, 10)
    process = SimpleNamespace(pid=9999999, poll=lambda: None)
    runtime.active = process, job
    runtime.active_started = time.monotonic()
    monkeypatch.setattr(psutil, 'Process', lambda pid: SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=1)))
    runtime.tick()
    assert runtime.active is not None and service.view(job['id'])['state'] == 'running'
    # This fixture owns no process: deliberately detach it, don't signal a fake PID.
    runtime.active = None


def test_waiting_action_approvals_are_real_not_invented(service):
    from nexus.jarvis.approvals import Approvals
    from nexus.types import Risk, ToolSpec
    job = approved(service)
    results = []
    approvals = Approvals(service.studio)
    thread = threading.Thread(target=lambda: results.append(approvals.callback(job)(ToolSpec('write_file', 'write', {}),
                             {'path': 'new.txt', 'content': 'review me'}, Risk.WRITE, 'reviewed file proposal')))
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            view = service.view(job['id'])
            if view['state'] == 'waiting_approval':
                break
            threading.Event().wait(.01)
        assert view['pending_actions'] and view['state'] == 'waiting_approval'
        approvals.decide(view['pending_actions'][0], False)
        thread.join(3)
        assert results and not results[0].approved
        assert service.view(job['id'])['state'] == 'running'
    finally:
        service.cancel(job['id'])
        thread.join(3)


def test_workspace_boundary_and_regular_jobs_cannot_be_claimed_as_delegations(service, tmp_path):
    regular = service.queue.enqueue('Regular queue job')
    with pytest.raises(ValueError):
        service.view(regular['id'])
    job = service.create('Delegated queue job')
    other = Delegations(ServerState(tmp_path / 'other', quiet=True).workbench.studio)
    with pytest.raises(ValueError):
        other.view(job['id'])


def test_cli_create_tasks_report_and_cancel_use_real_queue(repo, capsys):
    from nexus.cli import build_parser
    parser = build_parser()
    def invoke(*parts):
        args = parser.parse_args(['delegate', '-C', str(repo), *parts])
        assert args.func(args) == 0
        return json.loads(capsys.readouterr().out)
    created = invoke('create', 'Review README', '--max-seconds', '10')
    assert created['state'] == 'waiting_approval'
    assert invoke('tasks')[0]['id'] == created['id']
    assert invoke('report', created['id'])['actual_cost_usd'] is None
    assert invoke('cancel', created['id'])['state'] == 'cancelled'


def test_uncertain_calls_and_old_result_are_explicit_on_resume(service):
    job = approved(service)
    service.checkpoint(job, Event(EventType.TOOL_CALL, {'tool': 'write_file', 'args': {}, 'call_id': 'b' * 32}))
    Runtime(service.studio).finish(job, 'interrupted', {'summary': 'Outcome unknown'})
    view = service.resume(job['id'])
    assert len(view['uncertain_calls']) == 1
    assert view['uncertain_calls'][0]['call_id'] == 'b' * 32
    assert view['result_provenance'] == {'attempt': 1, 'current_attempt_terminal': False}


def test_optional_alert_failure_does_not_block_deadline_termination(service, monkeypatch):
    import psutil

    import nexus.live_process as processes
    runtime = Runtime(service.studio)
    job = approved(service, 5)
    killed = []
    process = SimpleNamespace(pid=9999999, poll=lambda: None)
    runtime.active = process, job
    runtime.active_started = time.monotonic() - 6
    monkeypatch.setattr(psutil, 'Process', lambda pid: SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=1)))
    monkeypatch.setattr(processes, 'terminate_group', lambda child: killed.append(child))
    monkeypatch.setattr(Delegations, 'progress', lambda *args: pytest.fail('alert must not run before termination'))
    runtime.tick()
    assert killed == [process] and runtime.active is None
    assert service.view(job['id'])['state'] == 'blocked'


def test_regular_job_approval_signature_stays_compatible_with_module4(service):
    import hashlib

    from nexus.jarvis.runtime import signature
    job = service.queue.enqueue('Existing queue compatibility')
    old = {k: job.get(k) for k in ('task', 'priority', 'parent', 'depth', 'expires', 'estimated_cost', 'source', 'mission', 'requires_online')}
    assert signature(job) == hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest()
    delegated = {**job, 'delegation_spec': {'schema': 1, 'max_seconds': 10}}
    assert signature(job) != signature(delegated)
