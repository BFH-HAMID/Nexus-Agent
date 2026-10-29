"""Real bounded agent/HTTP protocol tests; fixture is not live Ollama inference."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from nexus.jarvis.runtime import Runtime, execute, settings, signature
from nexus.server import ServerState


@pytest.fixture
def runtime(repo):
    (repo / 'autonomy.yaml').write_text(yaml.safe_dump({'runtime': {'enabled': True, 'local_model': 'fixture-local',
                                                                  'max_runs_day': 2, 'max_bytes_day': 200000}}))
    return Runtime(ServerState(repo, quiet=True).workbench.studio)


def approved(runtime, task='Inspect README, never modify it'):
    job = runtime.queue.enqueue(task)
    return runtime.queue.review(job['id'], 'approve', f'APPROVE QUEUED TASK {job["id"]}')


def test_runtime_requires_enabled_policy_and_exact_task_approval(runtime):
    runtime.queue.enqueue('Unapproved task')
    assert runtime.claim() is None
    job = approved(runtime, 'Approved different task')
    row = runtime.studio.find('jarvis-job', job['id'])
    row['task'] = 'Changed after approval'
    runtime.studio.put('jarvis-job', row, row['id'])
    with pytest.raises(ValueError, match='approval'):
        runtime.claim()


def test_claim_reserves_daily_budget_before_launch_and_never_replays(runtime):
    approved(runtime)
    claim = runtime.claim()
    assert claim['status'] == 'running' and claim['approval_digest'] == signature(claim)
    assert runtime.budget()['runs_reserved'] == 1 and runtime.budget()['bytes_reserved'] == 100000
    assert runtime.claim() is None
    runtime.finish(claim, 'interrupted', {'summary': 'crash simulation'})
    assert runtime.claim() is None
    assert runtime.budget()['runs_reserved'] == 1
    assert runtime.studio.items('jarvis-notice')[0]['state'] == 'interrupted'


def test_budget_cap_and_retry_require_fresh_approval(runtime):
    job = approved(runtime)
    for _number in range(2):
        row = runtime.claim()
        assert row is not None
        runtime.finish(row, 'error', {'summary': 'scripted failed attempt'})
        runtime.queue.review(job['id'], 'retry')
        assert runtime.claim() is None
        runtime.queue.review(job['id'], 'approve', f'APPROVE QUEUED TASK {job["id"]}')
    assert runtime.claim() is None and runtime.budget()['runs_reserved'] == 2


def test_remote_ollama_is_refused_before_agent_construction(runtime, monkeypatch):
    approved(runtime)
    row = runtime.claim()
    monkeypatch.setenv('OLLAMA_HOST', 'https://remote.example')
    result = execute(runtime.studio, row['id'], row['lease'], agent_factory=lambda *a, **k: pytest.fail('must not construct agent'))
    assert result['status'] == 'error' and result['result']['summary'].endswith('ValueError')


def test_real_agent_with_local_protocol_fixture_has_no_mutations_or_remote_adapter(runtime, monkeypatch):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(payload)
            body = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'Local inspection complete. No changes made.'},
                                            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 19, 'completion_tokens': 8}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('0.0.0.0', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    before = (runtime.queue.root / 'README.md').read_bytes()
    try:
        monkeypatch.setenv('OLLAMA_HOST', f'http://127.0.0.1:{server.server_port}')
        approved(runtime)
        row = runtime.claim()
        result = execute(runtime.studio, row['id'], row['lease'])
        assert result['status'] == 'done', result
        assert requests and all(r['max_tokens'] <= 512 for r in requests)
        assert result['result']['requests'] >= 1 and result['result']['input_tokens'] >= 19
        assert result['result']['direct_cloud_adapter_used'] is False
        assert (runtime.queue.root / 'README.md').read_bytes() == before
        assert result['result']['session_id']  # Existing history/learning path is reused.
        with pytest.raises(ValueError):
            execute(runtime.studio, row['id'], row['lease'])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('value', [{'enabled': 'yes'}, {'enabled': True}, {'max_runs_day': True}, {'max_requests': 0}, {'unknown': 1}])
def test_invalid_runtime_settings_fail_closed(value):
    with pytest.raises(ValueError):
        settings(value)


def test_cancel_running_job_revokes_lease_results(runtime):
    job = approved(runtime)
    row = runtime.claim()
    runtime.queue.review(job['id'], 'cancel')
    with pytest.raises(ValueError):
        runtime.finish(row, 'done', {'summary': 'stale worker output'})


def test_wire_budget_refuses_before_any_model_request(runtime, monkeypatch):
    import httpx

    from nexus.providers import OpenAICompatProvider, ProviderRegistry
    policy = runtime.queue.policy()
    policy['runtime']['max_request_bytes'] = 1000
    (runtime.queue.root / 'autonomy.yaml').write_text(yaml.safe_dump(policy))
    approved(runtime)
    row = runtime.claim()
    monkeypatch.setenv('OLLAMA_HOST', 'http://127.0.0.1:11434')
    def registry(config):
        client = httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail('budget must refuse before HTTP')))
        return ProviderRegistry(config.agents, factories={'selected-ollama': OpenAICompatProvider(config.agents['selected-ollama'], transport=client)})
    result = execute(runtime.studio, row['id'], row['lease'], registry_factory=registry)
    assert result['status'] == 'error' and runtime.budget()['runs_reserved'] == 1


def test_cpu_backpressure_does_not_reserve_or_launch(runtime, monkeypatch):
    import psutil
    approved(runtime)
    monkeypatch.setattr(psutil, 'cpu_percent', lambda **kwargs: 100)
    runtime.tick()
    assert runtime.active is None and runtime.budget()['runs_reserved'] == 0


def test_persisted_daily_wall_budget_is_reserved_before_launch(runtime):
    policy = runtime.queue.policy()
    policy['runtime']['max_seconds_day'] = 5
    (runtime.queue.root / 'autonomy.yaml').write_text(yaml.safe_dump(policy))
    approved(runtime)
    assert runtime.claim() is None and runtime.budget()['runs_reserved'] == 0


def test_real_daemon_dispatches_owned_worker_to_local_protocol_fixture(runtime, monkeypatch):
    import subprocess
    import sys
    import time

    from nexus.jarvis.daemon import Daemon
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'Fixture review complete; no source edits.'},
                                            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 7}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('0.0.0.0', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('OLLAMA_HOST', f'http://127.0.0.1:{server.server_port}')
    job = approved(runtime)
    daemon = Daemon(runtime.studio)
    receipt = daemon.request('start')
    process = subprocess.Popen([sys.executable, '-I', '-m', 'nexus', 'jarvis', '-C', str(runtime.queue.root),
                                'run', '--instance', receipt['control']['instance']], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        end = time.monotonic() + 15
        row = None
        while time.monotonic() < end:
            row = runtime.studio.find('jarvis-job', job['id'])
            if row['status'] in {'done', 'error', 'interrupted'}:
                break
            threading.Event().wait(.1)
        assert row['status'] == 'done', row
        assert row['result']['session_id'] and row['worker_started']
        assert runtime.budget()['runs_reserved'] == 1
    finally:
        daemon.request('stop')
        try:
            process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
