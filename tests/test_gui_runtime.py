"""Real Bot/Context with scripted HTTP; no live Telegram or desktop capture."""
import json
import threading
import time

import httpx
import pytest
import yaml
from filelock import FileLock

from nexus.gui_desktop import dispatch
from nexus.gui_telegram import runtime
from nexus.jarvis.queue import Queue
from nexus.server import ServerState
from nexus.telegram.bot import Bot

TOKEN = '123456789:' + 'a' * 30
PIN = '86420975'


def enabled(state, kind, cfg):
    (state.workspace / (kind + '.yaml')).write_text(yaml.safe_dump(cfg))
    return dispatch(state)['policies'][kind]['revision']


@pytest.fixture
def telegram(repo, monkeypatch):
    state = ServerState(repo, quiet=True)
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', TOKEN)
    monkeypatch.setenv('TELEGRAM_APPROVAL_PIN', PIN)
    rev = enabled(state, 'telegram', {'enabled': True, 'user_ids': [123]})
    yield state, rev
    runtime(state).stop(timeout=2)


def start(state, rev):
    return dispatch(state, {'operation': 'telegram-start', 'revision': rev, 'confirmation': 'START TELEGRAM POLLING'})


def test_cancel_and_stale_start_never_construct_a_bot(telegram, monkeypatch):
    state, rev = telegram
    calls = []
    monkeypatch.setattr('nexus.telegram.bot.Bot', lambda *a, **k: calls.append(True))
    for payload in [{'operation': 'telegram-start', 'revision': rev, 'confirmation': 'wrong'},
                    {'operation': 'telegram-start', 'revision': 'stale', 'confirmation': 'START TELEGRAM POLLING'},
                    {'operation': 'telegram-stop', 'confirmation': 'wrong'}]:
        with pytest.raises(ValueError):
            dispatch(state, payload)
    assert not calls
    assert not hasattr(state, 'telegram_runtime')


def test_supervised_stop_discards_inflight_updates_and_reports_real_heartbeat(telegram, monkeypatch):
    state, rev = telegram
    entered, release = threading.Event(), threading.Event()
    requests, handled = [], []
    def response(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={'ok': True, 'result': []})
        entered.set()
        assert release.wait(4)
        return httpx.Response(200, json={'ok': True, 'result': [{'update_id': 7, 'message': {
            'from': {'id': 123, 'is_bot': False}, 'chat': {'id': 123, 'type': 'private'},
            'date': int(time.time()), 'text': '/delegate never-execute-after-stop'}}]})
    client = httpx.Client(transport=httpx.MockTransport(response))
    monkeypatch.setattr('nexus.telegram.bot.Bot', lambda studio, **kwargs: Bot(studio, client=client, **kwargs))
    monkeypatch.setattr(Bot, 'handle', lambda self, message: handled.append(message))
    try:
        start(state, rev)
        assert entered.wait(3)
        info = dispatch(state)['telegram_runtime']
        assert info['alive'] and info['phase'] == 'polling' and info['last_poll_at']
        assert TOKEN not in json.dumps(info) and PIN not in json.dumps(info)
        with pytest.raises(ValueError):
            start(state, rev)
        stopped = dispatch(state, {'operation': 'telegram-stop', 'confirmation': 'STOP TELEGRAM POLLING'})
        assert stopped['alive'] and stopped['phase'] == 'stopping'
        release.set()
        runtime(state).thread.join(3)
        info = runtime(state).snapshot()
        assert not info['alive'] and info['phase'] == 'stopped'
        assert not handled and len(requests) == 2
        assert not state.workbench.studio.items('jarvis-job')
    finally:
        release.set()
        runtime(state).stop(timeout=2)
        client.close()


def test_existing_cli_lock_prevents_duplicate_gui_poller(telegram, monkeypatch):
    state, rev = telegram
    requests = []
    client = httpx.Client(transport=httpx.MockTransport(lambda req: requests.append(req) or httpx.Response(200, json={})))
    monkeypatch.setattr('nexus.telegram.bot.Bot', lambda studio, **kw: Bot(studio, client=client, **kw))
    try:
        with FileLock(str(Queue(state.workbench.studio).directory() / 'telegram.lock'), timeout=0):
            start(state, rev)
            runtime(state).thread.join(3)
            info = runtime(state).snapshot()
            assert not info['alive'] and info['phase'] == 'failed'
            assert not requests and TOKEN not in json.dumps(info)
    finally:
        client.close()


def test_missing_environment_credentials_fail_without_exposing_values(telegram, monkeypatch):
    state, rev = telegram
    monkeypatch.delenv('TELEGRAM_APPROVAL_PIN')
    start(state, rev)
    runtime(state).thread.join(3)
    info = runtime(state).snapshot()
    assert not info['alive'] and info['phase'] == 'failed'
    assert TOKEN not in json.dumps(info)

    def fail_start(self):
        raise RuntimeError(TOKEN)
    monkeypatch.setattr(threading.Thread, 'start', fail_start)
    with pytest.raises(ValueError):
        start(state, rev)
    info = runtime(state).snapshot()
    assert info['phase'] == 'failed' and not info['alive']
    assert TOKEN not in json.dumps(info)
    runtime(state).stop(timeout=2)  # A refused thread must not break GUI shutdown.


def test_one_time_context_is_revision_and_consent_guarded_and_uses_shared_studio(repo):
    state = ServerState(repo, quiet=True)
    rev = enabled(state, 'context', {'enabled': True, 'signals': {'project': True}})
    payload = {'operation': 'context-collect', 'revision': rev, 'confirmation': 'COLLECT LOCAL CONTEXT'}
    for patch in [{'confirmation': 'wrong'}, {'revision': 'stale'}]:
        with pytest.raises(ValueError):
            dispatch(state, {**payload, **patch})
    data = dispatch(state, payload)
    assert data['signals'] == {'project': repo.name}
    assert 'untrusted' in data['trust']
    state.workbench.studio.put('context-signal', {'kind': 'project'}, 'not-this-context')
    with pytest.raises(ValueError):
        dispatch(state, {'operation': 'context-clear', 'confirmation': 'wrong'})
    assert dispatch(state, {'operation': 'context-clear', 'confirmation': 'CLEAR LOCAL CONTEXT'})['cleared']
    assert state.workbench.studio.find('context-signal', 'not-this-context')


def test_default_off_context_and_demo_never_collect(repo, monkeypatch):
    from nexus.context.core import Context
    calls = []
    monkeypatch.setattr(Context, 'collect', lambda self: calls.append(True))
    for demo in (False, True):
        state = ServerState(repo, quiet=True, demo=demo)
        row = dispatch(state)['policies']['context']
        with pytest.raises(ValueError):
            dispatch(state, {'operation': 'context-collect', 'confirmation': 'COLLECT LOCAL CONTEXT', 'revision': row['revision']})
    assert not calls


@pytest.mark.parametrize('kind,operation,confirmation', [
    ('context', 'context-clear', 'CLEAR LOCAL CONTEXT'),
    ('telegram', 'telegram-stop', 'STOP TELEGRAM POLLING')])
def test_malformed_policy_does_not_block_safe_stop_or_reference_clear(repo, kind, operation, confirmation):
    state = ServerState(repo, quiet=True)
    (repo / (kind + '.yaml')).write_text('private: [never-echo-this-secret\n')
    assert 'error' in dispatch(state)['policies'][kind]
    result = dispatch(state, {'operation': operation, 'confirmation': confirmation})
    assert 'never-echo-this-secret' not in json.dumps(result)
    assert result.get('cleared') or result.get('phase') == 'stopped'
