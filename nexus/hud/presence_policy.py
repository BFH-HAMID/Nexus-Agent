"""Operator-only, OFF-default shared HUD/mobile/service policy in hud.yaml."""
from __future__ import annotations

import copy
import ipaddress
from pathlib import Path

import yaml

PRESENCE = {
    'enabled': False, 'devices_enabled': False, 'conversation_enabled': False,
    'handoff_enabled': False, 'voice_enabled': False, 'push_enabled': False, 'world_enabled': False,
    'device_scopes': ['read'], 'default_speaker': 'none',
    'max_devices': 12, 'device_days': 30, 'history_turns': 20,
    'retention_hours': 24, 'max_streams': 16, 'push_origins': [],
}
SERVICE = {
    'enabled': False, 'host': '127.0.0.1', 'port': 8787,
    'restart_initial_seconds': 2, 'restart_max_seconds': 60,
    'log_bytes': 1_048_576, 'log_backups': 3,
}
SCOPES = {'read', 'chat', 'approve', 'voice', 'handoff', 'push'}
PUSH_ORIGINS = {'https://fcm.googleapis.com', 'https://updates.push.services.mozilla.com',
                'https://web.push.apple.com'}


def sections(value):
    result = {}
    for name, defaults in (('presence', PRESENCE), ('service', SERVICE)):
        incoming = value.get(name, {})
        if not isinstance(incoming, dict) or set(incoming) - set(defaults):
            raise ValueError('Unknown HUD ' + name + ' setting')
        cfg = {**copy.deepcopy(defaults), **incoming}
        for key, default in defaults.items():
            if type(default) is bool and type(cfg[key]) is not bool:
                raise ValueError('HUD flags must be explicit booleans')
        result[name] = cfg
    p, s = result['presence'], result['service']
    for cfg, key, low, high in (
        (p, 'max_devices', 1, 32), (p, 'device_days', 1, 365),
        (p, 'history_turns', 1, 50), (p, 'retention_hours', 1, 168),
        (p, 'max_streams', 1, 32), (s, 'port', 1024, 65535),
        (s, 'restart_initial_seconds', 1, 60), (s, 'restart_max_seconds', 2, 600),
        (s, 'log_bytes', 16_384, 10_485_760), (s, 'log_backups', 1, 10),
    ):
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError('Invalid HUD limit: ' + key)
    scopes = p['device_scopes']
    if (not isinstance(scopes, list) or not scopes or len(scopes) > len(SCOPES)
            or any(not isinstance(x, str) or x not in SCOPES for x in scopes)
            or len(set(scopes)) != len(scopes) or 'read' not in scopes):
        raise ValueError('Explicit bounded device scopes including read required')
    if p['default_speaker'] not in ('none', 'local'):
        raise ValueError('Default speaker must be none or local; select paired devices at runtime')
    origins = p['push_origins']
    if (not isinstance(origins, list) or len(origins) > 3
            or any(not isinstance(x, str) or x not in PUSH_ORIGINS for x in origins)):
        raise ValueError('Push origins must be exact supported HTTPS push services')
    if p['push_enabled'] and not origins:
        raise ValueError('Push needs an explicit push_origins allowlist')
    if s['restart_initial_seconds'] > s['restart_max_seconds']:
        raise ValueError('Invalid restart backoff range')
    try:
        ipaddress.ip_address(s['host'])
    except (ValueError, TypeError):
        raise ValueError('Service host must be an explicit IP bind address') from None
    return result


def load(root):
    path = Path(root) / 'hud.yaml'
    from ..jarvis.sources import bounded
    try:
        value = yaml.safe_load(bounded(path, 16_000)) if path.exists() or path.is_symlink() else {}
    except yaml.YAMLError:
        raise ValueError('Invalid operator hud.yaml') from None
    if not isinstance(value, dict):
        raise ValueError('hud.yaml must be a mapping')
    if set(value) - {'schema', 'host', 'port', 'refresh_seconds', 'max_connections', 'presence', 'service'}:
        raise ValueError('Unknown HUD setting')
    if type(value.get('schema', 1)) is not int or value.get('schema', 1) != 1:
        raise ValueError('Invalid HUD schema')
    return sections(value)


def require(root, capability='enabled'):
    cfg = load(root)['presence']
    if not cfg['enabled'] or not cfg[capability]:
        raise ValueError('Shared presence capability is OFF in operator hud.yaml: ' + capability)
    return cfg
