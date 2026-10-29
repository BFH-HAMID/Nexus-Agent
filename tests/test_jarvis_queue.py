"""Module 4 admission: real existing SQLite store; never invokes a model/tool runner."""
import pytest

from nexus.jarvis.queue import Queue
from nexus.server import ServerState


@pytest.fixture
def queue(repo):
    return Queue(ServerState(repo, quiet=True).workbench.studio)


def test_default_l1_queue_persists_without_execution(queue):
    job = queue.enqueue('Review the unit tests', priority=3)
    restored = Queue(ServerState(queue.root, quiet=True).workbench.studio)
    status = restored.status()
    assert status['policy']['level'] == 'L1' and status['execution_enabled'] is False
    assert status['jobs'][0]['id'] == job['id'] and status['jobs'][0]['status'] == 'awaiting_approval'
    assert restored.studio.wb.path.name == 'dashboard.db'  # Existing store, not new KB.


def test_priority_and_duplicate_loop_guard(queue):
    queue.enqueue('Lower priority task', priority=8)
    high = queue.enqueue('Urgent task', priority=0)
    assert queue.status()['jobs'][0]['id'] == high['id']
    with pytest.raises(ValueError, match='Duplicate'):
        queue.enqueue('  URGENT   TASK  ')


def test_l0_and_bounded_admission_cost(queue):
    (queue.root / 'autonomy.yaml').write_text('level: L0')
    with pytest.raises(ValueError, match='L0'):
        queue.enqueue('No automatic admission')
    (queue.root / 'autonomy.yaml').write_text('level: L1\nmax_queue: 1\nmax_reserved_cost: 0.1')
    with pytest.raises(ValueError, match='reservation'):
        queue.enqueue('Too expensive', estimated_cost=0.2)
    queue.enqueue('Fits estimated budget', estimated_cost=0.1)
    with pytest.raises(ValueError, match='limit'):
        queue.enqueue('Queue full')


def test_message_depth_and_unknown_parent_blocked(queue):
    (queue.root / 'autonomy.yaml').write_text('max_depth: 1')
    root = queue.enqueue('Root')
    child = queue.enqueue('Child', parent=root['id'])
    with pytest.raises(ValueError, match='depth'):
        queue.enqueue('Grandchild', parent=child['id'])
    with pytest.raises(ValueError, match='parent'):
        queue.enqueue('Unknown', parent='not-a-real-id')


def test_approval_is_exact_and_does_not_approve_tools_or_execute(queue):
    job = queue.enqueue('Please review changes')
    with pytest.raises(ValueError, match='Exact'):
        queue.review(job['id'], 'approve', 'yes')
    job = queue.review(job['id'], 'approve', f'APPROVE QUEUED TASK {job["id"]}')
    assert job['status'] == 'approved' and queue.status()['execution_enabled'] is False
    assert queue.review(job['id'], 'cancel')['status'] == 'cancelled'
    with pytest.raises(ValueError):
        queue.review(job['id'], 'approve', f'APPROVE QUEUED TASK {job["id"]}')


def test_secrets_and_emergency_stop_block_admission(queue, monkeypatch):
    from nexus.computer_control import EmergencyStop
    monkeypatch.setenv('GITHUB_TOKEN', 'opaque-private-queue-key')
    with pytest.raises(ValueError):
        queue.enqueue('Use opaque-private-queue-key')
    EmergencyStop().set(True)
    with pytest.raises(ValueError, match='stop'):
        queue.enqueue('Stopped task')
    assert queue.status()['jobs'] == []
    EmergencyStop().set(False)


@pytest.mark.parametrize('text', ['[]', 'level: L9', 'max_queue: true', 'max_depth: 900', 'max_reserved_cost: .nan', 'unexpected: true'])
def test_invalid_autonomy_config_fails_closed(queue, text):
    (queue.root / 'autonomy.yaml').write_text(text)
    with pytest.raises(ValueError):
        queue.enqueue('Must not be admitted')
    assert queue.studio.items('jarvis-job') == []


def test_expired_tasks_cannot_be_approved(queue, monkeypatch):
    import nexus.jarvis.queue as module
    now = module.time.time()
    monkeypatch.setattr(module.time, 'time', lambda: now)
    (queue.root / 'autonomy.yaml').write_text('ttl_seconds: 60')
    job = queue.enqueue('Bounded TTL task')
    monkeypatch.setattr(module.time, 'time', lambda: now + 61)
    with pytest.raises(ValueError):
        queue.review(job['id'], 'approve', f'APPROVE QUEUED TASK {job["id"]}')
    assert queue.status()['jobs'][0]['status'] == 'expired'


def test_cli_queue_uses_actual_store_and_fresh_approval(repo, monkeypatch, capsys):
    import json

    from nexus.cli import main
    prefix = ['jarvis', '-C', str(repo)]
    assert main([*prefix, 'enqueue', 'CLI admission']) == 0
    job = json.loads(capsys.readouterr().out)
    monkeypatch.setattr('builtins.input', lambda *a: f'APPROVE QUEUED TASK {job["id"]}')
    assert main([*prefix, 'approve', job['id']]) == 0
    capsys.readouterr()
    assert main([*prefix, 'status']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['jobs'][0]['status'] == 'approved' and status['execution_enabled'] is False
