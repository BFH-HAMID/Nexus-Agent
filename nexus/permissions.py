"""Module 3 foundation: reloadable workspace policy at the existing ApprovalGate.

A permission decision is not an OS sandbox. Shell/code execution always needs a
fresh human decision; policy cannot grant absent tools or bypass Studio policy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import time
from pathlib import Path

import yaml

MODES = {'READ_ONLY', 'ASK_EVERY_TIME', 'AUTO_ALLOW'}
# Tool families that always take a fresh decision, whatever the mode says.
FORCED_REVIEW = frozenset({'run_command', 'git', 'gh', 'git_prepare', 'office_file',
                           'vscode_open', 'browser_action'})


class Permissions:
    def __init__(self, workspace):
        self.root = Path(workspace).resolve()

    def load(self):
        path = self.root / 'permissions.yaml'
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size > 64000)):
            raise ValueError('Permissions require a bounded regular operator-owned YAML file')
        try:
            data = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
        except yaml.YAMLError:
            raise ValueError('Malformed permission YAML') from None
        if (not isinstance(data, dict) or set(data) - {'schema', 'default', 'tools', 'computer'} or type(data.get('schema', 1)) is not int or data.get('schema', 1) != 1
                or not isinstance(data.get('default', 'ASK_EVERY_TIME'), str) or data.get('default', 'ASK_EVERY_TIME') not in MODES):
            raise ValueError('Invalid permissions schema/default')
        rules = data.get('tools', {})
        if (not isinstance(rules, dict) or len(rules) > 200 or any(not isinstance(k, str)
                or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', k) or not isinstance(v, str) or v not in MODES for k, v in rules.items())):
            raise ValueError('Invalid per-tool permission modes')
        from .computer import settings
        return {'default': data.get('default', 'ASK_EVERY_TIME'), 'tools': rules, 'computer': settings(data.get('computer', {}))}

    @staticmethod
    def readonly(spec, args):
        if spec.name == 'git':
            raw = args.get('args', [])
            try:
                parts = shlex.split(raw) if isinstance(raw, str) else list(raw)
            except (TypeError, ValueError):
                return False
            if not parts or any(not isinstance(p, str) for p in parts):
                return False
            if parts[0] in {'status', 'diff', 'log', 'show', 'rev-parse', 'ls-files', 'describe', 'shortlog'}:
                return not any(p.startswith(('--output', '--ext-diff', '--textconv', '--filters', '--exec-path', '--show-signature')) for p in parts[1:])
            if parts[0] == 'config':
                return len(parts) in {2, 3} and parts[1] in {'--get', '--get-all', '--list'}
            if parts[0] in {'branch', 'tag'}:
                return len(parts) == 1 or parts[1:] in [['--list'], ['-l']]
            if parts[0] == 'remote':
                return parts[1:] in [[], ['-v']] or (len(parts) == 3 and parts[1] == 'get-url')
            return False
        if spec.name == 'gh':
            from .tools import fresh_confirmation_required
            return not fresh_confirmation_required(spec, args)
        if spec.name == 'office_file':
            return args.get('action') == 'read'
        return spec.is_readonly_call(args)

    def path(self, value):
        from .config import nexus_home
        from .workbench import safe_path
        path = safe_path(self.root, value)
        if path.is_relative_to(nexus_home().resolve()):
            raise ValueError('Trusted Nexus state is not exposed through workspace file tools')
        return path

    def operator_control(self, path):
        controls = [self.root / name for name in ('permissions.yaml', 'autonomy.yaml', 'context.yaml', 'mcp.yaml', 'telegram.yaml', 'hud.yaml', 'widget.yaml')]
        if any(path == p or (path.exists() and p.exists() and os.path.samefile(path, p)) for p in controls):
            return True
        # Plugin code/manifests are operator-owned, even before enable. File tools
        # must not turn an approved plugin into new code under an unchanged name.
        from .jarvis.sources import bounded
        config = self.root / 'mcp.yaml'
        raw = yaml.safe_load(bounded(config, 64000)) if config.exists() else {}
        if not isinstance(raw, dict):
            raise ValueError('Invalid operator MCP policy')
        directories = {self.path('plugins'), self.path(raw.get('plugin_directory', 'plugins'))}
        for directory in directories:
            if directory == self.root:
                raise ValueError('Narrow operator plugin directory required')
            if path.is_relative_to(directory):
                return True
            if path.exists() and path.stat().st_nlink > 1 and directory.is_dir():
                for index, artifact in enumerate(directory.rglob('*')):
                    if index >= 1000:
                        raise ValueError('Plugin tree exceeds hardlink review budget')
                    if artifact.is_file() and os.path.samefile(path, artifact):
                        return True
        return False

    def evaluate(self, spec, args):
        config = self.load()
        mode = config['tools'].get(spec.name, config['default'])
        readonly = self.readonly(spec, args)
        # Resolve against the actual workspace even if a legacy caller enables allow_outside.
        if spec.name in {'read_file', 'write_file', 'edit_file', 'delete_file', 'undo_file', 'office_file', 'vscode_open', 'list_files', 'grep', 'run_command'}:
            for key in ('path', 'directory', 'cwd'):
                if key in args and args[key] is not None:
                    value = args[key]
                    if not isinstance(value, str):
                        raise ValueError('Expected a bounded workspace-relative path')
                    path = self.path(value or '.')
                    if not readonly and self.operator_control(path):
                        raise ValueError('Permission policy is operator-owned; agent edits are forbidden')
        forced = not readonly and spec.name in FORCED_REVIEW
        return {'mode': mode, 'readonly': readonly, 'forced': forced}

    def audit(self, spec, args, rule, decision):
        """One hash-linked line in the shared trail (see securitycenter.append_event)."""
        from . import securitycenter
        event = {'at': round(time.time(), 3), 'tool': spec.name, 'mode': rule['mode'],
                 'approved': bool(decision.approved), 'reason': decision.reason[:500],
                 'args_sha256': hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest(),
                 'action': 'permission.decision', 'target': spec.name,
                 'result': 'approved' if decision.approved else 'denied',
                 'risk': 'low', 'actor': 'agent', 'detail': {'mode': rule['mode']}}
        securitycenter.append_event(self.root, event)
