"""Module 6 first slice: real existing-session aggregation, no side effects."""
import json
from types import SimpleNamespace

import pytest

from nexus.sessions import SessionStore


def record(store, repo, role='general', ok=True, duration=2):
    return store.record(SimpleNamespace(role=role, ok=ok, duration_s=duration, tool_calls=1, turns=2, text='Agent claim, not verified'), 'task', workspace=repo)


def test_empty_report_has_no_fake_success_or_cost(repo):
    report = SessionStore().evaluation_report(repo, roles=['general'])
    assert report['totals']['runs'] == 0 and report['unused_roles'] == ['general']
    assert report['actual_cost_usd'] is None and report['verified_test_successes'] is None
    assert report['advisory_only'] and not report['role_changes']


def test_weekly_counts_actual_rows_and_keeps_workspace_isolation(repo):
    store = SessionStore()
    record(store, repo)
    record(store, repo, ok=False, duration=3)
    record(store, repo / 'other', duration=99)
    report = store.evaluation_report(repo)
    assert report['totals'] == {'runs': 2, 'reported_ok': 1, 'tool_calls': 2, 'turns': 4, 'duration_s': 5.0}
    assert report['roles'][0]['reported_ok_rate'] == .5
    assert 'Agent claim' not in json.dumps(report)


def test_old_and_future_rows_do_not_count_as_this_week(repo):
    store = SessionStore()
    old, future = record(store, repo), record(store, repo)
    from nexus.sessions import _now
    with store._lock, store._connect() as conn:
        conn.execute('UPDATE sessions SET created=? WHERE id=?', (_now() - 9 * 86400, old))
        conn.execute('UPDATE sessions SET created=? WHERE id=?', (_now() + 86400, future))
    assert store.evaluation_report(repo)['totals']['runs'] == 0


def test_feedback_is_separate_from_run_success_and_unused_is_advisory(repo):
    store = SessionStore()
    record(store, repo, 'used')
    store.record_routing_feedback('task', ['used'], False, workspace=repo)
    report = store.evaluation_report(repo, roles=['used', 'unused'])
    assert report['totals']['reported_ok'] == 1
    assert report['routing_feedback'] == {'observations': 1, 'approved': 0}
    assert report['unused_roles'] == ['unused'] and not report['role_changes']


@pytest.mark.parametrize('days', [0, -1, True, 366, '7'])
def test_bad_windows_fail_closed(repo, days):
    with pytest.raises(ValueError):
        SessionStore().evaluation_report(repo, days=days)


def test_failure_advice_never_deletes_or_rewrites_roles(repo):
    store = SessionStore()
    for _ in range(3):
        record(store, repo, ok=False)
    before = list(repo.rglob('*.yaml'))
    report = store.evaluation_report(repo)
    assert report['roles'][0]['advice'] == 'inspect_failures_and_reviews'
    assert list(repo.rglob('*.yaml')) == before
    assert store.stats()['sessions'] == 3


def test_hud_weekly_report_uses_same_real_store(repo):
    from nexus.hud.core import HUD
    from nexus.server import ServerState
    store = SessionStore()
    record(store, repo)
    report = HUD(ServerState(repo, quiet=True).workbench.studio).snapshot()['evaluation']
    assert report['totals']['runs'] == 1 and report['actual_cost_usd'] is None


def test_evaluation_cli_is_a_real_read_only_consumer(repo, capsys, monkeypatch):
    from nexus.evaluation.cli import command
    monkeypatch.setattr('nexus.providers.build_providers', lambda *a, **k: pytest.fail('must not start models'))
    record(SessionStore(), repo)
    assert command(SimpleNamespace(workspace=str(repo), days=7)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['totals']['runs'] == 1 and report['advisory_only']


def test_cli_invalid_window_returns_controlled_error(repo, capsys):
    from nexus.evaluation.cli import command
    assert command(SimpleNamespace(workspace=str(repo), days=0)) == 2
    assert 'error' in json.loads(capsys.readouterr().out)
