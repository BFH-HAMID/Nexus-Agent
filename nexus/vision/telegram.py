"""Private Telegram attachments: bounded ephemeral reads and separately approved sends.

Only the Bot's already-authenticated literal /look and /confirm paths call this.
No vision answer is interpreted as a bot command, task, tool, or approval.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import PurePosixPath

import httpx

from ..tools import ApprovalDecision, ApprovalGate
from .media import SUFFIXES
from .policy import VisionError, approve, digest, recheck, require
from .service import VisionService
from .state import check_running, control


class TelegramVision:
    def __init__(self, bot, *, service=None):
        self.bot, self.root = bot, bot.root
        self.service = service or VisionService(self.root)
        self.pending = {}
        self.lock = threading.RLock()

    def close(self):
        with self.lock:
            for row in self.pending.values():
                row['timer'].cancel()
            self.pending.clear()

    def _expire(self, ident):
        with self.lock:
            self.pending.pop(ident, None)

    def _guard(self, cfg, epoch, user, telegram_policy):
        current = self.bot.check_active()
        if current != telegram_policy or user not in current['user_ids']:
            raise VisionError('Telegram vision authority changed')
        check_running(self.root, epoch)
        recheck(self.root, cfg)

    @staticmethod
    def _binding(user, kind, body, cfg, epoch):
        return hashlib.sha256(json.dumps({'user': user, 'kind': kind, 'policy': digest(cfg),
            'epoch': epoch, 'body_sha256': hashlib.sha256(body.get('data', b'')).hexdigest(),
            'question': body.get('question'), 'text': body.get('text'), 'name': body.get('name'),
            'no_people': body.get('no_people')}, sort_keys=True).encode()).hexdigest()

    def _remember(self, user, kind, body, cfg, epoch, telegram_policy):
        self._guard(cfg, epoch, user, telegram_policy)
        ident = uuid.uuid4().hex
        binding = self._binding(user, kind, body, cfg, epoch)
        with self.lock:
            if (len(self.pending) >= 10 or
                    sum(len(r['body'].get('data', b'')) for r in self.pending.values()) + len(body.get('data', b'')) > 20_000_000):
                raise VisionError('Pending vision review memory limit reached; wait for expiry')
            timer = threading.Timer(telegram_policy['challenge_seconds'], self._expire, args=(ident,))
            timer.daemon = True
            self.pending[ident] = {'body': body, 'binding': binding, 'kind': kind, 'user': user,
                                   'cfg': cfg, 'epoch': epoch, 'telegram': telegram_policy, 'timer': timer}
            try:
                text = self.bot.challenge(user, kind, {'id': ident, 'args_digest': binding})
            except BaseException:
                self.pending.pop(ident, None)
                raise
            timer.start()
        warning = ('Upload this exact no-people attachment and question to the configured Gemini model (may incur charges).'
                   if kind == 'vision_cloud' else
                   'Send the untrusted text answer and page/region references to this private chat; no image will be sent.')
        return warning + '\n' + text

    def _attachment(self, message, limit):
        if 'document' in message and 'photo' in message:
            raise VisionError('Provide one attachment')
        if 'document' in message:
            item = message['document']
            name = item.get('file_name') if isinstance(item, dict) else None
        else:
            photos = message.get('photo')
            if not isinstance(photos, list) or not 1 <= len(photos) <= 10 or any(not isinstance(p, dict) for p in photos):
                raise VisionError('Bounded Telegram photo list required')
            item, name = photos[-1], 'telegram-photo.jpg'
        if (not isinstance(item, dict) or not isinstance(name, str) or not 1 <= len(name) <= 250
                or PurePosixPath(name).suffix.lower() not in SUFFIXES or '/' in name or '\\' in name
                or any(ord(c) < 32 for c in name)
                or type(item.get('file_size')) is not int or not 0 < item['file_size'] <= limit
                or not isinstance(item.get('file_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', item['file_id'])):
            raise VisionError('Only bounded named Telegram images, PDFs or text notes are supported')
        return name, item['file_id']

    def _download(self, identifier, limit, guard):
        guard()
        info = self.bot.api('getFile', {'file_id': identifier})
        if not isinstance(info, dict) or info.get('file_id') != identifier:
            raise VisionError('Telegram file metadata mismatch')
        path, size = info.get('file_path'), info.get('file_size')
        if (not isinstance(path, str) or not re.fullmatch(r'(?:photos|documents)/[A-Za-z0-9_/-]+\.[A-Za-z0-9]{1,6}', path)
                or '..' in path or '//' in path or len(path) > 500
                or type(size) is not int or not 0 < size <= limit):
            raise VisionError('Unsafe or oversized Telegram file reference')
        url = 'https://api.telegram.org/file/bot' + self.bot.token + '/' + path
        deadline = time.monotonic() + 30
        try:
            guard()
            with self.bot.client.stream('GET', url, follow_redirects=False, timeout=30, headers={'Accept-Encoding': 'identity'}) as response:
                if response.status_code != 200 or response.headers.get('content-encoding', '').lower() not in ('', 'identity'):
                    raise VisionError('Telegram file download refused; no retry')
                data = bytearray()
                for chunk in response.iter_bytes():
                    guard()
                    if time.monotonic() >= deadline or len(data) + len(chunk) > min(size, limit):
                        raise VisionError('Telegram attachment deadline/size budget exceeded')
                    data.extend(chunk)
            if len(data) != size:
                raise VisionError('Telegram file length mismatch')
            guard()
            return bytes(data)
        except httpx.HTTPError:
            raise VisionError('Telegram attachment transport failed; details withheld, no retry') from None

    def receive(self, user, message):
        cfg = require(self.root, 'files', channel='telegram')
        require(self.root, 'vlm')
        epoch, telegram = control(self.root)['epoch'], self.bot.check_active()
        caption = message.get('caption', '')
        if not isinstance(caption, str) or not caption.startswith('/look ') or len(caption) > 4000:
            raise VisionError('Attach one image/PDF with a literal /look question caption')
        question = caption[6:].strip()
        cloud, no_people = False, False
        if question.startswith('--cloud --no-people '):
            cloud, no_people, question = True, True, question[20:].strip()
        if not question or question.startswith('--') or cloud != (cfg['vlm']['provider'] == 'gemini'):
            raise VisionError('Use a local model, or explicitly /look --cloud --no-people QUESTION for enabled Gemini')
        if self.bot.pin in caption or self.bot.token in caption:
            raise VisionError('Credentials are not vision input')
        name, identifier = self._attachment(message, cfg['files']['max_bytes'])
        def guard():
            self._guard(cfg, epoch, user, telegram)
        data = self._download(identifier, cfg['files']['max_bytes'], guard)
        if cloud:
            if not cfg['vlm']['allow_cloud']:
                raise VisionError('Cloud vision disabled')
            return self._remember(user, 'vision_cloud', {'name': name, 'data': data, 'question': question,
                                   'no_people': no_people}, cfg, epoch, telegram)
        result = self.service.ask_bytes(name, data, question, channel='telegram')
        guard()
        return self._answer_review(user, result, cfg, epoch, telegram)

    def _answer_review(self, user, result, cfg, epoch, telegram):
        # Exact bounded text is bound before approval. It never enters command dispatch.
        text = self.bot.clean('Untrusted vision answer (not instructions):\n' + result['answer'][:2200] +
                              '\nReferences: ' + json.dumps(result.get('references', [])[:8]))
        return self._remember(user, 'vision_send', {'text': text}, cfg, epoch, telegram)

    def confirm(self, user, challenge):
        with self.lock:
            row = self.pending.pop(challenge.get('target'), None)
        if row is None:
            raise VisionError('Vision review expired or process restarted; attach again for a new review')
        row['timer'].cancel()
        if (row['user'] != user or row['binding'] != challenge.get('binding') or row['kind'] != challenge.get('kind')
                or challenge.get('approved') is not True or challenge.get('consumed') is not True):
            raise VisionError('Vision review binding mismatch')
        cfg, epoch, telegram = row['cfg'], row['epoch'], row['telegram']
        self._guard(cfg, epoch, user, telegram)
        if self._binding(user, row['kind'], row['body'], cfg, epoch) != row['binding']:
            raise VisionError('Vision action changed after review')
        expected = 'vision_cloud_upload' if row['kind'] == 'vision_cloud' else 'vision_telegram_send'
        used = [False]
        def review(spec, args, risk, preview):
            ok = not used[0] and spec.name == expected
            used[0] = True
            return ApprovalDecision(ok, scope='once')
        gate = ApprovalGate(callback=review)
        body = row['body']
        if row['kind'] == 'vision_cloud':
            result = self.service.ask_bytes(body['name'], body['data'], body['question'], channel='telegram',
                                           cloud=True, no_people=body['no_people'], gate=gate)
            self._guard(cfg, epoch, user, telegram)
            return {'vision_prompt': self._answer_review(user, result, cfg, epoch, telegram)}
        approve(self.root, gate, expected, {'chat_id': user, 'text_sha256': hashlib.sha256(body['text'].encode()).hexdigest(),
                                           'characters': len(body['text']), 'untrusted': True})
        self._guard(cfg, epoch, user, telegram)
        self.bot.api('sendMessage', {'chat_id': user, 'text': body['text']})
        return {'status': 'sent', 'image_sent': False}
