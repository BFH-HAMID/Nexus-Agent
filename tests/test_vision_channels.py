"""No hardware/network acceptance: authenticated UI, literal voice, private Telegram."""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from nexus import audit
from nexus.cli import main
from nexus.hud.app import create_app
from nexus.server import ServerState, make_handler
from nexus.telegram.bot import Bot
from nexus.tools import ApprovalGate
from nexus.vision.api import VisionAPI
from nexus.vision.policy import VisionError
from nexus.vision.service import VisionService
from nexus.vision.state import hard_off, resume

from .vision_helpers import HTTP_CLIENT, enable, fake_faces, fake_vlm, gate, image_bytes


def payload(**changes):
    return {'name': 'chart.png', 'file_base64': base64.b64encode(image_bytes()).decode(),
            'question': 'Explain this chart.', **changes}


@pytest.fixture
def state(repo):
    state = ServerState(repo, token='test-only-vision-server-token', auto_yes=True, quiet=True)
    yield state
    state.mission.close()
    if getattr(state, 'assistant', None):
        state.assistant.stop_worker()


def test_hud_channel_optin_ephemeral_files_and_legacy_api_delegate(state, monkeypatch):
    from nexus.assistant.api import post
    calls, _ = fake_vlm(monkeypatch)
    enable(state.workspace, files={'enabled': True}, vlm={'enabled': True})
    with pytest.raises(VisionError, match='channel'):
        post(state, 'vision/ask', payload(), 'test-client')
    enable(state.workspace, files={'hud': True})
    answer = post(state, 'vision/ask', payload(), 'test-client')
    assert answer['references'] and len(calls) == 1 and answer['image_retained'] is False
    assert not (state.workspace / '.nexus/assistant/uploads').exists()
    assert not (state.workspace / '.nexus/assistant/settings.json').exists()


def test_cloud_api_exact_one_use_review_binds_bytes_question_client_and_policy(repo, monkeypatch):
    enable(repo, files={'enabled': True, 'hud': True},
           vlm={'enabled': True, 'provider': 'gemini', 'allow_cloud': True, 'model': 'gemini-test'})
    monkeypatch.setenv('GEMINI_API_KEY', 'test-not-a-real-key')
    fake_faces(monkeypatch)
    calls, _ = fake_vlm(monkeypatch)
    api = VisionAPI(repo)
    body = payload(cloud=True, no_people=True)
    with pytest.raises(VisionError):
        api.post('ask', body, 'browser-1')
    assert not calls
    for change, client in [({'question': 'Different question'}, 'browser-1'),
                           ({'name': 'different.png'}, 'browser-1'), ({}, 'browser-2')]:
        review = api.post('preview', body, 'browser-1')
        submitted = {**body, 'challenge': review['challenge'], 'confirmation': review['challenge']['confirmation']}
        with pytest.raises(VisionError):
            api.post('ask', {**submitted, **change}, client)
        with pytest.raises(VisionError):
            api.post('ask', submitted, 'browser-1')
    review = api.post('preview', body, 'browser-1')
    submitted = {**body, 'challenge': review['challenge'], 'confirmation': review['challenge']['confirmation']}
    answer = api.post('ask', submitted, 'browser-1')
    assert answer['uploaded'] and len(calls) == 1
    with pytest.raises(VisionError):
        api.post('ask', submitted, 'browser-1')
    review = api.post('preview', body, 'browser-1')
    enable(repo, vlm={'model': 'gemini-other'})
    with pytest.raises(VisionError):
        api.post('ask', {**body, 'challenge': review['challenge'], 'confirmation': review['challenge']['confirmation']}, 'browser-1')
    assert len(calls) == 1
    assert not api.reviews._pending


def test_off_resume_invalidates_pending_cloud_reviews_and_off_ignores_busy_slots(repo, monkeypatch):
    enable(repo, files={'enabled': True, 'hud': True}, vlm={'enabled': True, 'provider': 'gemini', 'allow_cloud': True})
    fake_faces(monkeypatch)
    calls, _ = fake_vlm(monkeypatch)
    api = VisionAPI(repo)
    body = payload(cloud=True, no_people=True)
    review = api.post('preview', body, 'client')
    api.slots.acquire()
    api.slots.acquire()
    try:
        assert api.post('off', {})['stopped']
        with pytest.raises(VisionError, match='busy'):
            api.post('ask', body)
    finally:
        api.slots.release()
        api.slots.release()
    resume(repo, gate())
    with pytest.raises(VisionError):
        api.post('ask', {**body, 'challenge': review['challenge'], 'confirmation': review['challenge']['confirmation']}, 'client')
    assert not calls


def test_http_requires_actual_auth_same_origin_and_supports_bounded_upload(state, monkeypatch):
    enable(state.workspace, files={'enabled': True, 'hud': True}, vlm={'enabled': True})
    calls, _ = fake_vlm(monkeypatch)
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(state))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with HTTP_CLIENT(base_url=f'http://127.0.0.1:{httpd.server_port}', trust_env=False) as client:
            assert client.get('/api/vision/status').status_code == 401
            headers = {'X-Nexus-Token': state.token}
            assert client.get('/api/vision/status', headers=headers).json()['camera']['active'] is False
            assert client.post('/api/vision/ask', headers={**headers, 'Origin': 'https://evil.invalid'}, json=payload()).status_code == 403
            response = client.post('/api/vision/ask', headers=headers, json=payload())
            assert response.status_code == 200 and response.json()['references']
            assert len(calls) == 1
            state.token = ''
            for path in ('/api/vision/ask', '/api/assistant/vision/ask'):
                assert client.post(path, json=payload()).status_code == 401
            assert client.get('/api/vision/status').status_code == 401
            assert len(calls) == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_legacy_hud_auth_upload_off_and_real_snapshot(state, monkeypatch):
    enable(state.workspace, files={'enabled': True, 'hud': True}, vlm={'enabled': True})
    fake_vlm(monkeypatch)
    app = create_app(state.workbench.studio, 'x' * 40, allowed_hosts=['testserver'])
    with TestClient(app) as client:
        assert client.post('/api/vision/off', json={}).status_code == 401
        headers = {'Authorization': 'Bearer ' + 'x' * 40}
        assert client.post('/api/vision/ask', json=payload(), headers=headers).json()['references']
        assert client.get('/api/state', headers=headers).json()['vision']['screen']['state'] == 'idle'
        (state.workspace / 'vision.yaml').write_text('enabled: [invalid')
        assert client.post('/api/vision/off', json={}, headers=headers).json()['stopped']
        assert client.get('/api/vision/status', headers=headers).json()['enabled'] is None


@pytest.mark.parametrize('changes', [{'file_base64': '*invalid*'}, {'cloud': 'yes'}, {'agent': 'arbitrary-cloud-model'},
    {'file_base64': ''}, {'unexpected_tool': 'run_command'}, {'question': ''}, {'name': 'file.exe'}])
def test_hud_rejects_invalid_input_without_provider_calls(repo, monkeypatch, changes):
    enable(repo, files={'enabled': True, 'hud': True}, vlm={'enabled': True})
    calls, _ = fake_vlm(monkeypatch)
    with pytest.raises(VisionError):
        VisionAPI(repo).post('ask', payload(**changes))
    assert not calls


def test_literal_voice_look_returns_citations_without_running_agent(repo, monkeypatch):
    from nexus.voice.pipeline import build_agent_handler
    enable(repo, files={'enabled': True, 'voice': True}, vlm={'enabled': True})
    (repo / 'chart.png').write_bytes(image_bytes())
    calls, _ = fake_vlm(monkeypatch)
    class Agent:
        def __init__(self, *args, **kwargs):
            self.gate = ApprovalGate(callback=kwargs['approval_callback'])
        def run(self, *args, **kw):
            pytest.fail('vision evidence must not enter the agent tool loop')
    monkeypatch.setattr('nexus.agent.NexusAgent', Agent)
    handler = build_agent_handler(repo, stream=True, read_only=True, auto_yes=True)
    answer = handler(SimpleNamespace(text='look at this chart.png', language='en'))
    assert 'page 1' in answer and 'region' in answer and len(calls) == 1
    clarify = handler(SimpleNamespace(text='look at this', language='en'))
    assert 'Which image or PDF' in clarify and len(calls) == 1
    enable(repo, files={'voice': False})
    with pytest.raises(VisionError):
        handler(SimpleNamespace(text='look at this chart.png', language='en'))


def test_voice_screen_preserves_readonly_and_speaker_gate_and_local_tts(repo, monkeypatch):
    from nexus.voice.config import VoiceError
    from nexus.voice.persona import load_persona_config
    from nexus.voice.pipeline import build_agent_handler
    enable(repo, files={'voice': True}, screen={'enabled': True}, vlm={'enabled': True})
    (repo / 'context.yaml').write_text('enabled: true\nscreenshots: true\n')
    monkeypatch.setattr('nexus.context.native.foreground', lambda: {'app': 'editor', 'title': 'safe',
        'bounds': {'left': 0, 'top': 0, 'width': 20, 'height': 20}})
    class Agent:
        def __init__(self, *args, **kwargs):
            self.gate = ApprovalGate(callback=kwargs['approval_callback'])
        def run(self, *args, **kw):
            pytest.fail('no agent run')
    monkeypatch.setattr('nexus.agent.NexusAgent', Agent)
    transcript = SimpleNamespace(text='what is on my screen?', language='en')
    with pytest.raises(ValueError, match='consent'):
        build_agent_handler(repo, read_only=True, approval_callback=lambda *a: pytest.fail('no read-only approval'))(transcript)
    pipeline = SimpleNamespace(persona=load_persona_config(repo), llm_cancel_event=threading.Event(),
        make_approval_gate_check=lambda: (lambda *a: (False, 'unverified speaker')),
        config=SimpleNamespace(tts=SimpleNamespace(provider='piper'), conversation=SimpleNamespace(spoken_approvals=False)))
    with pytest.raises(ValueError, match='consent'):
        build_agent_handler(repo, pipeline=pipeline)(transcript)
    pipeline.config.tts.provider = 'edge'
    with pytest.raises(VoiceError, match='local'):
        build_agent_handler(repo, pipeline=pipeline)(transcript)
    assert not audit.tail(repo, action='vision.capture')


TOKEN = '123456789:' + 't' * 30
PIN = '86972413'


@pytest.fixture
def bot(state, monkeypatch):
    root = state.workspace
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', TOKEN)
    monkeypatch.setenv('TELEGRAM_APPROVAL_PIN', PIN)
    (root / 'telegram.yaml').write_text(yaml.safe_dump({'enabled': True, 'user_ids': [123]}))
    calls = []
    data = image_bytes()
    def transport(request):
        calls.append(request)
        if request.url.path.endswith('/getFile'):
            return httpx.Response(200, json={'ok': True, 'result': {'file_id': 'file123', 'file_path': 'photos/file.jpg', 'file_size': len(data)}})
        if request.method == 'GET':
            return httpx.Response(200, content=data)
        return httpx.Response(200, json={'ok': True, 'result': []})
    client = HTTP_CLIENT(transport=httpx.MockTransport(transport), trust_env=False, follow_redirects=False)
    instance = Bot(state.workbench.studio, client=client)
    instance.test_calls = calls
    yield instance
    instance.close()
    client.close()


def msg(text=None, *, attachment=False, user=123):
    result = {'from': {'id': user, 'is_bot': False}, 'chat': {'id': user, 'type': 'private'}, 'date': int(time.time())}
    if attachment:
        result['photo'] = [{'file_id': 'file123', 'file_size': len(image_bytes())}]
        result['caption'] = text or '/look Explain this chart'
    else:
        result['text'] = text
    return result


def challenge_id(prompt):
    return re.search(r'/confirm ([a-f0-9]+)', prompt).group(1)


def test_telegram_private_channel_and_policy_checked_before_any_download(bot):
    enable(bot.root, files={'enabled': True}, vlm={'enabled': True})
    with pytest.raises(VisionError, match='channel'):
        bot.handle(msg(attachment=True))
    enable(bot.root, files={'telegram': True})
    for message in (msg(attachment=True, user=999), {**msg(attachment=True), 'forward_origin': {'type': 'user'}},
                    {**msg(attachment=True), 'chat': {'id': 123, 'type': 'group'}},
                    {**msg(attachment=True), 'date': int(time.time()) - 121}):
        assert bot.handle(message) is None
    assert not bot.test_calls


def test_telegram_local_attachment_answer_requires_fresh_pin_bound_send(bot, monkeypatch):
    enable(bot.root, files={'enabled': True, 'telegram': True}, vlm={'enabled': True})
    model_calls, _ = fake_vlm(monkeypatch)
    prompt = bot.handle(msg(attachment=True))
    identifier = challenge_id(prompt)
    assert len(model_calls) == 1
    assert not any(r.url.path.endswith('/sendMessage') for r in bot.test_calls)
    row = bot.studio.find('telegram-challenge', identifier)
    assert row['kind'] == 'vision_send'
    assert 'The blue bar' not in json.dumps(row)
    with pytest.raises(ValueError, match='PIN'):
        bot.handle(msg('/confirm ' + identifier + ' 00000000'))
    bot.handle(msg('/confirm ' + identifier + ' ' + PIN))
    sends = [r for r in bot.test_calls if r.url.path.endswith('/sendMessage')]
    assert len(sends) == 1
    text = json.loads(sends[0].content)['text']
    assert 'Untrusted vision answer' in text and 'References:' in text
    with pytest.raises(ValueError):
        bot.handle(msg('/confirm ' + identifier + ' ' + PIN))
    assert not bot._vision.pending and not list(bot.root.glob('*.jpg'))


def test_telegram_cloud_upload_and_answer_send_are_two_separate_fresh_actions(bot, monkeypatch):
    enable(bot.root, files={'enabled': True, 'telegram': True},
           vlm={'enabled': True, 'provider': 'gemini', 'allow_cloud': True})
    monkeypatch.setenv('GEMINI_API_KEY', 'test-only-key')
    fake_faces(monkeypatch)
    calls, _ = fake_vlm(monkeypatch)
    preview = bot.handle(msg('/look --cloud --no-people Explain this chart', attachment=True))
    assert not calls
    followup = bot.handle(msg('/confirm ' + challenge_id(preview) + ' ' + PIN))
    assert len(calls) == 1
    assert not any(r.url.path.endswith('/sendMessage') for r in bot.test_calls)
    bot.handle(msg('/confirm ' + challenge_id(followup) + ' ' + PIN))
    sends = [r for r in bot.test_calls if r.url.path.endswith('/sendMessage')]
    assert len(sends) == 1 and 'file_base64' not in sends[0].content.decode()
    tools = [r['target'] for r in audit.tail(bot.root, action='permission.decision')]
    assert 'vision_cloud_upload' in tools and 'vision_telegram_send' in tools


def test_telegram_off_changed_answer_and_expiry_cancel_pending_sends(bot, monkeypatch):
    enable(bot.root, files={'enabled': True, 'telegram': True}, vlm={'enabled': True})
    fake_vlm(monkeypatch)
    preview = bot.handle(msg(attachment=True))
    pending = next(iter(bot._vision.pending.values()))
    pending['body']['text'] = 'Changed after review'
    with pytest.raises(VisionError, match='changed'):
        bot.handle(msg('/confirm ' + challenge_id(preview) + ' ' + PIN))
    preview = bot.handle(msg(attachment=True))
    bot._vision.close()  # same refusal as expiry/process restart
    with pytest.raises(VisionError, match='expired'):
        bot.handle(msg('/confirm ' + challenge_id(preview) + ' ' + PIN))
    preview = bot.handle(msg(attachment=True))
    bot.handle(msg('/vision-off'))
    with pytest.raises(VisionError):
        bot.handle(msg('/confirm ' + challenge_id(preview) + ' ' + PIN))
    assert not any(r.url.path.endswith('/sendMessage') for r in bot.test_calls)


def test_telegram_permissions_readonly_cannot_be_bypassed_by_pin(bot, monkeypatch):
    enable(bot.root, files={'enabled': True, 'telegram': True}, vlm={'enabled': True})
    fake_vlm(monkeypatch)
    preview = bot.handle(msg(attachment=True))
    (bot.root / 'permissions.yaml').write_text('default: READ_ONLY\n')
    with pytest.raises(VisionError, match='approval'):
        bot.handle(msg('/confirm ' + challenge_id(preview) + ' ' + PIN))
    assert not any(r.url.path.endswith('/sendMessage') for r in bot.test_calls)


@pytest.mark.parametrize('path', ['https://attacker.invalid/image.jpg', '../secret.jpg', 'photos/../../secret.jpg', 'photos/%2e%2e.jpg', '/photos/file.jpg'])
def test_telegram_download_origin_traversal_and_size_fail_closed(bot, monkeypatch, path):
    enable(bot.root, files={'enabled': True, 'telegram': True}, vlm={'enabled': True})
    fake_vlm(monkeypatch)
    monkeypatch.setattr(bot, 'api', lambda *args: {'file_id': 'file123', 'file_path': path, 'file_size': len(image_bytes())})
    with pytest.raises(VisionError):
        bot.handle(msg(attachment=True))
    assert not bot.test_calls


def test_cli_status_off_faces_commands_and_legacy_names_still_exist(repo, capsys):
    assert main(['vision', '-C', str(repo), 'status']) == 0
    assert json.loads(capsys.readouterr().out)['enabled'] is False
    assert main(['vision', '-C', str(repo), 'faces', 'list']) == 2
    assert 'OFF' in capsys.readouterr().out
    (repo / 'vision.yaml').write_text('enabled: [broken')
    assert main(['vision', '-C', str(repo), 'off']) == 0
    assert json.loads(capsys.readouterr().out)['stopped']
    with pytest.raises(SystemExit) as result:
        main(['assistant', 'vision', '--help'])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert 'enroll-face' in help_text and 'recognize-face' in help_text and 'forget-face' in help_text


def test_path_scope_symlinks_and_ambiguous_look_never_capture(repo, monkeypatch):
    enable(repo, files={'enabled': True, 'voice': True}, vlm={'enabled': True})
    (repo / 'chart.png').write_bytes(image_bytes())
    (repo / 'alias.png').symlink_to(repo / 'chart.png')
    calls, _ = fake_vlm(monkeypatch)
    svc = VisionService(repo)
    with pytest.raises(VisionError):
        svc.ask_path('alias.png', 'Explain')
    with pytest.raises(ValueError):
        svc.ask_path('../outside.png', 'Explain')
    assert svc.look('look at this')['clarification']
    hard_off(repo)
    with pytest.raises(VisionError):
        svc.ask_path('chart.png', 'Explain')
    assert not calls
