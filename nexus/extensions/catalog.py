"""Strict operator catalog. Files describe capabilities; discovery executes nothing."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..github_safety import scan_text
from ..jarvis.sources import bounded
from ..permissions import Permissions

IDENT = re.compile(r'[a-z][a-z0-9_]{0,23}')


def validate_server(root, name, data):
    if not isinstance(name, str) or not IDENT.fullmatch(name) or not isinstance(data, dict):
        raise ValueError('Invalid extension identity')
    keys = {'transport', 'command', 'url', 'env', 'headers', 'tools', 'timeout_seconds', 'rate_per_minute', 'max_calls_day'}
    if set(data) - keys:
        raise ValueError('Unknown extension fields')
    cfg = {'timeout_seconds': 10, 'rate_per_minute': 5, 'max_calls_day': 20, 'env': {}, 'headers': {}, **data}
    for key, low, high in [('timeout_seconds', 1, 30), ('rate_per_minute', 1, 30), ('max_calls_day', 1, 100)]:
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError('Invalid extension budget')
    if cfg.get('transport') == 'stdio':
        command = cfg.get('command')
        if (not isinstance(command, list) or not 1 <= len(command) <= 12 or any(not isinstance(a, str) or not 0 < len(a) <= 1000
                or any(c in a for c in '\n\r\x00') for a in command) or 'url' in cfg or cfg['headers']):
            raise ValueError('Explicit stdio argv required; no shell, URLs or headers')
        cfg['command'] = [sys.executable if command[0] == '@python' else command[0], *command[1:]]
        if not Path(cfg['command'][0]).is_absolute() or not Path(cfg['command'][0]).is_file():
            raise ValueError('Absolute installed executable or @python required')
        scan_text(json.dumps(command), workspace=root)
    elif cfg.get('transport') == 'http':
        url = cfg.get('url')
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (not parsed or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.scheme not in ('http', 'https') or 'command' in cfg or cfg['env']
                or (parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', '::1', 'localhost'))):
            raise ValueError('Explicit HTTPS endpoint or loopback HTTP required; no URL credentials/query/redirects')
        scan_text(url, workspace=root)
    else:
        raise ValueError('Unknown MCP transport')
    for key in ('env', 'headers'):
        refs = cfg[key]
        if not isinstance(refs, dict) or len(refs) > 10:
            raise ValueError('Bounded secret references required')
        for target, ref in refs.items():
            if (not isinstance(target, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', target)
                    or not isinstance(ref, dict) or set(ref) not in ({'env'}, {'keychain'})
                    or not isinstance(next(iter(ref.values())), str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', next(iter(ref.values())))):
                raise ValueError('Only named env/keychain references allowed, never inline credentials')
            if key == 'headers' and target.lower() not in ('authorization', 'x-api-key'):
                raise ValueError('Only explicit authorization headers allowed')
            if key == 'env' and target.upper() in {'PATH', 'PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'NODE_OPTIONS'}:
                raise ValueError('Cannot inject interpreter/library search settings')
    tools = cfg.get('tools')
    if not isinstance(tools, list) or not 1 <= len(tools) <= 20:
        raise ValueError('Declare 1–20 tools for human review before connecting')
    seen = set()
    for tool in tools:
        if (not isinstance(tool, dict) or set(tool) != {'name', 'description', 'inputSchema'}
                or not isinstance(tool['name'], str) or not IDENT.fullmatch(tool['name']) or tool['name'] in seen
                or not isinstance(tool['description'], str) or not 0 < len(tool['description']) <= 500):
            raise ValueError('Invalid declared MCP tool')
        seen.add(tool['name'])
        schema = tool['inputSchema']
        text = json.dumps(schema, allow_nan=False)
        if not isinstance(schema, dict) or schema.get('type') != 'object' or len(text) > 8000 or '"$ref"' in text:
            raise ValueError('Bounded object JSON Schema without references required')
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(schema)
        scan_text(tool['description'], workspace=root)
    scan_text(json.dumps(cfg), workspace=root)
    return cfg


class Catalog:
    def __init__(self, workspace):
        self.root = Path(workspace).resolve()

    def load(self):
        path = self.root / 'mcp.yaml'
        try:
            value = yaml.safe_load(bounded(path, 64000)) if path.exists() else {}
        except yaml.YAMLError:
            raise ValueError('Invalid MCP YAML') from None
        if not isinstance(value, dict) or set(value) - {'schema', 'enabled', 'servers', 'plugin_directory'}:
            raise ValueError('Unknown MCP policy')
        if type(value.get('schema', 1)) is not int or value.get('schema', 1) != 1 or type(value.get('enabled', False)) is not bool:
            raise ValueError('Explicit MCP schema/enable flag required')
        if not value.get('enabled', False):
            return {}
        servers = value.get('servers', {})
        if not isinstance(servers, dict) or len(servers) > 10:
            raise ValueError('At most ten configured servers')
        directory = Permissions(self.root).path(value.get('plugin_directory', 'plugins'))
        if directory == self.root or directory.is_symlink():
            raise ValueError('Narrow plugin manifest directory required')
        entries = dict(servers)
        files = sorted(directory.glob('*.mcp.json')) if directory.is_dir() else []
        if len(files) > 20:
            raise ValueError('At most twenty plugin manifests')
        for file in files:
            manifest = json.loads(bounded(file, 64000))
            if not isinstance(manifest, dict) or set(manifest) != {'name', 'server'} or manifest['name'] in entries:
                raise ValueError('Unique plugin manifest identity required')
            entries[manifest['name']] = manifest['server']
        result = {}
        for name, raw in entries.items():
            cfg = validate_server(self.root, name, raw)
            code_files = {}
            for argument in cfg.get('command', []):
                candidate = (self.root / argument).resolve()
                if candidate.is_relative_to(self.root) and candidate.is_file():
                    code_files[str(candidate.relative_to(self.root))] = hashlib.sha256(bounded(candidate, 1_000_000)).hexdigest()
            digest = hashlib.sha256(json.dumps({'config': cfg, 'code_files': code_files}, sort_keys=True).encode()).hexdigest()
            result[name] = {'name': name, 'config': cfg, 'digest': digest, 'code_files': code_files}
        return result
