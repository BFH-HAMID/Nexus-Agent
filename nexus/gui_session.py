"""GUI-only signed browser sessions, scoped to workspace and exact Host.

The original GUI API key is never stored in browser JS storage or in the cookie.
"""
import hashlib
import hmac
import secrets
import time
from http.cookies import CookieError, SimpleCookie

NAME = 'nexus_gui_session'
LIFETIME = 400 * 86400


def signature(state, host, value):
    message = (str(state.workspace) + '|' + host + '|' + value).encode()
    return hmac.new(state.token.encode(), message, hashlib.sha256).hexdigest()


def valid(state, host, header):
    if not state.token or len(header) > 8192:
        return False
    try:
        cookie = SimpleCookie(header)
        value = cookie[NAME].value
        expires, nonce, signed = value.split('.')
        if not (len(value) < 256 and len(nonce) == 32 and time.time() < int(expires) <= time.time() + LIFETIME + 60
                and hmac.compare_digest(signed, signature(state, host, expires + '.' + nonce))):
            return False
    except (ValueError, KeyError, TypeError, CookieError):
        return False
    # A signed cookie can still have been revoked. An unreadable store fails
    # closed: the serve token in the login box remains the way back in.
    from .securitycenter import is_revoked
    try:
        return not is_revoked(state.workspace, nonce)
    except (ValueError, OSError):
        return False


def issue(state, host, remember=True, nonce=''):
    nonce = nonce or ('p' if remember else 's') + secrets.token_hex(16)[1:]
    expires = int(time.time()) + LIFETIME
    value = str(expires) + '.' + nonce
    age = f'; Max-Age={LIFETIME}' if remember else ''
    cookie = f'{NAME}={value}.{signature(state, host, value)}{age}; Path=/; HttpOnly; SameSite=Strict'
    # The list is what the Security page shows and revokes; a failure to record
    # never disables an otherwise valid login, it is reported there instead.
    try:
        from .securitycenter import record_session
        record_session(state.workspace, nonce, host, remember, expires)
    except (ValueError, OSError):
        pass
    return cookie


def renew(state, host, header):
    """Call only after valid(): keep the same nonce, refresh the expiry."""
    nonce = SimpleCookie(header)[NAME].value.split('.')[1]
    return issue(state, host, remember=not nonce.startswith('s'), nonce=nonce)


def forget():
    return f'{NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict'


def launch_credential(root, explicit='', remember=False):
    """Optional shared GUI credential; enrolled vault failures never disable auth."""
    import os
    from pathlib import Path

    from .config import nexus_home
    from .hud.auth import launch_token
    root = Path(root).resolve()
    directory = nexus_home() / 'gui-auth'
    marker = directory / (hashlib.sha256(str(root).encode()).hexdigest() + '.enrolled')
    explicit = explicit or os.environ.get('NEXUS_SERVE_TOKEN', '')
    if explicit and not remember:
        return explicit
    if directory.is_symlink() or marker.is_symlink():
        raise ValueError('Unsafe GUI credential enrollment path')
    if not remember and not marker.exists():
        return ''  # Existing loopback GUI access; never invent a new secret.
    value = launch_token(root, remember, token=explicit, service='nexus-gui')
    if not value:
        raise ValueError('Enrolled GUI credential unavailable; unlock the OS vault')
    if remember:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as handle:
            handle.write('native-vault-enrolled\n')
    return value
