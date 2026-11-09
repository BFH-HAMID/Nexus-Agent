"""Opt-in Telegram voice drafts and one-use PIN-reviewed outgoing voice notes.

Incoming transcripts are never dispatched as commands or approvals. Audio stays
in bounded temporary memory/files for local STT/TTS and is not retained. PyAV and
Whisper/Piper are lazy optional dependencies; no OS media command is constructed.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path

import httpx


def policy(value):
    defaults = {'enabled': False, 'send_enabled': False, 'max_seconds': 30, 'max_bytes': 2_000_000}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError('Unknown Telegram voice policy')
    result = {**defaults, **value}
    if any(type(result[k]) is not bool for k in ('enabled', 'send_enabled')):
        raise ValueError('Telegram voice flags must be booleans')
    for key, low, high in (('max_seconds', 1, 60), ('max_bytes', 1000, 4_000_000)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Invalid Telegram audio bound')
    return result


def convert(data, seconds, *, outgoing=False):
    try:
        import av
    except ImportError:
        raise ValueError('Telegram audio needs the optional nexus-agent[telegram-voice] extra') from None
    output = io.BytesIO()
    rate = 48000 if outgoing else 16000
    sample_count = 0
    deadline = time.monotonic() + 20
    # Never auto-detect a playlist/container capable of fetching external URLs.
    with av.open(io.BytesIO(data), mode='r', format='wav' if outgoing else 'ogg') as source:
        resampler = av.AudioResampler(format='s16', layout='mono', rate=rate)
        frames = []
        for frame in source.decode(audio=0):
            if time.monotonic() >= deadline:
                raise ValueError('Audio decode deadline exceeded')
            for converted in resampler.resample(frame):
                sample_count += converted.samples
                if sample_count > rate * seconds:
                    raise ValueError('Decoded audio duration exceeds policy')
                frames.append(converted)
        for converted in resampler.resample(None):
            sample_count += converted.samples
            if sample_count > rate * seconds:
                raise ValueError('Decoded audio duration exceeds policy')
            frames.append(converted)
        if not frames:
            raise ValueError('No audio frames')
        if outgoing:
            with av.open(output, mode='w', format='ogg') as target:
                stream = target.add_stream('libopus', rate=rate)
                stream.layout = 'mono'
                for frame in frames:
                    frame.pts = None
                    for packet in stream.encode(frame):
                        target.mux(packet)
                for packet in stream.encode(None):
                    target.mux(packet)
        else:
            with wave.open(output, 'wb') as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(rate)
                for frame in frames:
                    target.writeframes(bytes(frame.planes[0])[:frame.samples * 2])
    return output.getvalue()


class TelegramVoice:
    def __init__(self, bot):
        self.bot = bot
        self.pending = {}
        self.lock = threading.RLock()

    def close(self):
        with self.lock:
            for row in self.pending.values():
                row['timer'].cancel()
            self.pending.clear()

    def guard(self, user, expected=None, *, sending=False):
        cfg = self.bot.check_active()
        if (user not in cfg['user_ids'] or not cfg['voice']['enabled']
                or sending and not cfg['voice']['send_enabled'] or expected is not None and cfg != expected):
            raise ValueError('Telegram voice authority changed or disabled')
        return cfg

    def receive(self, user, message):
        cfg = self.guard(user)
        item = message.get('voice')
        limits = cfg['voice']
        if (not isinstance(item, dict) or type(item.get('duration')) is not int
                or not 0 < item['duration'] <= limits['max_seconds']
                or type(item.get('file_size')) is not int or not 0 < item['file_size'] <= limits['max_bytes']
                or not isinstance(item.get('file_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', item['file_id'])):
            raise ValueError('Bounded Telegram voice note required')
        info = self.bot.api('getFile', {'file_id': item['file_id']})
        if not isinstance(info, dict) or info.get('file_id') != item['file_id']:
            raise ValueError('Telegram audio ID mismatch')
        path = info.get('file_path')
        if (not isinstance(path, str) or not re.fullmatch(r'voice/[A-Za-z0-9_-]{1,200}\.(?:oga|ogg)', path)
                or info.get('file_size') != item['file_size']):
            raise ValueError('Unsafe Telegram voice path/size')
        try:
            self.guard(user, cfg)
            url = 'https://api.telegram.org/file/bot' + self.bot.token + '/' + path
            data = bytearray()
            deadline = time.monotonic() + 20
            with self.bot.client.stream('GET', url, follow_redirects=False, timeout=20,
                                        headers={'Accept-Encoding': 'identity'}) as response:
                if response.status_code != 200 or response.headers.get('content-encoding', '') not in ('', 'identity'):
                    raise ValueError('Telegram audio download refused')
                for chunk in response.iter_bytes():
                    self.guard(user, cfg)
                    if time.monotonic() > deadline or len(data) + len(chunk) > item['file_size']:
                        raise ValueError('Audio download budget exceeded')
                    data.extend(chunk)
            if len(data) != item['file_size']:
                raise ValueError('Audio size mismatch')
        except httpx.HTTPError:
            raise ValueError('Telegram audio transport unavailable; details withheld') from None
        if not os.environ.get('NEXUS_WHISPER_MODEL'):
            raise ValueError('Set NEXUS_WHISPER_MODEL to an existing local model directory')
        normalized = convert(bytes(data), limits['max_seconds'])
        self.guard(user, cfg)
        from ..assistant.voice import FasterWhisperSTT
        with tempfile.TemporaryDirectory(prefix='nexus-telegram-voice-') as folder:
            source = Path(folder) / 'note.wav'
            source.write_bytes(normalized)
            transcript = FasterWhisperSTT(os.environ.get('NEXUS_WHISPER_MODEL', ''), device='cpu', compute_type='int8').transcribe(source)
        self.guard(user, cfg)
        from ..hud.presence import Presence
        shared = Presence(self.bot.root)
        if shared.enabled('voice_enabled'):
            shared.voice('idle', source='telegram', transcript=transcript.text)
            return 'Voice transcribed locally. Review the draft in the authenticated HUD. No task, send or approval was executed.'
        return 'Voice transcribed locally, then discarded: shared HUD voice is OFF. No transcript uploaded; no task or approval executed.'

    def prepare(self, user, text):
        cfg = self.guard(user, sending=True)
        if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 1200
                or self.bot.clean(text) != text or any(ord(c) < 32 for c in text)):
            raise ValueError('Bounded credential-free speech text required')
        ident = uuid.uuid4().hex
        binding = hashlib.sha256(json.dumps({'user': user, 'text': text, 'policy': cfg}, sort_keys=True).encode()).hexdigest()
        with self.lock:
            if len(self.pending) >= 10:
                raise ValueError('Voice review quota reached')
            def expire():
                with self.lock:
                    self.pending.pop(ident, None)
            timer = threading.Timer(cfg['challenge_seconds'], expire)
            timer.daemon = True
            self.pending[ident] = {'text': text, 'user': user, 'binding': binding, 'cfg': cfg, 'timer': timer}
            try:
                challenge = self.bot.challenge(user, 'voice_send', {'id': ident, 'args_digest': binding})
            except BaseException:
                self.pending.pop(ident, None)
                raise
            timer.start()
        return f'Send this exact locally synthesized voice note to private chat {user}:\n{text}\n{challenge}'

    def confirm(self, user, challenge):
        with self.lock:
            row = self.pending.pop(challenge['target'], None)
        if row is None or row['user'] != user or row['binding'] != challenge['binding']:
            raise ValueError('Voice review expired/restarted; request a new review')
        row['timer'].cancel()
        self.guard(user, row['cfg'], sending=True)
        from ..assistant.service import tts_for
        clip = tts_for(self.bot.studio.wb.state, 'piper').synthesize(row['text'])
        audio = convert(clip.data, row['cfg']['voice']['max_seconds'], outgoing=True)
        if not audio or len(audio) > row['cfg']['voice']['max_bytes']:
            raise ValueError('Synthesized voice exceeds upload bound')
        self.guard(user, row['cfg'], sending=True)
        try:
            with self.bot.client.stream('POST', 'https://api.telegram.org/bot' + self.bot.token + '/sendVoice',
                                        data={'chat_id': str(user)}, files={'voice': ('reply.ogg', audio, 'audio/ogg')},
                                        timeout=20, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise ValueError('Telegram voice send refused')
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > 64_000:
                        raise ValueError('Telegram send response exceeds bound')
                    body.extend(chunk)
                if json.loads(body).get('ok') is not True:
                    raise ValueError('Telegram voice send refused')
        except (httpx.HTTPError, ValueError):
            raise ValueError('Voice send outcome unavailable; inspect before retry. Fresh review required.') from None
        return {'status': 'submitted', 'played': False}
