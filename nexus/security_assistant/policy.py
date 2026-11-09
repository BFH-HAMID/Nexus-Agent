"""Operator-only defensive policy. No source, model or runtime flag grants scope."""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
from pathlib import Path

import yaml

from ..jarvis.sources import bounded

MONITORS = {name: [] for name in ('auth_logs', 'fail2ban_logs', 'ufw_logs', 'users_files', 'cron_dirs',
    'systemd_dirs', 'integrity_files', 'certificate_files', 'dpkg_files', 'cve_feeds')}
MONITORS.update(listening_ports=False, python_packages=False)
DEFAULTS = {
    'schema': 1, 'enabled': False, 'own_host_confirmed': False, 'host_label': 'local',
    'monitoring_enabled': False, 'watch_enabled': False, 'scan_enabled': False,
    'responses_enabled': False, 'analysis_enabled': False, 'analysis_allow_remote': False,
    'speech_enabled': False, 'hud_enabled': False,
    'monitors': MONITORS, 'targets': [], 'scan_ports': [22, 80, 443],
    'response_accounts': [], 'account_state_files': {}, 'rotation_paths': {},
    'analysis_roles': ['security-audit', 'blue-team', 'forensics'], 'analysis_agent': '',
    'ssh_threshold': 20, 'ssh_window_seconds': 300, 'cooldown_seconds': 300,
    'certificate_days': 30, 'poll_seconds': 15, 'max_records': 1000,
    'max_evidence_bytes': 25_000_000, 'max_read_bytes': 2_000_000,
    'max_tick_bytes': 8_000_000, 'cve_max_age_days': 7,
}
FLAGS = [key for key, value in DEFAULTS.items() if type(value) is bool]


def literal(value, pattern, label):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError('Invalid security setting: ' + label)
    return value


def path_value(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 1000 or any(ord(c) < 32 for c in value)
            or '\x7f' in value or '..' in Path(value).parts or '://' in value):
        raise ValueError('Security input paths must be explicit literal local paths')
    return value


def ports(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= 32
            or any(type(p) is not int or not 1 <= p <= 65535 for p in value) or len(set(value)) != len(value)):
        raise ValueError('Select 1–32 distinct TCP ports')
    return sorted(value)


def load(workspace):
    path = Path(workspace) / 'security.yaml'
    try:
        raw = yaml.safe_load(bounded(path, 32_000)) if path.exists() or path.is_symlink() else {}
    except yaml.YAMLError:
        raise ValueError('Invalid operator security.yaml') from None
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError('Unknown security.yaml setting')
    cfg = {**copy.deepcopy(DEFAULTS), **raw}
    if type(cfg['schema']) is not int or cfg['schema'] != 1 or any(type(cfg[k]) is not bool for k in FLAGS):
        raise ValueError('Explicit security schema/boolean flags required')
    literal(cfg['host_label'], r'[A-Za-z0-9][A-Za-z0-9._-]{0,59}', 'host_label')
    if cfg['enabled'] and not cfg['own_host_confirmed']:
        raise ValueError('Confirm ownership/authorization for this local host in security.yaml')
    if not isinstance(cfg['monitors'], dict) or set(cfg['monitors']) - set(MONITORS):
        raise ValueError('Unknown security monitor')
    cfg['monitors'] = {**copy.deepcopy(MONITORS), **cfg['monitors']}
    for key, value in cfg['monitors'].items():
        if type(MONITORS[key]) is bool:
            if type(value) is not bool:
                raise ValueError('Monitor enablement must be boolean')
        elif not isinstance(value, list) or len(value) > 16:
            raise ValueError('Monitor supports at most 16 literal paths per type')
        else:
            for item in value:
                path_value(item)
            if len(set(value)) != len(value):
                raise ValueError('Duplicate monitor input')
    if sum(len(v) for v in cfg['monitors'].values() if isinstance(v, list)) > 48:
        raise ValueError('Security monitor input quota exceeded')
    for key, low, high in (
        ('ssh_threshold', 2, 10000), ('ssh_window_seconds', 10, 3600), ('cooldown_seconds', 10, 86400),
        ('certificate_days', 1, 365), ('poll_seconds', 5, 3600), ('max_records', 10, 5000),
        ('max_evidence_bytes', 100_000, 100_000_000), ('max_read_bytes', 4096, 8_000_000),
        ('max_tick_bytes', 4096, 32_000_000), ('cve_max_age_days', 1, 90),
    ):
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError('Invalid bounded security limit: ' + key)
    cfg['scan_ports'] = ports(cfg['scan_ports'])
    if not isinstance(cfg['targets'], list) or len(cfg['targets']) > 32:
        raise ValueError('Explicit bounded IP/CIDR target allowlist required')
    for value in cfg['targets']:
        if not isinstance(value, str) or '%' in value:
            raise ValueError('Scope uses literal IP/CIDR, not DNS, URLs, zone IDs or patterns')
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError:
            raise ValueError('Scope requires canonical literal IP/CIDR') from None
        if network.num_addresses > 256 or network.is_multicast or network.network_address.is_unspecified:
            raise ValueError('Scope must be narrow (at most 256 addresses), never a wildcard')
    if not isinstance(cfg['response_accounts'], list) or len(cfg['response_accounts']) > 32:
        raise ValueError('Explicit response account allowlist required')
    for value in cfg['response_accounts']:
        literal(value, r'[a-z_][a-z0-9_-]{0,31}', 'response account')
        if value == 'root':
            raise ValueError('Disabling root is not a supported response proposal')
    if not isinstance(cfg['account_state_files'], dict) or len(cfg['account_state_files']) > 32:
        raise ValueError('Bounded operator account-expiry snapshots required')
    for account, value in cfg['account_state_files'].items():
        if account not in cfg['response_accounts']:
            raise ValueError('Account state snapshot requires a response_accounts entry')
        path_value(value)
    if not isinstance(cfg['rotation_paths'], dict) or len(cfg['rotation_paths']) > 16:
        raise ValueError('Bounded operator key aliases required')
    for key, value in cfg['rotation_paths'].items():
        literal(key, r'[a-z][a-z0-9_-]{0,39}', 'rotation alias')
        if not isinstance(value, dict) or set(value) != {'current', 'replacement'}:
            raise ValueError('Key rotation requires explicit current/replacement public authorized_keys paths')
        for item in value.values():
            path_value(item)
        if value['current'] == value['replacement']:
            raise ValueError('Key rotation replacement must be a different file')
    if not isinstance(cfg['analysis_roles'], list) or not 1 <= len(cfg['analysis_roles']) <= 10:
        raise ValueError('Select explicit existing defensive analysis roles')
    for value in cfg['analysis_roles']:
        literal(value, r'[a-z][a-z0-9-]{0,79}', 'analysis role')
    if cfg['analysis_agent']:
        literal(cfg['analysis_agent'], r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}', 'analysis agent')
    elif not isinstance(cfg['analysis_agent'], str):
        raise ValueError('analysis_agent must name an existing configured agent')
    if cfg['speech_enabled'] and not cfg['hud_enabled']:
        raise ValueError('Spoken security alerts also require the local HUD alert record')
    from .store import clean
    if clean(workspace, cfg) != cfg:
        raise ValueError('Credentials must not be stored in security.yaml')
    return cfg


def require(workspace, flag='enabled'):
    cfg = load(workspace)
    if not cfg['enabled'] or not cfg[flag]:
        raise ValueError('Defensive security capability is OFF in operator security.yaml: ' + flag)
    return cfg


def digest(cfg):
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, allow_nan=False).encode()).hexdigest()
