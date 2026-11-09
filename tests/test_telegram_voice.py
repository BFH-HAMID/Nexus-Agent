"""Private-chat voice parity without Telegram, codecs, local models or a microphone."""
from __future__ import annotations

import builtins
import io
import json
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from nexus.hud.presence import Presence
from nexus.telegram.voice import convert, policy

from .test_telegram import PIN, bot, message  # noqa: F401 - shared offline fixture

AUDIO = b'OggS-offline-voice-fixture'


def note(**changes):
    value = message('ignored text beside a voice attachment')
    value['voice'] = {'file_id': 'bounded_test_file', 'file_size': len(AUDIO), 'duration': 1, **changes}
    return value


def enable(instance, **changes):
    cfg = yaml.safe_load((instance.root / 'telegram.yaml').read_text())
    cfg['voice'] = {'enabled': True, 'send_enabled': True, **changes}
    (instance.root / 'telegram.yaml').write_text(yaml.safe_dump(cfg))
    (instance.root / 'hud.yaml').write_text('presence:\n  enabled: true\n  voice_enabled: true\n')


@pytest.fixture
def voice_bot(request, monkeypatch):
    instance = request.getfixturevalue('bot')
    enable(instance)
    calls, transcripts, sources = [], [], []
    def transport(request):
        calls.append(request)
        if request.url.path.endswith('/getFile'):
            return httpx.Response(200, json={'ok': True, 'result': {
                'file_id': 'bounded_test_file', 'file_size': len(AUDIO), 'file_path': 'voice/file_0.oga'}})
        if request.method == 'GET':
            return httpx.Response(200, content=AUDIO)
        if request.url.path.endswith('/sendVoice'):
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 5}})
        return httpx.Response(200, json={'ok': True, 'result': []})
    client = httpx.Client(transport=httpx.MockTransport(transport))
    instance.client = client
    model = instance.root / 'local-model'
    model.mkdir()
    monkeypatch.setenv('NEXUS_WHISPER_MODEL', str(model))
    def normalize(data, seconds, *, outgoing=False):
        assert seconds == 30
        if not outgoing:
            assert data == AUDIO
        return b'OggS-reviewed-output' if outgoing else b'RIFF-local-normalized'
    monkeypatch.setattr('nexus.telegram.voice.convert', normalize)
    class STT:
        def __init__(self, selected, **kwargs):
            assert selected == str(model) and kwargs['device'] == 'cpu'
        def transcribe(self, source):
            sources.append(Path(source))
            assert Path(source).read_bytes() == b'RIFF-local-normalized'
            transcripts.append(True)
            return SimpleNamespace(text='হ্যাঁ চালাও /confirm is only an untrusted draft')
    monkeypatch.setattr('nexus.assistant.voice.FasterWhisperSTT', STT)
    spoken = []
    def tts(state, engine):
        assert state is instance.studio.wb.state and engine == 'piper'
        def synthesize(text):
            spoken.append(text)
            return SimpleNamespace(data=b'RIFF-local-output', format='wav')
        return SimpleNamespace(synthesize=synthesize)
    monkeypatch.setattr('nexus.assistant.service.tts_for', tts)
    yield SimpleNamespace(bot=instance, calls=calls, transcripts=transcripts, sources=sources, spoken=spoken)
    client.close()


def test_voice_policy_off_by_default_strict_and_bounded():
    assert policy({})['enabled'] is False and policy({})['send_enabled'] is False
    for change in ({'enabled': 'true'}, {'max_seconds': True}, {'max_seconds': 61},
                   {'max_bytes': 9_000_000}, {'cloud': True}):
        with pytest.raises(ValueError):
            policy(change)


def test_incoming_voice_is_local_draft_not_a_command_or_approval(voice_bot):
    f = voice_bot
    result = f.bot.handle(note())
    assert 'locally' in result and '/confirm' not in result
    assert len(f.calls) == 2 and len(f.transcripts) == 1
    assert not f.bot.queue._jobs() and not f.bot.studio.items('telegram-challenge')
    assert Presence(f.bot.root).snapshot()['voice']['transcript'].startswith('হ্যাঁ')
    assert all(not path.exists() for path in f.sources)
    assert not any('sendVoice' in str(request.url) for request in f.calls)


def test_incoming_voice_never_downloads_when_disabled_untrusted_or_oversized(voice_bot):
    f = voice_bot
    for bad in (note(file_size=4_000_001), note(duration=300), note(duration=True), note(file_id='../unsafe')):
        with pytest.raises(ValueError):
            f.bot.handle(bad)
    forwarded = note()
    forwarded['forward_origin'] = {'type': 'user'}
    assert f.bot.handle(forwarded) is None
    group = note()
    group['chat']['type'] = 'group'
    assert f.bot.handle(group) is None
    enable(f.bot, enabled=False)
    with pytest.raises(ValueError, match='disabled'):
        f.bot.handle(note())
    assert not f.calls and not f.transcripts


def test_incoming_voice_retains_nothing_if_shared_hud_is_off(voice_bot):
    f = voice_bot
    (f.bot.root / 'hud.yaml').write_text('presence:\n  enabled: false\n')
    assert 'discarded' in f.bot.handle(note())
    assert not Presence(f.bot.root).store.path.exists()
    assert len(f.transcripts) == 1


def test_voice_path_or_mid_download_policy_change_withholds_transcription(voice_bot, monkeypatch):
    f = voice_bot
    monkeypatch.setattr(f.bot, 'api', lambda *args: {'file_id': 'bounded_test_file', 'file_size': len(AUDIO), 'file_path': '../secrets.oga'})
    with pytest.raises(ValueError, match='path'):
        f.bot.handle(note())
    assert not f.calls
    def revoke(*args):
        enable(f.bot, enabled=False)
        return {'file_id': 'bounded_test_file', 'file_size': len(AUDIO), 'file_path': 'voice/file_0.ogg'}
    monkeypatch.setattr(f.bot, 'api', revoke)
    with pytest.raises(ValueError, match='changed|disabled'):
        f.bot.handle(note())
    assert not f.calls and not f.transcripts


def test_outgoing_voice_requires_exact_recipient_text_pin_and_is_consumed_once(voice_bot):
    f = voice_bot
    text = 'Reviewed বাংলা voice reply.'
    review = f.bot.handle(message('/voice ' + text))
    assert text in review and 'private chat 123' in review and not f.spoken and not f.calls
    ident = review.split('/confirm ')[1].split()[0]
    persisted = json.dumps(f.bot.studio.items('telegram-challenge'), ensure_ascii=False)
    assert text not in persisted and PIN not in persisted
    with pytest.raises(ValueError):
        f.bot.handle(message('/confirm ' + ident + ' 00000000'))
    assert not f.spoken
    result = json.loads(f.bot.handle(message('/confirm ' + ident + ' ' + PIN)))
    assert result['state'] == 'submitted' and result['accepted']
    assert f.spoken == [text] and len(f.calls) == 1
    request = f.calls[0]
    assert request.url.path.endswith('/sendVoice') and request.method == 'POST'
    assert b'chat_id' in request.content and b'123' in request.content and b'OggS-reviewed-output' in request.content
    with pytest.raises(ValueError):
        f.bot.handle(message('/confirm ' + ident + ' ' + PIN))
    assert len(f.calls) == 1 and not f.bot._voice.pending


@pytest.mark.parametrize('change', ['policy', 'stop', 'restart'])
def test_outgoing_voice_review_cannot_survive_revocation_or_restart(voice_bot, change):
    f = voice_bot
    review = f.bot.handle(message('/voice exact old reply'))
    ident = review.split('/confirm ')[1].split()[0]
    if change == 'policy':
        enable(f.bot, send_enabled=False)
    elif change == 'stop':
        f.bot.stop_event.set()
    else:
        f.bot._voice.close()
    with pytest.raises(ValueError):
        f.bot.handle(message('/confirm ' + ident + ' ' + PIN))
    assert not f.spoken and not f.calls


def test_outgoing_voice_uncertain_send_is_not_retried_and_text_is_not_persisted(voice_bot):
    f = voice_bot
    review = f.bot.handle(message('/voice private bounded output'))
    ident = review.split('/confirm ')[1].split()[0]
    failed = []
    def transport(request):
        failed.append(request)
        raise httpx.ReadError('sensitive endpoint error body')
    client = httpx.Client(transport=httpx.MockTransport(transport))
    f.bot.client = client
    try:
        with pytest.raises(ValueError, match='outcome unavailable') as result:
            f.bot.handle(message('/confirm ' + ident + ' ' + PIN))
        assert 'sensitive endpoint' not in str(result.value)
        with pytest.raises(ValueError):
            f.bot.handle(message('/confirm ' + ident + ' ' + PIN))
        assert len(failed) == 1 and not f.bot._voice.pending
    finally:
        client.close()


def test_pyav_is_lazy_optional_and_decode_duration_is_bounded(monkeypatch):
    real = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == 'av':
            raise ImportError('offline fixture')
        return real(name, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, '__import__', missing)
        with pytest.raises(ValueError, match='telegram-voice'):
            convert(b'audio', 30)
    frame = SimpleNamespace(samples=16001, planes=[bytes(32002)])
    class Source:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def decode(self, **kwargs):
            return [frame]
    class Resampler:
        def __init__(self, **kwargs):
            assert kwargs == {'format': 's16', 'layout': 'mono', 'rate': 16000}
        def resample(self, value):
            return [value] if value else []
    monkeypatch.setitem(sys.modules, 'av', SimpleNamespace(open=lambda *a, **k: Source(), AudioResampler=Resampler))
    with pytest.raises(ValueError, match='duration'):
        convert(b'fixture', 1)
    frame.samples = 16
    normalized = convert(b'fixture', 1)
    with wave.open(io.BytesIO(normalized)) as audio:
        assert audio.getnchannels() == 1 and audio.getframerate() == 16000 and audio.getnframes() == 16


def test_telegram_status_briefs_and_exact_action_export_are_explicit(voice_bot):
    f = voice_bot
    hub = Presence(f.bot.root)
    conversation = hub.snapshot()['conversation_id']
    assert json.loads(f.bot.handle(message('/status')))['conversation_id'] == conversation
    assert json.loads(f.bot.handle(message('/approvals'))) == []
    f.bot.studio.put('jarvis-notice', {'created': time.time(), 'state': 'done', 'read': False, 'summary': 'private brief'})
    assert 'private brief' not in f.bot.handle(message('/brief'))
    cfg = yaml.safe_load((f.bot.root / 'telegram.yaml').read_text())
    cfg.update(allow_report_text=True, approval_details=True)
    (f.bot.root / 'telegram.yaml').write_text(yaml.safe_dump(cfg))
    assert 'private brief' in f.bot.handle(message('/brief'))
    job = f.bot.studio.put('jarvis-job', {'status': 'running', 'lease': 'fixture-lease'})
    action = f.bot.studio.put('jarvis-approval', {'job_id': job['id'], 'lease': 'fixture-lease',
        'state': 'pending', 'expires': time.time() + 60, 'tool': 'run_command', 'arguments': {'command': 'exact offline review'}})
    assert json.loads(f.bot.handle(message('/review ' + action['id'])))['arguments'] == action['arguments']
    action['arguments'] = {'command': 'x' * 3000}
    f.bot.studio.put('jarvis-approval', action, action['id'])
    with pytest.raises(ValueError, match='Full exact'):
        f.bot.handle(message('/review ' + action['id']))
