"""Optional Web Push, generic notification only, one freshly reviewed send.

No automatic approval text, transcript, camera data or credentials enter a push
payload. Only exact operator-allowed browser push services are contacted. The
subscription's opaque endpoint is private state, not a URL for general fetching.
"""
from __future__ import annotations

import base64
import logging
import os
from urllib.parse import parse_qsl, unquote, urlsplit

from .presence_policy import require


def validate_subscription(value, cfg):
    if not isinstance(value, dict) or set(value) - {'endpoint', 'keys', 'expirationTime'}:
        raise ValueError('Bounded browser push subscription required')
    endpoint, keys = value.get('endpoint'), value.get('keys')
    if not isinstance(endpoint, str) or len(endpoint) > 2048 or any(c.isspace() for c in endpoint):
        raise ValueError('Invalid push endpoint')
    url = urlsplit(endpoint)
    if (url.scheme != 'https' or url.username or url.password or url.fragment or url.port not in {None, 443}
            or 'https://' + str(url.hostname) not in cfg['push_origins']
            or not url.path.startswith('/') or len(url.path) < 2):
        raise ValueError('Push endpoint is not an exact allowed HTTPS provider')
    if not isinstance(keys, dict) or set(keys) != {'p256dh', 'auth'}:
        raise ValueError('Browser encryption keys required')
    for name, size in (('p256dh', 65), ('auth', 16)):
        text = keys[name]
        if not isinstance(text, str) or not 1 <= len(text) <= 128:
            raise ValueError('Invalid push encryption key')
        try:
            raw = base64.b64decode(text + '=' * (-len(text) % 4), altchars=b'-_', validate=True)
        except ValueError:
            raise ValueError('Invalid push encryption key') from None
        if len(raw) != size or name == 'p256dh' and raw[0] != 4:
            raise ValueError('Invalid push encryption key size')
    return {'endpoint': endpoint, 'keys': dict(keys)}


def public_key():
    value = os.environ.get('NEXUS_VAPID_PUBLIC_KEY', '')
    if not value:
        raise ValueError('Set the VAPID key pair in the private environment/native vault')
    validate_subscription({'endpoint': 'https://fcm.googleapis.com/vapid-key-validation',
                           'keys': {'p256dh': value, 'auth': base64.urlsafe_b64encode(bytes(16)).decode()}},
                          {'push_origins': ['https://fcm.googleapis.com']})
    return value


def send(workspace, subscription):
    cfg = require(workspace, 'push_enabled')
    sub = validate_subscription(subscription, cfg)
    key = os.environ.get('NEXUS_VAPID_PRIVATE_KEY', '')
    contact = os.environ.get('NEXUS_VAPID_SUBJECT', '')
    if not key or not contact.startswith('mailto:') or len(contact) > 200:
        raise ValueError('Private VAPID key and mailto subject required in environment')
    try:
        import requests
        from pywebpush import webpush
    except ImportError:
        raise ValueError('Web Push requires the optional nexus-agent[mobile] extra') from None

    class PinnedSession(requests.Session):
        def send(self, request, **kwargs):
            if request.url != sub['endpoint']:
                raise ValueError('Push redirects are refused')
            kwargs['allow_redirects'] = False
            kwargs['timeout'] = 10
            return super().send(request, **kwargs)

    # urllib3 logs the URL path (not always the full endpoint). Handler filters
    # also catch propagated child-logger records; logger filters alone do not.
    url = urlsplit(sub['endpoint'])
    fragments = {part for part in unquote(url.path).split('/') if len(part) >= 8}
    fragments.update(value for _, value in parse_qsl(url.query) if len(value) >= 6)
    private = sorted({sub['endpoint'], url.path, url.query, unquote(url.path), key,
                      contact, *sub['keys'].values(), *fragments} - {''}, key=len, reverse=True)
    prefixes = ('pywebpush', 'py_vapid', 'requests', 'urllib3')
    class PrivatePushLog(logging.Filter):
        def filter(self, record):
            try:
                message = record.getMessage()
            except Exception:
                message = 'Private push diagnostic withheld'
            sensitive = any(value in message for value in private)
            for value in private:
                message = message.replace(value, '[REDACTED_PUSH]')
            if sensitive or record.name.startswith(prefixes):
                # Exception formatters can otherwise append the original URL/key.
                record.exc_info = record.exc_text = record.stack_info = None
            record.msg, record.args = message, ()
            return True
    log_filter = PrivatePushLog()
    names = {'', *prefixes, 'urllib3.connectionpool'} | {
        name for name in list(logging.Logger.manager.loggerDict) if name.startswith(prefixes)}
    loggers = [logging.getLogger(name) for name in names]
    targets = set(loggers) | {handler for logger in loggers for handler in logger.handlers}
    for target in targets:
        target.addFilter(log_filter)
    try:
        with PinnedSession() as session:
            session.trust_env = False
            response = webpush(subscription_info=sub,
                               data='{"title":"Nexus","body":"Open Nexus to review an update."}',
                               vapid_private_key=key, vapid_claims={'sub': contact},
                               ttl=60, timeout=10, requests_session=session, verbose=False)
        if not 200 <= response.status_code < 300:
            raise ValueError('Push provider refused')
    except Exception:
        raise ValueError('Push outcome unavailable; do not retry automatically. Request a fresh review.') from None
    finally:
        for target in targets:
            target.removeFilter(log_filter)
    return {'submitted': True, 'delivered': False, 'note': 'Provider accepted; phone delivery is not verified.'}
