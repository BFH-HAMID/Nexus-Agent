"""Six honest scripted integration demos. Real local effects, NO live account/model claim."""
import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time

import yaml

from nexus.hud.core import HUD
from nexus.jarvis.inbox import Inbox
from nexus.jarvis.runtime import Runtime
from nexus.server import ServerState
from nexus.sessions import SessionStore
from nexus.tools import ApprovalDecision, ApprovalGate
from nexus.types import Risk, ToolSpec
from tests.conftest import finish_completion, text_completion, tool_completion
from tests.test_agent import make_agent
from tests.test_github_completion import action, passing, project_service, scaffold_project
from tests.test_mcp import ROOT, extension
from tests.test_orchestration import service_for
from tests.test_telegram import PIN, bot, message
from tests.test_trace import run_file

# Imported fixtures intentionally share existing real adapters, not parallel implementations.
__all__ = ['project_service', 'extension', 'bot']


def unittest_command(module):
    args = [sys.executable, '-m', 'unittest', module, '-v']
    return subprocess.list2cmdline(args) if os.name == 'nt' else shlex.join(args)


def execute_script(repo, script, task):
    agent = make_agent(repo, script, approval_callback=lambda *args: ApprovalDecision(True))
    return agent, agent.run(task, role='general', learn=False)


def test_demo_1_ecommerce_plan_actual_cart_tests_and_payment_boundary(repo, tmp_path):
    planner, provider = service_for(repo, tmp_path)
    routed = planner.route('Build an ecommerce cart and checkout with explicit payment approval')
    definition = planner.plan(routed)
    assert 3 <= len(routed['roles']) <= 7 and len(definition['nodes']) == 20
    assert SessionStore(planner.sessions.path).observation(routed['trace_id'], repo)['state'] == 'finished'
    cart = 'def total(items):\n    if any(price < 0 or quantity < 0 for price, quantity in items):\n        raise ValueError("negative cart")\n    return sum(price * quantity for price, quantity in items)\n'
    tests = 'import unittest\nfrom cart import total\nclass CartTests(unittest.TestCase):\n    def test_total(self): self.assertEqual(total([(10,2),(5,1)]),25)\n    def test_negative(self):\n        with self.assertRaises(ValueError): total([(-1,1)])\n'
    agent, result = execute_script(repo, [tool_completion([('write_file', {'path': 'cart.py', 'content': cart}),
        ('write_file', {'path': 'test_cart.py', 'content': tests})]),
        tool_completion([('run_command', {'command': unittest_command('test_cart')})]), finish_completion('cart verified locally')], 'implement cart.py and run cart tests')
    assert result.ok and (repo / 'cart.py').is_file()
    trace = agent.sessions.observation(result.trace_id, repo)
    assert any(e['kind'] == 'process' and e['data'].get('exit_code') == 0 for e in trace['events'])
    payment = ToolSpec('payment_charge', 'External payment requires new operator authorization', {'type': 'object'}, Risk.DANGEROUS)
    assert not ApprovalGate(mode='auto').check(payment, {'amount': 25}).approved
    # No payment provider/gateway is connected or charged by this scripted demo.


def test_demo_2_actual_failing_test_fix_branch_peer_review_and_durable_inbox(repo):
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture base'], check=True)
    before = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    subprocess.run(['git', '-C', str(repo), 'switch', '-qc', 'fix/scripted-demo'], check=True)
    (repo / 'bug.py').write_text('def add(a,b): return a-b\n')
    (repo / 'test_bug.py').write_text('import unittest\nfrom bug import add\nclass BugTest(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2,3),5)\n')
    failed = subprocess.run([sys.executable, '-m', 'unittest', 'test_bug'], cwd=repo, capture_output=True, timeout=15)
    assert failed.returncode != 0
    agent, result = execute_script(repo, [tool_completion([('write_file', {'path': 'bug.py', 'content': 'def add(a,b):\n    return a+b\n'})]),
        tool_completion([('run_command', {'command': unittest_command('test_bug')})]), finish_completion('actual regression fixed')], 'fix bug.py and verify regression')
    assert result.ok
    reviewer = make_agent(repo, [tool_completion([('read_file', {'path': 'bug.py'})]), text_completion('Reviewed actual change against the recorded regression command')])
    reviewed = reviewer.run('review bug.py only', role='general', read_only=True, learn=False)
    agent.sessions.peer_review(result.trace_id, repo, reviewed.trace_id, True, 'Reviewed changed file and actual subprocess outcome', [result.trace_id])
    subprocess.run(['git', '-C', str(repo), 'add', 'bug.py', 'test_bug.py'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fix: actual regression'], check=True)
    assert subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD^'], text=True).strip() == before
    (repo / 'autonomy.yaml').write_text(yaml.safe_dump({'runtime': {'enabled': True, 'local_model': 'fixture-local'}}))
    runtime = Runtime(ServerState(repo, quiet=True).workbench.studio)
    job = runtime.queue.enqueue('Report the measured regression/peer-review outcome')
    runtime.queue.review(job['id'], 'approve', 'APPROVE QUEUED TASK ' + job['id'])
    claimed = runtime.claim()
    runtime.finish(claimed, 'done', {'summary': 'Scripted worker result: actual regression passed and peer reviewed; trace ' + result.trace_id})
    assert Inbox(runtime.studio).digest()['unread'] == 1


def test_demo_3_context_reference_resolves_real_file_without_cloud_or_screenshot(repo):
    from nexus.context.core import Context
    (repo / 'context.yaml').write_text(yaml.safe_dump({'enabled': True, 'signals': {'vscode': True}}))
    (repo / 'view.py').write_text('broken = True\n')
    context = Context(repo)
    context.record('vscode', {'path': 'view.py', 'line': 1})
    agent, result = execute_script(repo, [tool_completion([('read_file', {'path': 'view.py'})]), text_completion('This file currently sets broken to True')], 'explain this file')
    assert result.ok
    body = agent.sessions.observation(result.trace_id, repo)
    assert next(e for e in body['events'] if e['kind'] == 'context')['data']['references']['vscode']['path'] == 'view.py'
    assert not list(repo.glob('*.jpg'))
    context.clear()


def test_demo_4_telegram_delegation_pin_hud_exact_action_and_scripted_github_pr(bot, project_service, monkeypatch):
    from fastapi.testclient import TestClient

    from nexus.hud.app import create_app
    from nexus.jarvis.actions import project
    service, remote, repo = project_service
    scaffold_project(service)
    action(service, 'push')
    head = service.get('shop')['remote_head']
    remote['checks'][head] = [passing(head)]  # Explicitly scripted remote CI, not live GitHub.
    (repo / 'autonomy.yaml').write_text(yaml.safe_dump({'runtime': {'enabled': True, 'local_model': 'fixture-local', 'projects': ['shop'], 'connectivity': 'online'}}))
    monkeypatch.setattr(type(bot.studio.wb.repositories.github), 'projects', property(lambda self: service))
    identifier = bot.handle(message('/delegate Create reviewed shop PR')).split()[-1]
    challenge = bot.handle(message('/approve ' + identifier)).split('/confirm ')[1].split()[0]
    bot.handle(message('/confirm ' + challenge + ' ' + PIN))
    runtime = Runtime(bot.studio)
    job = runtime.claim()
    results, errors = [], []
    def worker():
        try:
            results.append(project(bot.studio, job, {'name': 'shop', 'action': 'pr'}))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    approval = None
    while time.monotonic() < deadline:
        rows = [r for r in bot.studio.items('jarvis-approval') if r.get('confirmation') and r['state'] == 'pending']
        if rows:
            approval = rows[0]
            break
        threading.Event().wait(.01)
    assert approval is not None and not remote['pr']
    token = 'scripted-hud-pr-private-fixture-token'
    with TestClient(create_app(bot.studio, token), base_url='http://localhost') as client:
        response = client.post('/api/action/decide', headers={'Authorization': 'Bearer ' + token, 'Origin': 'http://localhost'},
            json={'id': approval['id'], 'allow': True, 'confirmation': approval['confirmation']})
        assert response.status_code == 200
    thread.join(timeout=5)
    assert not thread.is_alive() and not errors and results
    assert service.get('shop')['pr'] == 12 and remote['refs']['main'] == 'b' * 40
    assert HUD(bot.studio).snapshot()['jobs_total'] == 1


def test_demo_5_manifest_plugin_added_without_core_edits_runs_real_child(extension):
    ctx, registry = extension
    core = ROOT / 'nexus/tools.py'
    before = hashlib.sha256(core.read_bytes()).hexdigest()
    output, error = registry.call('mcp_words_count', {'text': 'plugin without core edits'})
    assert not error and json.loads(output)['result']['content'][0]['text'] == '4'
    assert hashlib.sha256(core.read_bytes()).hexdigest() == before


def test_demo_6_safe_step_replay_has_no_effects_until_fresh_authorization(repo):
    from nexus.evaluation.replay import inspect, reexecute
    agent, result, body, step = run_file(repo)
    (repo / 'artifact.txt').write_text('new state to inspect')
    assert inspect(agent.sessions, repo, result.trace_id, step)['effects'] is False
    assert (repo / 'artifact.txt').read_text() == 'new state to inspect'
    approvals = []
    reexecute(agent.sessions, repo, result.trace_id, step, lambda spec, args, *rest: approvals.append(spec.name) or ApprovalDecision(True))
    assert approvals[0] == 'trace_reexecute' and (repo / 'artifact.txt').read_text() == 'original'
