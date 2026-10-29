"""Reviewed native World settings; nonsecret YAML and optional native-vault key."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import yaml

from .hud.world.core import settings
from .hud.world.sources import SOURCES
from .hud.world.weatherapi import credential, remember


def path(state):
    target = state.workspace / 'widget.yaml'
    if target.is_symlink() or (target.exists() and (not target.is_file() or target.stat().st_size > 32000)):
        raise ValueError('Unsafe World configuration file')
    return target


def revision(state):
    target = path(state)
    return hashlib.sha256(target.read_bytes() if target.exists() else b'').hexdigest()


def read_settings(state):
    try:
        return settings(state.workspace)
    except (yaml.YAMLError, UnicodeError):
        raise ValueError('World YAML is invalid; inspect it locally. File contents are not exposed.') from None


def view(state):
    with state.lock:
        key = getattr(state, 'weatherapi_key', '')
        current_revision = revision(state)
        return {'settings': read_settings(state), 'revision': current_revision,
                'weather_key_present': bool(key or credential(state.workspace)),
                'weather_key_source': 'session' if key else 'environment' if os.environ.get('WEATHERAPI_KEY') else 'native vault or not configured',
                'sources': [{'id': k, 'category': v[0], 'name': v[1], 'url': v[2], 'terms': v[3]} for k, v in SOURCES.items()]}


def save(state, payload):
    if payload.get('confirmation') != 'SAVE NEWS AND WEATHER' or state.demo:
        raise ValueError('Review settings and type SAVE NEWS AND WEATHER in live mode')
    if set(payload) - {'confirmation', 'revision', 'settings', 'key', 'remember_key', 'clear_session_key'}:
        raise ValueError('Unknown World settings fields')
    new = payload.get('settings')
    if not isinstance(new, dict) or set(new) - {'sources', 'language', 'weather_provider', 'enabled'}:
        raise ValueError('Choose known feeds, provider, language and enabled setting')
    key = payload.get('key', '')
    if not isinstance(key, str) or (key and (not 32 <= len(key) <= 256 or not key.isascii() or any(c.isspace() for c in key))):
        raise ValueError('WeatherAPI key format is invalid')
    if type(payload.get('remember_key', False)) is not bool or type(payload.get('clear_session_key', False)) is not bool:
        raise ValueError('Credential switches must be boolean')
    with state.lock:
        if payload.get('revision') != revision(state):
            raise ValueError('World settings changed; reload before saving')
        target = path(state)
        merged = {**read_settings(state), **new}
        fd, tmp = tempfile.mkstemp(prefix='.world-settings-', dir=state.workspace)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                yaml.safe_dump(merged, handle, allow_unicode=True, sort_keys=False)
            # Validate using the existing widget schema before replacing the real file.
            import tempfile as temp
            with temp.TemporaryDirectory() as directory:
                (Path(directory) / 'widget.yaml').write_text(Path(tmp).read_text(encoding='utf-8'), encoding='utf-8')
                settings(directory)
            if key and payload.get('remember_key'):
                try:
                    remember(state.workspace, key)
                except (ImportError, ValueError):
                    raise ValueError('Native OS vault unavailable. Save for this server session or configure WEATHERAPI_KEY; no plaintext fallback.') from None
            os.replace(tmp, target)
            if key:
                state.weatherapi_key = key
            elif payload.get('clear_session_key'):
                state.weatherapi_key = ''
            with state.gui.lock:
                state.gui._world = None
        finally:
            Path(tmp).unlink(missing_ok=True)
    return {'saved': True, 'revision': revision(state), 'notice': 'World settings saved; key never written to YAML. Existing tasks are unchanged.'}
