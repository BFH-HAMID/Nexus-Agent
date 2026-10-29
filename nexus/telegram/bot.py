"""Whitelisted, replay-resistant Telegram control over existing Jarvis state."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

import httpx
import yaml
from filelock import FileLock

from ..delegation.core import Delegations
from ..jarvis.approvals import Approvals
from ..jarvis.daemon import Daemon
from ..jarvis.inbox import Inbox
from ..jarvis.queue import Queue
from ..jarvis.runtime import signature
from ..jarvis.sources import bounded
from ..redaction import mask
from ..redaction import secrets as known_secrets


def settings(root):
    path = root / 'telegram.yaml'
    try:
        value = yaml.safe_load(bounded(path, 16000)) if path.exists() else {}
    except yaml.YAMLError:
        raise ValueError('Invalid Telegram YAML') from None
    defaults = {'schema': 1, 'enabled': False, 'user_ids': [], 'poll_seconds': 20, 'challenge_seconds': 60,
                'quiet_utc_hours': [], 'allow_report_text': False}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError('Unknown Telegram policy')
    result = {**defaults, **value}
    if type(result['schema']) is not int or result['schema'] != 1 or type(result['enabled']) is not bool or type(result['allow_report_text']) is not bool:
        raise ValueError('Explicit Telegram flags required')
    ids = result['user_ids']
    if not isinstance(ids, list) or len(ids) > 10 or any(type(i) is not int or not 0 < i < 2**53 for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Whitelist up to ten exact numeric private-chat user IDs')
    if result['enabled'] and not ids:
        raise ValueError('Enabled Telegram requires an explicit user whitelist')
    for key, low, high in [('poll_seconds', 1, 30), ('challenge_seconds', 15, 120)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Invalid Telegram limit')
    hours = result['quiet_utc_hours']
    if not isinstance(hours, list) or len(hours) not in (0, 2) or any(type(h) is not int or not 0 <= h <= 23 for h in hours):
        raise ValueError('Quiet hours are an explicit UTC [start,end) interval')
    return result


class Bot:
    def __init__(self, studio, *, client=None, stop_event=None):
        self.studio = studio
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.authority_lock = threading.RLock()
        self.queue = Queue(studio)
        self.root = studio.root
        self.cfg = settings(self.root)
        self.token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        self.pin = os.environ.get('TELEGRAM_APPROVAL_PIN', '')
        if not self.cfg['enabled'] or not re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,100}', self.token) or not re.fullmatch(r'[0-9]{8,12}', self.pin):
            raise ValueError('Explicit enable/whitelist, env bot token and 8–12 digit env PIN required')
        self.key = 'telegram-' + hashlib.sha256((str(self.root) + self.token).encode()).hexdigest()[:32]
        self.client = client or httpx.Client(timeout=35, trust_env=False, follow_redirects=False)
        self.owns_client = client is None
        token, pin = self.token, self.pin
        class NoCredentials(logging.Filter):
            def filter(self, record):
                record.msg = str(record.getMessage()).replace(token, '[REDACTED]').replace(pin, '[REDACTED]')
                record.args = ()
                return True
        self.log_filter = NoCredentials()
        logging.getLogger('httpx').addFilter(self.log_filter)

    def close(self):
        if self.owns_client:
            self.client.close()
        logging.getLogger('httpx').removeFilter(self.log_filter)

    def check_active(self):
        if self.stop_event.is_set():
            raise ValueError("Telegram polling stop requested")
        current = settings(self.root)
        if (not current["enabled"] or os.environ.get("TELEGRAM_BOT_TOKEN") != self.token
                or os.environ.get("TELEGRAM_APPROVAL_PIN") != self.pin):
            raise ValueError("Telegram transport authority revoked")
        return current

    def api(self, method, payload):
        with self.authority_lock:
            self.check_active()
            return self._api(method, payload)

    def _api(self, method, payload):
        if method not in ('getUpdates', 'sendMessage'):
            raise ValueError('Telegram API method not authorized')
        current = settings(self.root)
        if (not current['enabled'] or os.environ.get('TELEGRAM_BOT_TOKEN') != self.token
                or os.environ.get('TELEGRAM_APPROVAL_PIN') != self.pin
                or method == 'sendMessage' and payload.get('chat_id') not in current['user_ids']):
            raise ValueError('Telegram transport authority revoked')
        try:
            with self.client.stream('POST', 'https://api.telegram.org/bot' + self.token + '/' + method, json=payload) as response:
                if response.status_code != 200:
                    raise ValueError('Telegram API refused')
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > 1000000:
                        raise ValueError('Telegram response exceeds budget')
                    body.extend(chunk)
                value = json.loads(body)
                if value.get('ok') is not True:
                    raise ValueError('Telegram API reported failure')
                return value['result']
        except (httpx.HTTPError, KeyError, TypeError):
            raise ValueError('Telegram transport unavailable; sensitive request/error details withheld') from None

    def clean(self, text):
        return mask(str(text), known_secrets(self.root) + [self.token, self.pin])[:3500]

    def challenge(self, user, kind, row):
        records = self.studio.items('telegram-challenge')
        if len(records) >= 1000:
            raise ValueError('Challenge history quota full; archive trusted state')
        identifier = secrets.token_hex(8)
        cfg_digest = hashlib.sha256(json.dumps(self.cfg, sort_keys=True).encode()).hexdigest()
        binding = signature(row) if kind == 'job' else row.get('args_digest') if kind == 'action' else 'kill'
        self.studio.put('telegram-challenge', {'user': user, 'kind': kind, 'target': row.get('id'), 'binding': binding,
                        'cfg_digest': cfg_digest, 'expires': time.time() + self.cfg['challenge_seconds'], 'tries': 0, 'consumed': False}, identifier)
        return f'Review actual task/diff in local HUD. Target {row.get("id", "daemon")}, digest {binding[:12]}. Confirm once: /confirm {identifier} YOUR_PIN. Expires shortly; no arguments/code uploaded.'

    def confirm(self, user, identifier, supplied):
        # Consume BEFORE side effect. A crash can drop intent, never replay it.
        with self.queue.locked():
            row = self.studio.find('telegram-challenge', identifier)
            if (row['user'] != user or row['consumed'] or row['expires'] <= time.time()
                    or row['cfg_digest'] != hashlib.sha256(json.dumps(self.cfg, sort_keys=True).encode()).hexdigest()):
                raise ValueError('Stale/replayed challenge')
            pin_key = self.key + '-pin-' + str(user)
            budget = next((r for r in self.studio.items('telegram-pin-budget') if r['id'] == pin_key), {})
            hour = int(time.time() // 3600)
            failures = budget.get('failures', 0) if budget.get('hour') == hour else 0
            if failures >= 5:
                raise ValueError('PIN failure limit reached; wait for next hour/operator review')
            row['tries'] += 1
            correct = hmac.compare_digest(supplied.encode(), self.pin.encode())
            if not correct:
                self.studio.put('telegram-pin-budget', {'hour': hour, 'failures': failures + 1}, pin_key)
            row['consumed'] = correct or row['tries'] >= 3
            self.studio.put('telegram-challenge', row, row['id'])
            if not correct:
                raise ValueError('PIN refused')
        if row['kind'] == 'kill':
            return Daemon(self.studio).request('kill')
        if row['kind'] == 'job':
            current = self.studio.find('jarvis-job', row['target'])
            if signature(current) != row['binding']:
                raise ValueError('Task changed after challenge')
            return self.queue.review(row['target'], 'approve', 'APPROVE QUEUED TASK ' + row['target'])
        current = self.studio.find('jarvis-approval', row['target'])
        if current['args_digest'] != row['binding']:
            raise ValueError('Action changed after challenge')
        return Approvals(self.studio).decide(row['target'], True, current['confirmation'])

    def handle(self, message):
        self.cfg = settings(self.root)
        if not isinstance(message, dict) or not isinstance(message.get('from'), dict) or not isinstance(message.get('chat'), dict):
            return None
        user = message.get('from', {}).get('id')
        chat = message.get('chat', {})
        if (not self.cfg['enabled'] or type(user) is not int or user not in self.cfg['user_ids'] or chat.get('type') != 'private'
                or type(chat.get('id')) is not int or chat['id'] != user or message.get('from', {}).get('is_bot') is not False
                or any(k in message for k in ('forward_origin', 'forward_date', 'forward_from', 'via_bot'))
                or type(message.get('date')) is not int or not -30 <= time.time() - message['date'] <= 120):
            return None
        text = message.get('text')
        if not isinstance(text, str) or not 0 < len(text) <= 8000:
            return None
        with self.queue.locked():
            rate_key = self.key + '-rate-' + str(user)
            rate = next((r for r in self.studio.items('telegram-rate') if r['id'] == rate_key), {})
            minute = int(time.time() // 60)
            count = rate.get('calls', 0) if rate.get('minute') == minute else 0
            if count >= 20:
                raise ValueError('Per-user command rate exceeded')
            self.studio.put('telegram-rate', {'minute': minute, 'calls': count + 1}, rate_key)
        command, _, rest = text.strip().partition(' ')
        if command == '/confirm':
            parts = rest.split()
            if len(parts) != 2:
                raise ValueError('Challenge and PIN required')
            result = self.confirm(user, *parts)
            return self.clean(json.dumps({'accepted': True, 'state': result.get('status', result.get('request', result.get('state'))),
                                         'note': 'Request accepted, not execution/readiness proof.'}))
        if self.pin in text or self.token in text:
            raise ValueError('Credentials are not task content')
        if command == '/status':
            state = Daemon(self.studio).status()
            return json.dumps({k: state[k] for k in ('state', 'execution_enabled', 'lock_held')})
        if command == '/tasks':
            return json.dumps([{'id': r['id'], 'status': r['status']} for r in self.queue._jobs()[-20:]])
        if command == '/delegate':
            row = Delegations(self.studio).create(rest)
            return 'Queued for fresh approval: ' + row['id']
        if command == '/approve':
            action = next((r for r in Approvals(self.studio).items() if r['id'] == rest and r['state'] == 'pending'), None)
            row = action or self.studio.find('jarvis-job', rest)
            return self.challenge(user, 'action' if action else 'job', row)
        if command == '/reject':
            action = next((r for r in Approvals(self.studio).items() if r['id'] == rest), None)
            result = Approvals(self.studio).decide(rest, False) if action else self.queue.review(rest, 'reject')
            return 'Rejected: ' + result['id']
        if command == '/pause':
            Daemon(self.studio).request('pause')
            return 'Pause requested; inspect actual heartbeat for acknowledgement.'
        if command == '/kill':
            return self.challenge(user, 'kill', {})
        if command == '/report':
            row = self.studio.find('jarvis-job', rest)
            result = {'id': rest, 'state': row['status'], 'attempts': row.get('attempts', 0)}
            if self.cfg['allow_report_text']:
                result['agent_report_not_test_proof'] = row.get('result', {}).get('summary', '')[:1500]
            return self.clean(json.dumps(result))
        raise ValueError('Unknown literal Telegram command; no arbitrary chat/shell execution')

    def poll_once(self):
        state = next((r for r in self.studio.items('telegram-state') if r['id'] == self.key), {'offset': 0})
        updates = self.api('getUpdates', {'offset': state['offset'], 'timeout': self.cfg['poll_seconds'], 'limit': 50, 'allowed_updates': ['message']})
        if not isinstance(updates, list) or len(updates) > 50:
            raise ValueError('Invalid bounded Telegram update batch')
        for update in updates:
            identifier = update.get('update_id')
            if type(identifier) is not int or not 0 <= identifier < 2**53 or identifier < state['offset']:
                continue
            state['offset'] = identifier + 1
            self.studio.put('telegram-state', state, self.key)
            message = update.get('message', {})
            try:
                with self.authority_lock:
                    self.cfg = self.check_active()
                    reply = self.handle(message)
            except (ValueError, OSError, KeyError, TypeError):
                # Only reply after independent identity check, never to arbitrary IDs.
                user = message.get('from', {}).get('id')
                if type(user) is not int or user not in self.cfg['user_ids'] or message.get('chat', {}).get('id') != user:
                    continue
                reply = 'Request refused. Inspect policy, exact IDs, PIN expiry and current state. No automatic retry/replay.'
            if reply:
                self.api('sendMessage', {'chat_id': message['chat']['id'], 'text': self.clean(reply)})
        self.notify(state)

    def notify(self, state):
        self.cfg = settings(self.root)
        if not self.cfg['enabled']:
            return
        delivery = Inbox(self.studio).delivery_policy()
        hour = datetime.now(timezone.utc).hour
        quiet = self.cfg['quiet_utc_hours']
        in_quiet = bool(quiet) and (quiet[0] <= hour < quiet[1] if quiet[0] < quiet[1] else hour >= quiet[0] or hour < quiet[1])
        if delivery['quiet'] or delivery['dnd_until'] > time.time() or in_quiet or time.time() - state.get('last_notice', 0) < 60:
            return
        digest = Inbox(self.studio).digest()
        fingerprint = hashlib.sha256(json.dumps(digest, sort_keys=True).encode()).hexdigest()
        if fingerprint == state.get('notice_digest') or not (digest['unread'] or digest['pending_jobs'] or digest['pending_actions']):
            return
        state.update(last_notice=time.time())
        self.studio.put('telegram-state', state, self.key)
        for user in self.cfg['user_ids']:
            self.api('sendMessage', {'chat_id': user, 'text': f'Nexus inbox: {digest["unread"]} unread results, {digest["pending_jobs"]} tasks and {digest["pending_actions"]} actions need review. Use the local HUD for details.'})
        state['notice_digest'] = fingerprint
        self.studio.put('telegram-state', state, self.key)

    def run(self, *, on_ready=None, on_poll=None):
        path = self.queue.directory() / 'telegram.lock'
        if path.is_symlink():
            raise ValueError('Unsafe Telegram lock')
        with FileLock(str(path), timeout=0):
            if on_ready:
                on_ready()
            while not self.stop_event.is_set() and settings(self.root)['enabled']:
                self.cfg = self.check_active()
                self.poll_once()
                if on_poll:
                    on_poll()
