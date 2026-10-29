"""Module 4 completion tests: real files/queue/gate; network and OS fixtures explicit."""
import json
import threading
import time
from types import SimpleNamespace

import pytest
import yaml

from nexus.jarvis.approvals import Approvals
from nexus.jarvis.events import Events
from nexus.jarvis.runtime import Runtime, execute
from nexus.server import ServerState
from nexus.types import Risk, Role, ToolSpec


@pytest.fixture
def runtime(repo):
    (repo / 'work').mkdir()
    (repo / 'autonomy.yaml').write_text(yaml.safe_dump({'runtime': {'enabled': True, 'local_model': 'fixture-local',
                                                'mode': 'reviewed_write', 'write_roots': ['work']}}))
    return Runtime(ServerState(repo, quiet=True).workbench.studio)


def configure(engine, **values):
    path = engine.queue.root / 'autonomy.yaml'
    data = yaml.safe_load(path.read_text())
    settings = values.pop('runtime', {})
    data.update(values)
    data['runtime'].update(settings)
    path.write_text(yaml.safe_dump(data))


def claim(runtime):
    job = runtime.queue.enqueue('Review the scoped local task')
    runtime.queue.review(job['id'], 'approve', 'APPROVE QUEUED TASK ' + job['id'])
    return runtime.claim()


def await_row(approvals):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rows = approvals.items()
        if rows and rows[-1].get('confirmation'):
            return rows[-1]
        threading.Event().wait(.01)
    pytest.fail('durable approval not created')


def pending(runtime, job, path='work/file.txt'):
    approvals = Approvals(runtime.studio)
    results = []
    thread = threading.Thread(target=lambda: results.append(approvals.callback(job)(
        ToolSpec('write_file', 'Write scoped file', {}, risk=Risk.WRITE),
        {'path': path, 'content': 'reviewed'}, Risk.WRITE, 'actual proposed change')))
    thread.start()
    return approvals, thread, results, await_row(approvals)


@pytest.mark.parametrize('allow', [True, False])
def test_background_action_requires_exact_one_use_decision(runtime, allow):
    job = claim(runtime)
    approvals, thread, results, row = pending(runtime, job)
    try:
        with pytest.raises(ValueError):
            approvals.decide(row['id'], True, 'yes')
        approvals.decide(row['id'], allow, row['confirmation'] if allow else '')
        thread.join(3)
        assert results[0].approved is allow
        assert runtime.studio.find('jarvis-approval', row['id'])['state'] == 'consumed'
        with pytest.raises(ValueError):
            approvals.decide(row['id'], True, row['confirmation'])
    finally:
        runtime.queue.review(job['id'], 'cancel')
        thread.join(3)


def test_changed_preimage_invalidates_approval(runtime):
    path = runtime.queue.root / 'work/file.txt'
    path.write_text('before')
    job = claim(runtime)
    approvals, thread, results, row = pending(runtime, job)
    path.write_text('changed during review')
    approvals.decide(row['id'], True, row['confirmation'])
    thread.join(3)
    assert not results[0].approved and path.read_text() == 'changed during review'


@pytest.mark.parametrize('revocation', ['cancel', 'policy', 'lease'])
def test_action_wait_cannot_outlive_job_authority(runtime, revocation):
    job = claim(runtime)
    approvals, thread, results, row = pending(runtime, job)
    if revocation == 'cancel':
        runtime.queue.review(job['id'], 'cancel')
    elif revocation == 'policy':
        configure(runtime, level='L0')
    else:
        current = runtime.studio.find('jarvis-job', job['id'])
        current['lease'] = 'replaced'
        runtime.studio.put('jarvis-job', current, current['id'])
    thread.join(3)
    assert not thread.is_alive() and not results[0].approved
    assert runtime.studio.find('jarvis-approval', row['id'])['state'] == 'expired'


def test_secret_is_rejected_before_durable_approval(runtime, monkeypatch):
    key = 'ghp_' + 'a' * 36
    monkeypatch.setenv('GITHUB_TOKEN', key)
    job = claim(runtime)
    with pytest.raises(ValueError):
        Approvals(runtime.studio).callback(job)(ToolSpec('write_file', 'write', {}),
                                  {'path': 'work/file.txt', 'content': key}, Risk.WRITE, '')
    assert Approvals(runtime.studio).items() == []


@pytest.mark.parametrize('level,source,expected', [('L1', 'todo', 'awaiting_approval'), ('L2', 'todo', 'approved'),
                                                 ('L3', 'todo', 'approved'), ('L2', 'git', 'awaiting_approval')])
def test_autonomy_only_admits_explicit_sources(runtime, level, source, expected):
    configure(runtime, level=level, runtime={'auto_sources': ['todo']})
    row = Events(runtime.queue).publish(source, 'Inspect configured event')
    assert row['status'] == expected
    if expected == 'approved':
        assert runtime.claim()['id'] == row['id']


@pytest.mark.parametrize('revoke', [{'level': 'L1'}, {'runtime': {'auto_sources': []}}])
def test_autonomy_revocation_downgrades_queued_work_to_human(runtime, revoke):
    configure(runtime, level='L2', runtime={'auto_sources': ['todo']})
    row = Events(runtime.queue).publish('todo', 'Inspect configured event')
    configure(runtime, **revoke)
    assert runtime.claim() is None
    row = runtime.studio.find('jarvis-job', row['id'])
    assert row['status'] == 'awaiting_approval'
    runtime.queue.review(row['id'], 'approve', 'APPROVE QUEUED TASK ' + row['id'])
    assert runtime.claim()['id'] == row['id']


@pytest.mark.parametrize('level,explicit,existing,auto', [('L2', True, False, False), ('L3', False, False, False),
                 ('L3', True, True, False), ('L3', True, False, True)])
def test_real_gate_registry_scopes_l3_and_snapshots(runtime, monkeypatch, level, explicit, existing, auto):
    from nexus.agent import NexusAgent, RunResult
    from nexus.tools import ApprovalDecision
    configure(runtime, level=level, runtime={'allow_safe_writes': True})
    (runtime.queue.root / 'permissions.yaml').write_text(yaml.safe_dump({'default': 'AUTO_ALLOW',
                                             'tools': {'write_file': 'AUTO_ALLOW'} if explicit else {}}))
    if existing:
        (runtime.queue.root / 'work/new.txt').write_text('preimage')
    callbacks = []
    def denied(self, job):
        return lambda *args: callbacks.append(args) or ApprovalDecision(False, reason='test human denies')
    monkeypatch.setattr(Approvals, 'callback', denied)
    def run(agent, *args, **kwargs):
        _, tools, _ = agent.tools_for(Role(name='fixture', persona='Fixture'))
        assert not any(tools.has(n) for n in ('run_command', 'git', 'gh', 'browser_action', 'office_file'))
        assert tools.call('write_file', {'path': 'outside.txt', 'content': 'no'})[1]
        text, failed = tools.call('write_file', {'path': 'work/new.txt', 'content': 'new text'})
        assert failed is not auto, text
        return RunResult(text='Actual gated tool test')
    monkeypatch.setattr(NexusAgent, 'run', run)
    row = claim(runtime)
    result = execute(runtime.studio, row['id'], row['lease'])
    assert result['status'] == 'done', result
    assert bool(callbacks) is not auto
    if auto:
        assert (runtime.queue.root / 'work/new.txt').read_text() == 'new text'
        from nexus.snapshots import Snapshots
        assert Snapshots(runtime.queue.root).listing()


@pytest.mark.parametrize('kind,filename,content', [('todo', 'tasks.md', 'TODO: inspect failure'),
                ('build', 'build.json', '{"exit_code": 2, "summary": "untrusted secret content"}')])
def test_real_todo_and_build_artifacts_admit_without_contents(runtime, kind, filename, content):
    configure(runtime, triggers=[{'id': 'probe', 'kind': kind, 'path': filename, 'task': 'Inspect event locally'}])
    (runtime.queue.root / filename).write_text(content)
    events = Events(runtime.queue)
    jobs = events.poll()
    assert len(jobs) == 1 and events.poll() == []
    assert content not in str(runtime.studio.items('jarvis-trigger'))
    assert runtime.studio.find('jarvis-job', jobs[0])['status'] == 'awaiting_approval'


def test_git_probe_never_runs_filters_or_shell(runtime, monkeypatch):
    import subprocess

    from nexus.jarvis.sources import git
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('must never execute Git hooks/filters'))
    state = git(runtime.queue.root)
    assert len(state['head']) >= 40 and len(state['index']) == 64


def test_system_threshold_probe_uses_actual_metrics_adapter(runtime, monkeypatch):
    import psutil
    configure(runtime, triggers=[{'id': 'memory', 'kind': 'system', 'metric': 'memory', 'threshold': 80,
                                  'poll_seconds': 1, 'task': 'Investigate resource pressure'}])
    monkeypatch.setattr(psutil, 'virtual_memory', lambda: SimpleNamespace(percent=90))
    assert len(Events(runtime.queue).poll()) == 1


def test_offline_ci_defers_without_network_then_reconciles(runtime, monkeypatch):
    import nexus.jarvis.sources as sources
    configure(runtime, triggers=[{'id': 'ci', 'kind': 'ci', 'repo': 'owner/project', 'task': 'Investigate failed checks'}])
    calls = []
    monkeypatch.setattr(sources, 'github', lambda *args: calls.append(args) or ({'sha': 'a' * 40, 'checks': ['failure']}, True))
    events = Events(runtime.queue)
    assert events.poll() == [] and not calls
    state = runtime.studio.items('jarvis-trigger')[0]
    assert state['deferred']
    configure(runtime, runtime={'connectivity': 'online'})
    state['polled'] = 0
    state['last_attempt'] = 0
    runtime.studio.put('jarvis-trigger', state, state['id'])
    assert len(events.poll()) == 1 and len(calls) == 1
    assert not runtime.studio.items('jarvis-trigger')[0]['deferred']


def test_ci_http_transport_is_env_only_read_only_bounded(runtime, monkeypatch):
    import httpx

    from nexus.jarvis.sources import github
    key = 'ghp_' + 'b' * 36
    monkeypatch.setenv('GITHUB_TOKEN', key)
    requests = []
    def handle(request):
        requests.append(request)
        assert request.method == 'GET' and request.url.host == 'api.github.com'
        assert request.headers['Authorization'] == 'Bearer ' + key
        body = {'sha': 'a' * 40} if '/check-runs' not in str(request.url) else {'total_count': 1,
                      'check_runs': [{'id': 1, 'name': key, 'status': 'completed', 'conclusion': 'failure'}]}
        return httpx.Response(200, json=body)
    native = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: native(transport=httpx.MockTransport(handle), **kwargs))
    state, failing = github(runtime.queue.root, 'owner/project', 'feature/test')
    assert failing and len(requests) == 2 and key not in str(state)


def test_inbox_bus_retains_parent_for_depth_and_does_not_replay_ack(runtime):
    from nexus.jarvis.sources import sample
    job = claim(runtime)
    runtime.finish(job, 'done', {'summary': 'Actual completed task'})
    trigger = {'kind': 'inbox'}
    state, fresh, parent = sample(runtime.queue, trigger, {})
    assert fresh and parent == job['id']
    child = Events(runtime.queue).publish('inbox', 'Review completed notice', parent=parent)
    assert child['depth'] == 1
    assert sample(runtime.queue, trigger, state)[1] is False


def test_project_bridge_uses_exact_review_preview_and_existing_executor(runtime, monkeypatch):
    from nexus.jarvis.actions import project
    from nexus.tools import ApprovalDecision
    configure(runtime, runtime={'projects': ['demo'], 'connectivity': 'online'})
    actions = []
    service = SimpleNamespace(preview=lambda args: {'data': {'id': 'preview-1', 'confirmation': 'EXACT HEAD', 'head': 'a' * 40}},
                              execute=lambda args: actions.append(args) or {'data': {'committed': True}})
    monkeypatch.setattr(type(runtime.studio.wb.repositories.github), 'projects', property(lambda self: service))
    decisions = []
    monkeypatch.setattr(Approvals, 'callback', lambda self, job: lambda *args: decisions.append(args) or ApprovalDecision(True))
    assert json.loads(project(runtime.studio, {}, {'name': 'demo', 'action': 'commit', 'message': 'fix: tests'}))['committed']
    assert decisions[0][2] is Risk.DANGEROUS and 'EXACT HEAD' in decisions[0][3]
    assert actions == [{'preview_id': 'preview-1', 'confirm': 'EXACT HEAD'}]
    configure(runtime, runtime={'connectivity': 'offline'})
    with pytest.raises(ValueError):
        project(runtime.studio, {}, {'name': 'demo', 'action': 'push'})
    assert len(actions) == 1


def test_native_notifications_opt_in_quiet_dnd_retry_and_no_read_claim(runtime, monkeypatch):
    import nexus.jarvis.notifications as notifications
    from nexus.jarvis.inbox import Inbox
    inbox = Inbox(runtime.studio)
    runtime.studio.put('jarvis-notice', {'job_id': 'actual-job', 'state': 'done', 'summary': 'private task text', 'created': 1, 'read': False})
    calls = []
    monkeypatch.setattr(notifications, 'submit', lambda *args: calls.append(args) or {'submitted': True, 'human_read_confirmed': False})
    assert not inbox.push()['submitted'] and not calls
    inbox.configure(transport='native', quiet=True)
    assert not inbox.push()['submitted'] and not calls
    inbox.configure(quiet=False, minutes=10)
    assert not inbox.push()['submitted'] and not calls
    inbox.configure(minutes=0)
    assert inbox.push()['submitted'] and calls == [(1, 0)]
    assert not inbox.items()[0]['read'] and not inbox.items()[0].get('delivered')
    assert not inbox.push()['submitted'] and len(calls) == 1


def test_native_transport_never_exports_environment_keys_or_task_text(monkeypatch):
    import subprocess

    import nexus.jarvis.notifications as module
    monkeypatch.setenv('GITHUB_TOKEN', 'never-export-notification-key')
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/usr/bin/notify-send')
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(returncode=0))
    assert module.submit(2, 1)['submitted']
    assert '2 unread results' in calls[0][0][-1]
    assert 'GITHUB_TOKEN' not in calls[0][1]['env'] and calls[0][1]['timeout'] == 8
    assert calls[0][1]['stdin'] == subprocess.DEVNULL


def test_native_transport_failure_preserves_inbox(runtime, monkeypatch):
    import nexus.jarvis.notifications as module
    from nexus.jarvis.inbox import Inbox
    inbox = Inbox(runtime.studio)
    runtime.studio.put('jarvis-notice', {'job_id': 'job', 'state': 'done', 'summary': 'result', 'created': 1, 'read': False})
    inbox.configure(transport='native')
    def unavailable(*args):
        raise OSError('sensitive OS details must not persist')
    monkeypatch.setattr(module, 'submit', unavailable)
    assert inbox.push() == {'submitted': False, 'error_type': 'OSError'}
    assert not inbox.items()[0].get('delivered') and not inbox.items()[0]['read']
    assert 'sensitive' not in str(inbox.delivery_policy())


def test_slow_probe_cannot_block_daemon_stop_or_resource_tick(runtime, monkeypatch):
    from nexus.jarvis.daemon import Daemon
    entered, release = threading.Event(), threading.Event()
    def slow(self):
        entered.set()
        release.wait(5)
        return []
    monkeypatch.setattr(Events, 'poll', slow)
    daemon = Daemon(runtime.studio)
    receipt = daemon.request('start')
    results = []
    thread = threading.Thread(target=lambda: results.append(daemon.run(receipt['control']['instance'])))
    thread.start()
    try:
        assert entered.wait(3)
        daemon.request('stop')
        thread.join(3)
        assert not thread.is_alive() and results == [0]
    finally:
        release.set()
        thread.join(5)


def test_policy_revoked_during_human_callback_is_rechecked_before_tools(runtime, monkeypatch):
    from nexus.agent import NexusAgent, RunResult
    from nexus.tools import ApprovalDecision
    def revoke(self, job):
        def callback(*args):
            runtime.queue.review(job['id'], 'cancel')
            return ApprovalDecision(True)
        return callback
    monkeypatch.setattr(Approvals, 'callback', revoke)
    def run(agent, *args, **kwargs):
        _, tools, _ = agent.tools_for(Role(name='fixture'))
        result, failed = tools.call('write_file', {'path': 'work/no.txt', 'content': 'refused'})
        assert failed and not (runtime.queue.root / 'work/no.txt').exists(), result
        return RunResult(text='Revocation worked')
    monkeypatch.setattr(NexusAgent, 'run', run)
    row = claim(runtime)
    with pytest.raises(ValueError, match='lease revoked'):
        execute(runtime.studio, row['id'], row['lease'])
    assert not (runtime.queue.root / 'work/no.txt').exists()



def test_online_required_jobs_reconcile_without_spending_offline_budget(runtime):
    job = runtime.queue.enqueue('Review a mapped network project', requires_online=True)
    runtime.queue.review(job['id'], 'approve', 'APPROVE QUEUED TASK ' + job['id'])
    assert runtime.claim() is None and runtime.budget()['runs_reserved'] == 0
    assert runtime.studio.find('jarvis-job', job['id'])['status'] == 'approved'
    configure(runtime, runtime={'connectivity': 'online'})
    assert runtime.claim()['id'] == job['id'] and runtime.budget()['runs_reserved'] == 1


def test_pending_event_tasks_are_in_inbox_digest_and_native_reviews(runtime, monkeypatch):
    import nexus.jarvis.notifications as module
    from nexus.jarvis.inbox import Inbox
    inbox = Inbox(runtime.studio)
    job = Events(runtime.queue).publish('ci', 'Review actual check failure')
    assert inbox.pending()['jobs'][0]['id'] == job['id']
    assert inbox.digest()['pending_jobs'] == 1
    calls = []
    monkeypatch.setattr(module, 'submit', lambda *args: calls.append(args) or {'submitted': True})
    inbox.configure(transport='native')
    assert inbox.push()['submitted'] and calls == [(0, 1)]


@pytest.mark.skipif(not hasattr(__import__('os'), 'mkfifo'), reason='FIFO fixture is POSIX only')
def test_probe_rejects_fifo_without_waiting_for_a_writer(runtime):
    import os

    from nexus.jarvis.sources import bounded
    path = runtime.queue.root / 'fifo'
    os.mkfifo(path)
    with pytest.raises(ValueError):
        bounded(path)


@pytest.mark.parametrize('setting,value', [('max_runs_hour', 1), ('max_requests_hour', 8),
                   ('max_tokens_day', 105000), ('max_actions_day', 20)])
def test_hourly_daily_quotas_persist_before_second_launch(runtime, setting, value):
    configure(runtime, runtime={setting: value})
    job = claim(runtime)
    runtime.finish(job, 'done', {'summary': 'completed actual reservation'})
    next_job = runtime.queue.enqueue('A different bounded task')
    runtime.queue.review(next_job['id'], 'approve', 'APPROVE QUEUED TASK ' + next_job['id'])
    assert Runtime(runtime.studio).claim() is None
    assert runtime.budget()['runs_reserved'] == 1


def test_per_job_action_budget_caps_real_registry_calls(runtime, monkeypatch):
    from nexus.agent import NexusAgent, RunResult
    configure(runtime, runtime={'max_actions': 1})
    def run(agent, *args, **kwargs):
        _, tools, _ = agent.tools_for(Role(name='fixture'))
        assert not tools.call('list_files', {'path': '.'})[1]
        text, failed = tools.call('list_files', {'path': '.'})
        assert failed and 'action budget' in text
        return RunResult(text='Bound enforced')
    monkeypatch.setattr(NexusAgent, 'run', run)
    job = claim(runtime)
    assert execute(runtime.studio, job['id'], job['lease'])['status'] == 'done'
    assert runtime.studio.find('jarvis-job', job['id'])['action_calls'] == 1


@pytest.mark.parametrize('returncode', [0, 1])
def test_user_service_installer_exact_manifest_and_partial_outcome(runtime, tmp_path, monkeypatch, returncode):
    import hashlib
    import subprocess
    from pathlib import Path

    import nexus.jarvis.services as services
    from nexus.jarvis.services import install, manifest
    monkeypatch.setattr(services.sys, 'platform', 'linux')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'home'))
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(returncode=returncode))
    data = manifest(runtime.queue.root, 'systemd')
    with pytest.raises(ValueError):
        install(runtime.queue.root, 'systemd', confirmation='yes')
    assert not calls
    phrase = 'INSTALL SERVICE ' + data['filename'] + ' ' + hashlib.sha256(data['content'].encode()).hexdigest()[:16]
    result = install(runtime.queue.root, 'systemd', confirmation=phrase)
    assert result['installed'] is (returncode == 0)
    assert Path(result['configuration_written']).read_text() == data['content']
    assert calls[0][0] == ['systemctl', '--user', 'daemon-reload']
    if returncode:
        assert result['needs_manual_review']
    with pytest.raises(FileExistsError):
        install(runtime.queue.root, 'systemd', confirmation=phrase)


def test_action_consent_feedback_reuses_existing_learning_store(runtime):
    from nexus.sessions import SessionStore
    job = claim(runtime)
    current = runtime.studio.find('jarvis-job', job['id'])
    current['active_role'] = 'general'
    runtime.studio.put('jarvis-job', current, current['id'])
    approvals, thread, results, row = pending(runtime, job)
    decided = approvals.decide(row['id'], False)
    thread.join(3)
    assert decided['feedback_id'] and not results[0].approved
    quality = SessionStore().role_quality(runtime.queue.root)
    assert 'general' in quality



def test_wheel_manifest_includes_real_operator_policy_examples():
    from pathlib import Path

    import tomllib
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / 'pyproject.toml').read_text())
    included = config['tool']['hatch']['build']['targets']['wheel']['force-include']
    for name in ('autonomy.yaml', 'permissions.yaml', 'role-index.yaml', '.env.example', 'docs/jarvis.md'):
        assert included[name].startswith('nexus/_config/') and (root / name).is_file()
