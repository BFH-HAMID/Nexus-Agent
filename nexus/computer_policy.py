"""Strict operator-owned scopes for environment control. Importing this does no IO."""
from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

DESKTOP_ACTIONS = {'open', 'close', 'focus', 'type', 'hotkey', 'click', 'screenshot', 'move', 'scroll'}
CONTROL_TOOLS = {'desktop_action', 'file_action', 'workflow_run', 'home_action'}
MACRO_TOOLS = CONTROL_TOOLS - {'workflow_run'} | {
    'browser_action', 'office_file', 'vscode_open', 'run_command', 'git', 'gh',
    'read_file', 'list_files', 'grep', 'write_file', 'edit_file', 'delete_file',
}
IDENT = re.compile(r'[a-z][a-z0-9_-]{0,63}')
SERVICES = {
    'light': {'turn_on', 'turn_off', 'toggle'}, 'switch': {'turn_on', 'turn_off', 'toggle'},
    'fan': {'turn_on', 'turn_off', 'toggle'}, 'climate': {'turn_on', 'turn_off'},
    'lock': {'lock', 'unlock'}, 'alarm_control_panel': {'alarm_arm_home', 'alarm_arm_away', 'alarm_disarm'},
    'cover': {'open_cover', 'close_cover', 'stop_cover'}, 'notify': {'notify'},
}


def mapping(value, keys, label):
    if not isinstance(value, dict) or set(value) - set(keys):
        raise ValueError('Invalid ' + label + ' configuration/fields')
    return value


def flag(value, label):
    if type(value) is not bool:
        raise ValueError(label + ' requires a boolean opt-in')
    return value


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label} must be an integer in {low}..{high}')
    return value


def text(value, limit=300, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or (not value and not empty)
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('Bounded plain text required')
    return value


def identifier(value):
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise ValueError('Lowercase bounded identifier required')
    return value


def relative(value, *, narrow=False):
    text(value, 400)
    path = PurePosixPath(value)
    if (path.is_absolute() or PureWindowsPath(value).drive or '\\' in value or '..' in path.parts
            or (narrow and str(path) == '.') or str(path) != value):
        raise ValueError('Canonical workspace-relative path required')
    if any(p.lower() in {'.git', '.nexus', '.env', '.venv', '.ssh', '.aws', 'node_modules'} for p in path.parts):
        raise ValueError('Private/runtime scope refused')
    return value


def origin(value):
    text(value, 300)
    p = urlsplit(value)
    host = f'[{p.hostname}]' if p.hostname and ':' in p.hostname else p.hostname
    if (p.scheme not in {'http', 'https'} or not host or p.username is not None or p.password is not None or p.path or p.query
            or p.fragment or p.port is None or not 1 <= p.port <= 65535 or value != f'{p.scheme}://{host}:{p.port}'):
        raise ValueError('Use canonical exact scheme://host:port origins without credentials')
    return value


def url_origin(value):
    text(value, 2048)
    p = urlsplit(value)
    if p.scheme not in {'http', 'https'} or not p.hostname or p.username is not None or p.password is not None or '\\' in value:
        raise ValueError('Credential-free HTTP(S) URL required')
    host = f'[{p.hostname}]' if ':' in p.hostname else p.hostname
    port = p.port if p.port is not None else (443 if p.scheme == 'https' else 80)
    if not 1 <= port <= 65535:
        raise ValueError('Invalid URL port')
    return f'{p.scheme}://{host}:{port}'


def strings(values, limit, check=text):
    if not isinstance(values, list) or len(values) > limit:
        raise ValueError('Bounded allowlist required')
    checked = [check(v) for v in values]
    if len(set(checked)) != len(checked):
        raise ValueError('Duplicate allowlist entries')
    return checked


def desktop(value):
    value = mapping(value, {'enabled', 'backend', 'apps'}, 'desktop')
    cfg = {'enabled': False, 'backend': 'auto', 'apps': {}, **value}
    flag(cfg['enabled'], 'desktop')
    if cfg['backend'] not in ('auto', 'xdotool', 'pywinauto'):
        raise ValueError('Desktop backend must be auto, xdotool or pywinauto')
    if not isinstance(cfg['apps'], dict) or len(cfg['apps']) > 30:
        raise ValueError('At most 30 explicitly allowed apps')
    apps = {}
    for key, app in cfg['apps'].items():
        identifier(key)
        mapping(app, {'executable', 'args', 'window_class', 'actions'}, 'desktop app')
        executable = text(app.get('executable'), 500)
        if not (PurePosixPath(executable).is_absolute() or PureWindowsPath(executable).is_absolute()):
            raise ValueError('An absolute trusted app executable is required; no shell/launcher strings')
        argv = app.get('args', [])
        if not isinstance(argv, list) or len(argv) > 20:
            raise ValueError('Bounded fixed launch argv required')
        actions = strings(app.get('actions', []), len(DESKTOP_ACTIONS))
        if set(actions) - DESKTOP_ACTIONS:
            raise ValueError('Unsupported desktop action')
        apps[key] = {'executable': executable, 'args': [text(a, 500, empty=True) for a in argv],
                     'window_class': text(app.get('window_class', ''), 120, empty=True), 'actions': actions}
    return {**cfg, 'apps': apps}


def files(value):
    value = mapping(value, {'enabled', 'roots', 'max_files', 'max_bytes'}, 'files')
    cfg = {'enabled': False, 'roots': [], 'max_files': 200, 'max_bytes': 20_000_000, **value}
    flag(cfg['enabled'], 'files')
    strings(cfg['roots'], 8, relative)
    integer(cfg['max_files'], 1, 500, 'File count')
    integer(cfg['max_bytes'], 1, 50_000_000, 'File byte budget')
    return cfg


def workflows(value):
    value = mapping(value, {'enabled', 'voice', 'directory', 'approved', 'allowed_tools', 'max_steps'}, 'workflows')
    cfg = {'enabled': False, 'voice': False, 'directory': 'computer-macros', 'approved': {},
           'allowed_tools': [], 'max_steps': 30, **value}
    for key in ('enabled', 'voice'):
        flag(cfg[key], 'workflows.' + key)
    relative(cfg['directory'], narrow=True)
    if not isinstance(cfg['approved'], dict) or len(cfg['approved']) > 100:
        raise ValueError('Bounded operator-reviewed macro digest map required')
    for name, digest in cfg['approved'].items():
        identifier(name)
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('Every reviewed macro requires its SHA-256 in permissions.yaml')
    if set(strings(cfg['allowed_tools'], 30)) - MACRO_TOOLS:
        raise ValueError('Unsupported macro tool (nested replay/permission changes are forbidden)')
    integer(cfg['max_steps'], 1, 50, 'Macro steps')
    return cfg


def browser_options(value):
    value = mapping(value, {'enabled', 'forms', 'downloads', 'download_dir', 'max_download_bytes', 'search_url'}, 'browser_controls')
    cfg = {'enabled': False, 'forms': False, 'downloads': False, 'download_dir': 'browser-downloads',
           'max_download_bytes': 2_000_000, 'search_url': '', **value}
    for key in ('enabled', 'forms', 'downloads'):
        flag(cfg[key], 'browser_controls.' + key)
    relative(cfg['download_dir'], narrow=True)
    integer(cfg['max_download_bytes'], 1, 20_000_000, 'Download bytes')
    text(cfg['search_url'], 1800, empty=True)
    if cfg['search_url']:
        if cfg['search_url'].count('{query}') != 1 or '{' in cfg['search_url'].replace('{query}', '') or '}' in cfg['search_url'].replace('{query}', ''):
            raise ValueError('Search URL requires exactly one literal {query} placeholder')
        url_origin(cfg['search_url'].replace('{query}', 'query'))
    return cfg


def smart_home(value):
    value = mapping(value, {'enabled', 'voice', 'home_assistant', 'mqtt', 'voice_commands'}, 'smart_home')
    cfg = {'enabled': False, 'voice': False, 'home_assistant': {}, 'mqtt': {}, 'voice_commands': {}, **value}
    flag(cfg['enabled'], 'smart_home')
    flag(cfg['voice'], 'smart_home.voice')
    ha = mapping(cfg['home_assistant'], {'enabled', 'url', 'devices'}, 'Home Assistant')
    ha = {'enabled': False, 'url': '', 'devices': {}, **ha}
    flag(ha['enabled'], 'Home Assistant')
    if ha['url']:
        origin(ha['url'])
        p = urlsplit(ha['url'])
        if p.scheme != 'https' and p.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('Home Assistant requires HTTPS or exact loopback HTTP')
    if ha['enabled'] and not ha['url']:
        raise ValueError('Explicit Home Assistant URL required')
    if not isinstance(ha['devices'], dict) or len(ha['devices']) > 100:
        raise ValueError('Bounded Home Assistant device allowlist required')
    devices = {}
    for name, device in ha['devices'].items():
        identifier(name)
        mapping(device, {'entity', 'services'}, 'Home Assistant device')
        entity = text(device.get('entity'), 180)
        if not re.fullmatch(r'[a-z_]+\.[a-z0-9_]+', entity) or entity.split('.')[0] not in SERVICES:
            raise ValueError('Unsupported Home Assistant entity/domain')
        allowed = strings(device.get('services', []), 10)
        if set(allowed) - SERVICES[entity.split('.')[0]]:
            raise ValueError('Unsupported Home Assistant service')
        devices[name] = {'entity': entity, 'services': allowed}
    ha['devices'] = devices
    mqtt = mapping(cfg['mqtt'], {'enabled', 'host', 'port', 'devices'}, 'MQTT')
    mqtt = {'enabled': False, 'host': '', 'port': 8883, 'devices': {}, **mqtt}
    flag(mqtt['enabled'], 'MQTT')
    text(mqtt['host'], 253, empty=True)
    if mqtt['host'] and not re.fullmatch(r'[A-Za-z0-9.:-]{1,253}', mqtt['host']):
        raise ValueError('MQTT host only, not a URL or credentials')
    if mqtt['enabled'] and not mqtt['host']:
        raise ValueError('Explicit MQTT host required')
    integer(mqtt['port'], 1, 65535, 'MQTT TLS port')
    if not isinstance(mqtt['devices'], dict) or len(mqtt['devices']) > 100:
        raise ValueError('Bounded MQTT device map required')
    devices = {}
    for name, device in mqtt['devices'].items():
        identifier(name)
        mapping(device, {'topic', 'commands', 'kind'}, 'MQTT device')
        topic = text(device.get('topic'), 300)
        if any(c in topic for c in '#+') or topic.startswith('$'):
            raise ValueError('Exact MQTT topics only; no prefixes or wildcards')
        kind = device.get('kind', 'other')
        if kind not in ('light', 'switch', 'lock', 'alarm', 'garage', 'other'):
            raise ValueError('Unknown MQTT device kind')
        commands = device.get('commands', {})
        if not isinstance(commands, dict) or not 1 <= len(commands) <= 20:
            raise ValueError('MQTT requires bounded operator-fixed command payloads')
        for key, message in commands.items():
            identifier(key)
            text(message, 2000)
        devices[name] = {'topic': topic, 'commands': dict(commands), 'kind': kind}
    mqtt['devices'] = devices
    aliases = cfg['voice_commands']
    if not isinstance(aliases, dict) or len(aliases) > 100:
        raise ValueError('Bounded voice command map required')
    for phrase, command in aliases.items():
        text(phrase, 100)
        if phrase != ' '.join(phrase.casefold().split()) or phrase.startswith('run my '):
            raise ValueError('Use exact lowercase voice phrases; macro phrases are reserved')
        mapping(command, {'adapter', 'device', 'action'}, 'voice command')
        adapter = command.get('adapter')
        if adapter not in ('home_assistant', 'mqtt'):
            raise ValueError('Unknown voice device adapter')
        identifier(command.get('device'))
        identifier(command.get('action'))
        device = (ha if adapter == 'home_assistant' else mqtt)['devices'].get(command.get('device'))
        if not device or command.get('action') not in device.get('services', device.get('commands', {})):
            raise ValueError('Voice command must select an exact allowed device action')
        if command.get('action') == 'notify':
            raise ValueError('Phone notifications require an explicit message and separate review')
    return {**cfg, 'home_assistant': ha, 'mqtt': mqtt}


def extensions(value):
    return {'desktop': desktop(value.get('desktop', {})), 'files': files(value.get('files', {})),
            'workflows': workflows(value.get('workflows', {})),
            'browser_controls': browser_options(value.get('browser_controls', {})),
            'smart_home': smart_home(value.get('smart_home', {}))}
