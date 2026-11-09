"""Revocable scoped device tokens; only keyed hashes are durable.

Tokens are presented in X-Nexus-Token or an HttpOnly cookie, NEVER in URLs.
A changed serve token invalidates every device. Device sessions cannot enroll
other devices, edit policy, or access the general dashboard administration API.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from http.cookies import CookieError, SimpleCookie

from .presence_policy import require
from .presence_store import Store

COOKIE = 'nexus_device_session'
TOKEN = re.compile(r'nd1_([a-f0-9]{24})_([A-Za-z0-9_-]{43})')


class Devices:
    def __init__(self, workspace, master, *, clock=time.time):
        self.workspace, self.master, self.clock = workspace, master, clock
        self.store = Store(workspace, 'devices', lambda: {'revision': 0, 'items': {}})

    def _hash(self, token):
        if not self.master:
            raise ValueError('A configured serve token is required')
        return hmac.new(self.master.encode(), token.encode(), hashlib.sha256).hexdigest()

    def enroll(self, name):
        cfg = require(self.workspace, 'devices_enabled')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(ord(c) < 32 for c in name):
            raise ValueError('Device name must be 1–60 printable characters')
        ident = secrets.token_hex(12)
        token = 'nd1_' + ident + '_' + secrets.token_urlsafe(32)
        now = self.clock()
        row = {'id': ident, 'name': name.strip(), 'hash': self._hash(token), 'created': now,
               'expires': now + cfg['device_days'] * 86400, 'revoked': False,
               'scopes': cfg['device_scopes'], 'last_seen': None, 'subscription': None}
        def add(data):
            items = data['items']
            # Expired/revoked token strings can never become valid again.
            for key in list(items):
                if items[key]['revoked'] or items[key]['expires'] <= now:
                    del items[key]
            if len(items) >= cfg['max_devices']:
                raise ValueError('Device quota reached; revoke a device first')
            items[ident] = row
        self.store.update(add)
        return {**self.public(row), 'token': token}

    def authenticate(self, token):
        try:
            cfg = require(self.workspace, 'devices_enabled')
            match = TOKEN.fullmatch(token) if isinstance(token, str) else None
            if not match or not self.master:
                return None
            row = self.store.read()['items'].get(match[1])
            if (not row or row['revoked'] or row['expires'] <= self.clock()
                    or not hmac.compare_digest(row['hash'], self._hash(token))):
                return None
            return {'kind': 'device', 'id': row['id'], 'name': row['name'],
                    'scopes': sorted(set(row['scopes']) & set(cfg['device_scopes'])), 'expires': row['expires']}
        except (ValueError, OSError, KeyError, TypeError):
            return None

    def live(self, ident):
        cfg = require(self.workspace, 'devices_enabled')
        row = self.store.read()['items'].get(ident)
        if not row or row['revoked'] or row['expires'] <= self.clock():
            raise ValueError('Device is expired or revoked')
        return {**row, 'scopes': sorted(set(row['scopes']) & set(cfg['device_scopes']))}

    @staticmethod
    def public(row):
        return {key: row.get(key) for key in ('id', 'name', 'created', 'expires', 'revoked', 'scopes', 'last_seen')} | {
            'push_subscribed': bool(row.get('subscription'))}

    def items(self):
        return [self.public(row) for row in self.store.read()['items'].values()]

    def touch(self, ident):
        # At most one durable heartbeat per device every 15 seconds.
        row = self.live(ident)
        now = self.clock()
        if now - (row.get('last_seen') or 0) < 15:
            return
        def change(data):
            item = data['items'].get(ident)
            if item and not item['revoked']:
                item['last_seen'] = now
        self.store.update(change)

    def revoke(self, ident):
        def change(data):
            row = data['items'].get(ident)
            if row is None:
                raise ValueError('Unknown device')
            row.update(revoked=True, subscription=None)
            return {'id': ident, 'revoked': True}
        return self.store.update(change)

    def subscribe(self, ident, subscription):
        self.live(ident)
        def change(data):
            row = data['items'].get(ident)
            if not row or row['revoked'] or row['expires'] <= self.clock():
                raise ValueError('Device revoked during subscription')
            row['subscription'] = subscription
        self.store.update(change)
        return {'subscribed': subscription is not None}


def cookie_token(header):
    try:
        if len(header) > 8192:
            return '', True
        cookies = SimpleCookie(header)
        return (cookies[COOKIE].value, True) if COOKIE in cookies else ('', False)
    except (ValueError, CookieError):
        return '', True


def cookie(token='', *, max_age=0):
    if token and not TOKEN.fullmatch(token):
        raise ValueError('Invalid device cookie')
    return f'{COOKIE}={token}; Max-Age={max(0, int(max_age))}; Path=/; HttpOnly; SameSite=Strict'
