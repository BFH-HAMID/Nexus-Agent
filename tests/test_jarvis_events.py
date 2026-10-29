import pytest
import yaml

from nexus.jarvis.events import Events
from nexus.jarvis.inbox import Inbox
from nexus.jarvis.queue import Queue
from nexus.server import ServerState


@pytest.fixture
def events(repo):
    return Events(Queue(ServerState(repo, quiet=True).workbench.studio))


def test_event_sources_are_untrusted_approval_pending_and_depth_limited(events):
    first = events.publish('ci', 'CI reported failing checks')
    assert first['status'] == 'awaiting_approval' and first['source'] == 'event:ci'
    second = events.publish('build', 'Inspect build logs', parent=first['id'])
    assert second['depth'] == 1
    with pytest.raises(ValueError):
        events.publish('unknown', 'must be refused')
    with pytest.raises(ValueError):
        events.publish('ci', 'CI reported failing checks')


def test_actual_local_file_watcher_baselines_then_enqueues_no_contents(events):
    root = events.queue.root
    (root / 'tasks.md').write_text('first version')
    (root / 'autonomy.yaml').write_text(yaml.safe_dump({'triggers': [{'id': 'tasks', 'kind': 'file', 'path': 'tasks.md',
                                                                  'task': 'Review changed tasks.md, propose only'}]}))
    assert events.poll() == []
    (root / 'tasks.md').write_text('second version with untrusted instructions')
    jobs = events.poll()
    assert len(jobs) == 1 and events.poll() == []
    row = events.studio.find('jarvis-job', jobs[0])
    assert row['status'] == 'awaiting_approval' and 'untrusted instructions' not in row['task']
    assert 'second version' not in str(events.studio.items('jarvis-trigger'))


def test_schedule_poll_is_persistent_and_bounded(events, monkeypatch):
    import nexus.jarvis.events as module
    now = module.time.time()
    (events.queue.root / 'autonomy.yaml').write_text(yaml.safe_dump({'triggers': [{'id': 'daily', 'kind': 'schedule',
                                        'every_seconds': 60, 'task': 'Review due tasks'}]}))
    monkeypatch.setattr(module.time, 'time', lambda: now)
    assert events.poll() == []
    monkeypatch.setattr(module.time, 'time', lambda: now + 61)
    assert len(events.poll()) == 1
    assert events.poll() == []


@pytest.mark.parametrize('trigger', [{'id': 'private', 'kind': 'file', 'path': '.env', 'task': 'unsafe'},
                                    {'id': 'repeat', 'kind': 'schedule', 'every_seconds': 1, 'task': 'loop'},
                                    {'id': 'bad', 'kind': 'shell', 'task': 'execute untrusted program'}])
def test_unsafe_triggers_fail_closed(events, trigger):
    (events.queue.root / 'autonomy.yaml').write_text(yaml.safe_dump({'triggers': [trigger]}))
    with pytest.raises(ValueError):
        events.poll()
    assert events.studio.items('jarvis-job') == []


def test_local_inbox_digest_and_acknowledgement_use_actual_records(events):
    inbox = Inbox(events.studio)
    row = events.studio.put('jarvis-notice', {'job_id': 'test-job', 'state': 'done', 'summary': 'actual test notice', 'created': 1, 'read': False})
    assert inbox.digest()['unread'] == 1
    inbox.acknowledge(row['id'])
    assert inbox.digest()['unread'] == 0 and inbox.items()[0]['read']


def test_quiet_dnd_suppress_delivery_but_never_drop_results(events, monkeypatch):
    inbox = Inbox(events.studio)
    events.studio.put('jarvis-notice', {'job_id': 'real-fixture-job', 'state': 'done', 'summary': 'retained', 'created': 1, 'read': False})
    inbox.configure(quiet=True)
    assert inbox.notify()['suppressed'] and len(inbox.items()) == 1
    inbox.configure(quiet=False, minutes=10)
    assert inbox.notify()['suppressed']
    inbox.configure(minutes=0)
    assert len(inbox.notify()['notices']) == 1
    assert inbox.notify()['notices'] == [] and len(inbox.items()) == 1


@pytest.mark.parametrize('mission', ['dev', 'trading', 'pentest', 'study'])
def test_analysis_missions_keep_queue_approval_and_scope_boundaries(events, mission):
    row = events.queue.enqueue('Review supplied local material', mission=mission)
    assert row['status'] == 'awaiting_approval' and row['mission'] == mission
    assert 'User goal:' in row['task']


def test_file_event_survives_full_queue_and_reconciles_after_capacity_returns(events, monkeypatch):
    import nexus.jarvis.events as module
    now = module.time.time()
    monkeypatch.setattr(module.time, 'time', lambda: now)
    (events.queue.root / 'tasks.md').write_text('first')
    (events.queue.root / 'autonomy.yaml').write_text(yaml.safe_dump({'max_queue': 1, 'triggers': [
        {'id': 'tasks', 'kind': 'file', 'path': 'tasks.md', 'task': 'Inspect updated task file'}]}))
    events.poll()
    blocking = events.queue.enqueue('Queue capacity used')
    (events.queue.root / 'tasks.md').write_text('updated')
    assert events.poll() == []
    state = events.studio.items('jarvis-trigger')[0]
    assert state['pending'] and state['last_emit'] == 0
    events.queue.review(blocking['id'], 'cancel')
    monkeypatch.setattr(module.time, 'time', lambda: now + 61)
    assert len(events.poll()) == 1
    assert not events.studio.items('jarvis-trigger')[0]['pending']
