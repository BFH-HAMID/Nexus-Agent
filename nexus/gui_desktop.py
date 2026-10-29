"""Review-first desktop setup, reusing the original policy validators/services.

Reads and saves do not connect MCP, collect context or start Telegram polling.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

from .github_safety import scan_text

KINDS = {'context', 'telegram', 'mcp'}


def target(state, kind):
    if kind not in KINDS:
        raise ValueError('Unknown setup policy')
    path = state.workspace / (kind + '.yaml')
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size > 64000)):
        raise ValueError('Unsafe setup policy file')
    return path


def revision(path):
    return hashlib.sha256(path.read_bytes() if path.exists() else b'').hexdigest()


def validate(root, kind, value):
    if not isinstance(value, dict) or len(json.dumps(value).encode()) > 60000:
        raise ValueError('Bounded policy object required')
    scan_text(json.dumps(value), workspace=root)
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        (directory / (kind + '.yaml')).write_text(yaml.safe_dump(value), encoding='utf-8')
        if kind == 'context':
            from .context.core import settings
            return settings(directory)
        if kind == 'telegram':
            from .telegram.bot import settings
            return settings(directory)
        from .extensions.catalog import Catalog, validate_server
        from .permissions import Permissions
        # Validate declarations even when disabled; discovery never executes tools.
        servers = value.get('servers', {})
        if not isinstance(servers, dict) or len(servers) > 10:
            raise ValueError('At most ten MCP servers')
        for name, data in servers.items():
            validate_server(root, name, data)
        plugin = Permissions(root).path(value.get('plugin_directory', 'plugins'))
        if plugin == root or plugin.is_symlink():
            raise ValueError('Narrow plugin directory required')
        # Validate top-level schema in isolation, after resolving the real plugin
        # directory above. Do not reinterpret an existing absolute workspace path
        # relative to the temporary validation root or load its manifests here.
        (directory / "mcp.yaml").write_text(yaml.safe_dump({**value, "plugin_directory": "plugins"}), encoding="utf-8")
        Catalog(directory).load()
        return {'schema': 1, 'enabled': False, 'servers': {}, 'plugin_directory': 'plugins', **value}


def read(state, kind):
    path = target(state, kind)
    raw = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
    return validate(state.workspace, kind, raw or {})


def snapshot(state):
    from .gui_telegram import status
    result = {'telegram_runtime': status(state), 'policies': {}, 'platform': sys.platform, 'demo': state.demo,
              'credentials': {k: bool(os.environ.get(k)) for k in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_APPROVAL_PIN')},
              'notice': 'Policy only. No context collected, bot polling started or MCP tool connected.'}
    with state.lock:
        for kind in sorted(KINDS):
            try:
                cfg = read(state, kind)
                result['policies'][kind] = {'settings': cfg, 'revision': revision(target(state, kind))}
            except Exception:
                result['policies'][kind] = {'error': 'Policy invalid, private or unavailable; inspect it locally. Contents withheld.'}
    return result


def action(state, payload):
    if state.demo:
        raise ValueError('Desktop setup requires live mode')
    operation = payload.get('operation')
    if operation in {'telegram-start', 'telegram-stop'}:
        from .gui_telegram import runtime
        expected = 'START TELEGRAM POLLING' if operation == 'telegram-start' else 'STOP TELEGRAM POLLING'
        if payload.get('confirmation') != expected:
            raise ValueError('Explicit polling lifecycle confirmation required')
        with state.lock:
            if operation == 'telegram-stop':
                return runtime(state).stop()
            cfg = read(state, 'telegram')
            if not cfg['enabled'] or payload.get('revision') != revision(target(state, 'telegram')):
                raise ValueError('Review current enabled Telegram policy before starting')
            return runtime(state).start()
    if operation in {'context-collect', 'context-clear'}:
        from .context.core import Context
        expected = 'COLLECT LOCAL CONTEXT' if operation == 'context-collect' else 'CLEAR LOCAL CONTEXT'
        if payload.get('confirmation') != expected:
            raise ValueError('Explicit one-time context consent required')
        with state.lock:
            if operation == 'context-collect':
                cfg = read(state, 'context')
                if not cfg['enabled'] or payload.get('revision') != revision(target(state, 'context')):
                    raise ValueError('Review current enabled context policy before collecting')
            context = Context(state.workspace, state.workbench.studio)
            return context.collect() if operation == 'context-collect' else context.clear()
    if operation in {'service-preview', 'service-install'}:
        from .jarvis.services import install, manifest
        platform = payload.get('platform', '')
        user = payload.get('user', '')
        if not isinstance(user, str) or len(user) > 200 or platform not in {'windows', 'systemd'}:
            raise ValueError('Choose a supported service platform and bounded OS user')
        data = manifest(state.workspace, platform, user)
        confirmation = 'INSTALL SERVICE ' + data['filename'] + ' ' + hashlib.sha256(data['content'].encode()).hexdigest()[:16]
        if operation == 'service-preview':
            return {**data, 'confirmation': confirmation, 'platform': platform,
                    'note': 'Preview only. Installation is a separate explicit OS change; no elevation or password handling.'}
        if payload.get('confirmation') != confirmation:
            raise ValueError('Review current manifest and exact installation confirmation')
        with state.lock:
            return install(state.workspace, platform, user, confirmation=confirmation)
    if operation != 'save' or set(payload) - {'operation', 'kind', 'revision', 'settings', 'confirmation'}:
        raise ValueError('Unknown desktop setup action')
    kind = payload.get('kind')
    path = target(state, kind)
    if payload.get('confirmation') != 'SAVE ' + kind.upper() + ' POLICY':
        raise ValueError('Exact policy consent required')
    with state.lock:
        if payload.get('revision') != revision(path):
            raise ValueError('Policy changed; reload and review again')
        cfg = validate(state.workspace, kind, payload.get('settings'))
        fd, name = tempfile.mkstemp(prefix='.desktop-policy-', dir=state.workspace)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                yaml.safe_dump(cfg, file, sort_keys=False, allow_unicode=True)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    return {'saved': True, 'notice': 'Policy saved. No collector/bot/tool started. Existing approvals still apply.'}


def dispatch(state, payload=None):
    try:
        return snapshot(state) if payload is None else action(state, payload)
    except Exception:
        # Parser/provider/OS errors may contain private config, URLs or credentials.
        raise ValueError('Desktop setup refused. Check policy schema, revision, exact consent and target OS. No private contents exposed.') from None
