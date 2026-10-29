"""Reviewed workspace configuration edits using existing YAML layers, never keys/KBs."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .config import clear_yaml_cache, load_config
from .providers import OPENAI_COMPAT_KINDS
from .redaction import mask, secrets
from .types import sanitize_role_name

FILES = {'agent': 'agents.yaml', 'role': 'roles.d/zz-gui-managed.yaml', 'team': 'pipelines.yaml'}
AGENT_FIELDS = ('provider', 'model', 'base_url', 'api_key_env', 'max_tokens', 'temperature', 'timeout',
                'supports_tools', 'tool_mode', 'fallback', 'optional', 'stream', 'label', 'description')


class Configuration:
    def __init__(self, workbench):
        self.wb = workbench
        self.root = workbench.workspace

    def path(self, kind):
        path = self.root / FILES[kind]
        for p in (path.parent, path):
            if p.is_symlink():
                raise ValueError('Configuration symlinks cannot be edited')
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('Configuration path escapes workspace')
        return path

    def document(self, kind):
        path = self.path(kind)
        if path.exists() and (not path.is_file() or path.stat().st_size > 2_000_000):
            raise ValueError('Configuration must be a bounded YAML file')
        data = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
        if data is not None and not isinstance(data, dict):
            raise ValueError('Configuration must be a YAML mapping')
        return data or {}

    def revision(self):
        h = hashlib.sha256()
        for kind in FILES:
            self.document(kind)
            path = self.path(kind)
            h.update(FILES[kind].encode())
            h.update(path.read_bytes() if path.exists() else b'')
        return h.hexdigest()

    def snapshot(self):
        with self.wb.lock:
            revision = self.revision()  # Refuse symlinks/oversized files before the loader reads them.
            cfg = load_config(self.root)
            agents = {name: {k: getattr(a, k) for k in AGENT_FIELDS} for name, a in cfg.agents.items()}
            roles = {}
            for name, role in cfg.roles.roles.items():
                row = asdict(role)
                row['instructions'] = row.pop('extra_instructions')
                row.pop('source')
                row.pop('name')
                roles[name] = row
            data = {'revision': revision, 'agents': agents, 'roles': roles,
                    'teams': {n: {'description': p.description, 'steps': p.steps} for n, p in cfg.pipelines.items()},
                    'overrides': {k: list(self.document(k).get({'agent': 'agents', 'role': 'roles', 'team': 'pipelines'}[k], {})) for k in FILES},
                    'default_agent': cfg.default_agent}
            values = ([self.wb.state.token] if self.wb.state.token else []) + secrets(self.root) + [a.api_key for a in cfg.agents.values() if a.api_key and a.api_key != 'ollama']
            def clean(value):
                if isinstance(value, str):
                    return mask(value, values)
                if isinstance(value, list):
                    return [clean(v) for v in value]
                if isinstance(value, dict):
                    return {k: clean(v) for k, v in value.items()}
                return value
            return clean(data)

    @staticmethod
    def text(value, limit=16000):
        if not isinstance(value, str) or len(value) > limit or '${' in value or '[REDACTED' in value or '\x00' in value:
            raise ValueError('Invalid text, environment interpolation or redacted secret placeholder')
        return value

    def validate(self, kind, row, cfg):
        if not isinstance(row, dict):
            raise ValueError('Configuration entry must be an object')
        if kind == 'agent':
            if set(row) - set(AGENT_FIELDS):
                raise ValueError('Unknown agent field; raw API keys are never accepted here')
            for field in ('provider', 'model'):
                self.text(row.get(field), 300)
            if row['provider'] not in OPENAI_COMPAT_KINDS | {'anthropic'} or not row['model'].strip():
                raise ValueError('Choose a supported provider and nonempty model ID')
            endpoint = row.get('base_url')
            if endpoint:
                self.text(endpoint, 1000)
                url = urlparse(endpoint)
                if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
                    raise ValueError('Endpoint must be HTTP(S), without credentials/query/fragment')
                if url.port is not None and not 1 <= url.port <= 65535:
                    raise ValueError("Invalid endpoint port")
            env = row.get('api_key_env')
            if env is not None:
                self.text(env, 100)
            if env in {'GITHUB_TOKEN', 'NEXUS_SERVE_TOKEN', 'NEXUS_HUD_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_APPROVAL_PIN'}:
                raise ValueError('GUI/GitHub/Telegram credentials cannot be used as model credentials')
            if env and not re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', self.text(env, 100)):
                raise ValueError('API key environment variable name is invalid')
            self.text(row.get('tool_mode', 'native'), 30)
            if row.get('tool_mode', 'native') not in {'native', 'text', 'none'}:
                raise ValueError('Invalid tool mode')
            for field in ('supports_tools', 'stream', 'optional'):
                if field in row and type(row[field]) is not bool:
                    raise ValueError('Agent switches must be boolean')
            for field, low, high in [('max_tokens', 1, 1_000_000), ('timeout', 1, 3600), ('temperature', 0, 2)]:
                value = row.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high):
                    raise ValueError('Invalid token, timeout or temperature limit')
            if 'max_tokens' in row and type(row['max_tokens']) is not int:
                raise ValueError('max_tokens must be an integer')
            for field in ('label', 'description'):
                if field in row:
                    self.text(row[field], 2000)
            if row.get('fallback') is not None:
                self.text(row['fallback'], 80)
            if row.get('fallback') and row['fallback'] not in cfg.agents:
                raise ValueError('Unknown fallback agent')
        elif kind == 'role':
            allowed = {'title', 'category', 'agent', 'persona', 'instructions', 'keywords', 'aliases', 'tools', 'focus', 'model', 'temperature'}
            self.text(row.get('agent'), 80)
            if set(row) - allowed or row.get('agent') not in cfg.agents:
                raise ValueError('Unknown role field or agent')
            for field in ('title', 'category', 'persona', 'instructions'):
                if field in row:
                    self.text(row[field])
            if not row.get('persona', '').strip():
                raise ValueError('A role needs a nonempty persona')
            for field in ('aliases', 'tools', 'focus', 'keywords'):
                value = row.get(field)
                if value is None and field == 'tools':
                    continue
                if value is not None:
                    if not isinstance(value, list) or len(value) > 300:
                        raise ValueError('Role lists are bounded')
                    for item in value:
                        if field == 'keywords' and isinstance(item, dict):
                            self.text(item.get('text', ''), 200)
                            weight = item.get('weight', 1)
                            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or not 0 <= weight <= 100:
                                raise ValueError('Invalid keyword weight')
                        else:
                            self.text(item, 200)
            if row.get('model') is not None:
                self.text(row['model'], 300)
            value = row.get('temperature')
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 2):
                raise ValueError('Invalid role temperature')
        else:
            if set(row) - {'description', 'steps'}:
                raise ValueError('Unknown team field')
            self.text(row.get('description', ''), 2000)
            steps = row.get('steps')
            if not isinstance(steps, list) or not 1 <= len(steps) <= 30:
                raise ValueError('Choose 1–30 ordered team steps')
            for step in steps:
                if not isinstance(step, dict) or set(step) - {'role', 'instruction', 'read_only'} or step.get('role') not in cfg.roles:
                    raise ValueError('Every team member must reference an existing role')
                self.text(step.get('instruction', ''))
                if type(step.get('read_only')) is not bool:
                    raise ValueError('Every team step needs an explicit read-only setting')

    def save(self, payload):
        if not isinstance(payload, dict) or len(json.dumps(payload, allow_nan=False)) > 100_000:
            raise ValueError('Configuration request too large')
        kind, name = payload.get('kind'), payload.get('name')
        if kind not in FILES or not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', name):
            raise ValueError('Choose a lowercase name using letters, digits and hyphens')
        action = payload.get('action', 'save')
        if action not in {'save', 'remove'} or payload.get('confirmation') != f'{action.upper()} {kind.upper()} {name}':
            raise ValueError('Exact reviewed configuration confirmation is required')
        with self.wb.state.lock, self.wb.lock:
            if self.wb.state.demo:
                raise ValueError('Configuration edits require live mode')
            if payload.get('revision') != self.revision():
                raise ValueError('Configuration changed; reload before saving')
            cfg = load_config(self.root)
            if kind == 'role' and name not in cfg.roles.roles and name in cfg.roles:
                raise ValueError('An equivalent role already exists under this alias; edit its canonical name')
            doc = self.document(kind)
            section = {'agent': 'agents', 'role': 'roles', 'team': 'pipelines'}[kind]
            if not isinstance(doc.get(section, {}), dict):
                raise ValueError('This YAML layer needs a mapping before GUI editing')
            entries = doc.setdefault(section, {})
            if action == 'remove':
                if name not in entries:
                    raise ValueError('No workspace override exists; built-in entries are not deleted')
                # Never strand persisted role mappings or team dependencies.
                if kind == 'agent' and (cfg.default_agent == name or cfg.settings.router_agent == name or any(r.agent == name for r in cfg.roles.roles.values()) or any(a.fallback == name for a in cfg.agents.values()) or name in self.wb.settings()['role_models'].values()):
                    raise ValueError('Reassign referenced/default agents before removing their workspace entry')
                if kind == 'role' and (name in self.wb.settings()['role_models'] or any(s.get('role') in cfg.roles and cfg.roles.get(s['role']).name == name for p in cfg.pipelines.values() for s in p.steps)):
                    raise ValueError('Remove team/model references before removing this role override')
                del entries[name]
            else:
                row = payload.get('entry')
                self.validate(kind, row, cfg)
                if kind == 'role':
                    for alias in row.get('aliases', []):
                        clean = sanitize_role_name(alias)
                        if not clean or (clean in cfg.roles and cfg.roles.get(clean).name != name):
                            raise ValueError('Role alias conflicts with an existing role; edit the canonical role instead')
                # Preserve unexposed existing fields/inline credentials, never send them to the UI.
                entries[name] = {**entries.get(name, {}), **row}
                if kind == 'agent' and payload.get('set_default'):
                    if payload['set_default'] is not True:
                        raise ValueError('set_default must be boolean')
                    doc['default_agent'] = name
                if kind == 'agent':
                    seen = {name}
                    current = row.get('fallback')
                    while current:
                        if current in seen:
                            raise ValueError('Fallback agents must not form a cycle')
                        seen.add(current)
                        current = cfg.agents[current].fallback if current in cfg.agents else None
            path = self.path(kind)
            before = path.read_bytes() if path.exists() else None
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.gui-config-', dir=path.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                    yaml.safe_dump(doc, handle, allow_unicode=True, sort_keys=False)
                os.replace(temporary, path)
                clear_yaml_cache()
                try:
                    updated = load_config(self.root)
                    self.wb.state.config = updated
                except Exception:
                    if before is None:
                        path.unlink()
                    else:
                        path.write_bytes(before)
                    clear_yaml_cache()
                    raise ValueError('Configuration refused; previous file restored') from None
            finally:
                Path(temporary).unlink(missing_ok=True)
            return {'saved': name, 'kind': kind, 'action': action, 'revision': self.revision(),
                    'applies_to': 'New runs; existing approvals, permissions and local-only policy remain enforced'}
