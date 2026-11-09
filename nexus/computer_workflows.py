"""Operator-pinned semantic macros, never learned skills or recorded approval grants."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import yaml
from filelock import FileLock

from .computer_actions import check, runtime, safe_local
from .computer_policy import identifier, mapping
from .jarvis.sources import bounded

PARAM = re.compile(r'\$\{([a-z][a-z0-9_]{0,39})\}')
VOICE_RUN = re.compile(r'(?i)^run my ([a-z][a-z0-9 -]{0,63}) routine[.!]?$')


def read_macro(root, cfg, name):
    identifier(name)
    if not cfg['enabled'] or name not in cfg['approved']:
        raise ValueError('Macro is disabled or not pinned to an operator-reviewed SHA-256')
    path = safe_local(root, Path(cfg['directory']) / (name + '.yaml'))
    raw = bounded(path, 64000)
    if hashlib.sha256(raw).hexdigest() != cfg['approved'][name]:
        raise ValueError('Macro changed since operator review; update the pin manually after review')
    try:
        value = yaml.safe_load(raw)
        seen, budget = set(), [0]
        def inspect(item, depth=0):
            budget[0] += 1
            if depth > 12 or budget[0] > 5000:
                raise ValueError('Macro nesting/node budget exceeded')
            if isinstance(item, (dict, list)):
                if id(item) in seen:
                    raise ValueError('YAML aliases/cycles are forbidden in executable macro files')
                seen.add(id(item))
                for child in item.values() if isinstance(item, dict) else item:
                    inspect(child, depth + 1)
        inspect(value)
        if len(json.dumps(value, allow_nan=False)) > 64000:
            raise ValueError('Macro expansion budget exceeded')
    except (yaml.YAMLError, RecursionError, TypeError):
        raise ValueError('Macro must be bounded plain YAML data') from None
    mapping(value, {'schema', 'parameters', 'steps'}, 'macro')
    if type(value.get('schema')) is not int or value['schema'] != 1:
        raise ValueError('Macro schema must be 1')
    return value


def expand(value, parameters, *, depth=0):
    if depth > 12:
        raise ValueError('Macro nesting budget exceeded')
    if isinstance(value, str):
        match = PARAM.fullmatch(value)
        if match:
            if match[1] not in parameters:
                raise ValueError('Unknown macro parameter')
            return parameters[match[1]]
        if '${' in value:
            raise ValueError('Parameters replace whole JSON values only, never interpolate shell/code strings')
        return value
    if isinstance(value, list):
        return [expand(v, parameters, depth=depth + 1) for v in value]
    if isinstance(value, dict):
        if any(not isinstance(k, str) or '${' in k for k in value):
            raise ValueError('Macro object keys cannot be parameterized')
        return {k: expand(v, parameters, depth=depth + 1) for k, v in value.items()}
    if value is None or type(value) in {int, float, bool}:
        return value
    raise ValueError('Macro values must be JSON data')


def plan(ctx, args, computer):
    mapping(args, {'name', 'parameters'}, 'macro request')
    cfg = computer['workflows']
    value = read_macro(ctx.workspace, cfg, args.get('name'))
    definitions = value.get('parameters', {})
    parameters = args.get('parameters', {})
    if (not isinstance(definitions, dict) or len(definitions) > 30 or not isinstance(parameters, dict)
            or set(parameters) != set(definitions)):
        raise ValueError('Supply exactly the declared macro parameters')
    for name, definition in definitions.items():
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', name):
            raise ValueError('Invalid macro parameter name')
        mapping(definition, {'type', 'max_length'}, 'macro parameter')
        kind, item = definition.get('type'), parameters[name]
        if kind == 'string':
            limit = definition.get('max_length', 500)
            if type(limit) is not int or not 1 <= limit <= 4000 or not isinstance(item, str) or len(item) > limit or '\0' in item:
                raise ValueError('Macro string parameter exceeds its declared budget')
        elif kind == 'integer':
            if type(item) is not int or not -1000000 <= item <= 1000000:
                raise ValueError('Macro integer parameter is out of bounds')
        elif kind == 'boolean':
            if type(item) is not bool:
                raise ValueError('Macro boolean parameter required')
        else:
            raise ValueError('Macro parameters are string/integer/boolean data only')
    steps = value.get('steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= cfg['max_steps']:
        raise ValueError('Macro step budget exceeded or no steps')
    result = []
    for step in steps:
        mapping(step, {'tool', 'args'}, 'macro step')
        if step.get('tool') not in cfg['allowed_tools'] or step.get('tool') == 'workflow_run' or not isinstance(step.get('args'), dict):
            raise ValueError('Macro tool is outside the operator allowed_tools ceiling')
        if step['tool'] in {'run_command', 'git', 'gh'} and '${' in json.dumps(step['args']):
            raise ValueError('Shell/Git/gh macro steps must be fixed operator-reviewed commands, not parameters')
        result.append({'tool': step['tool'], 'args': expand(step['args'], parameters)})
    from .github_safety import scan_text
    scan_text(json.dumps(result), workspace=ctx.workspace)
    return {'name': args['name'], 'sha256': cfg['approved'][args['name']], 'steps': result,
            'note': 'Every step is revalidated and separately approved in the SAME role/tool registry; no grants are saved.'}


def perform(ctx, args, review, guard, registry):
    if ctx.state.get('computer_replay'):
        raise ValueError('Nested workflow replay forbidden')
    if any(not registry.has(step['tool']) for step in review['steps']):
        raise ValueError('Macro requires a tool absent from this role/session; replay cannot widen the registry')
    results = []
    ctx.state['computer_replay'] = True
    prior_force = getattr(ctx.gate, 'force_fresh', False)
    ctx.gate.force_fresh = True  # macro membership is NOT an approval for any step
    try:
        for index, step in enumerate(review['steps']):
            guard()
            cfg = check(ctx)['computer']['workflows']
            read_macro(ctx.workspace, cfg, args['name'])  # recheck pin before EVERY step
            output, error = registry.call(step['tool'], step['args'])
            results.append({'step': index + 1, 'tool': step['tool'], 'error': error})
            if error:
                raise ValueError(f'Macro stopped at step {index + 1}; earlier effects are not rolled back. Inspect before retry. {output[:240]}')
        return {'name': args['name'], 'steps': results, 'completed': True, 'reused_approvals': 0}
    finally:
        ctx.state.pop('computer_replay', None)
        ctx.gate.force_fresh = prior_force


class Recorder:
    """Runtime drafts are NEVER executable. Promotion/pinning is an operator file edit."""
    def __init__(self, workspace, *, gate=None):
        self.root = Path(workspace).resolve()
        self.path = safe_local(self.root, '.nexus/computer/recording.json')
        self.gate = gate

    def config(self):
        from .permissions import Permissions
        cfg = Permissions(self.root).load()['computer']['workflows']
        if not cfg['enabled']:
            raise ValueError('Workflow recording disabled in permissions.yaml')
        return cfg

    def read(self):
        if not self.path.exists():
            return {'active': False, 'steps': [], 'parameters': {}}
        row = json.loads(bounded(self.path, 64000))
        if not isinstance(row, dict) or type(row.get('active')) is not bool:
            raise ValueError('Invalid recording state')
        return row

    def _write(self, value):
        from .assistant.common import atomic_json
        runtime(self.root)
        safe_local(self.root, self.path)
        atomic_json(self.path, value)

    def start(self, name):
        identifier(name)
        self.config()
        from .computer_actions import registry_for
        from .types import Risk, ToolSpec
        reg = registry_for(self.root, self.gate)
        spec = ToolSpec('workflow_record', 'Start visible semantic recording; never raw OS events.', {'type': 'object'}, Risk.DANGEROUS)
        if not reg.ctx.gate.check(spec, {'name': name, 'destination': 'unreviewed runtime draft'}).approved:
            raise ValueError('Fresh approval required to start recording')
        self.config()
        check(reg.ctx)
        with self.lock():
            if self.read()['active']:
                raise ValueError('Stop the active recording before starting another')
            row = {'active': True, 'name': name, 'started': time.time(), 'parameters': {}, 'steps': []}
            self._write(row)
        return {**row, 'executable': False}

    def lock(self):
        lock = runtime(self.root) / 'recording.lock'
        safe_local(self.root, lock)
        return FileLock(str(lock), timeout=2)

    def stop(self):
        self.config()
        with self.lock():
            row = self.read()
            if not row['active']:
                return {**row, 'executable': False}
            row['active'] = False
            self._write(row)
            draft = {'schema': 1, 'parameters': row['parameters'], 'steps': row['steps']}
            from .computer_actions import exclusive
            path = runtime(self.root, 'drafts') / (row['name'] + '-' + str(time.time_ns()) + '.yaml')
            exclusive(path, yaml.safe_dump(draft, sort_keys=False).encode())
        return {'name': row['name'], 'draft': str(path.relative_to(self.root)), 'steps': len(row['steps']),
                'executable': False, 'note': 'Review, edit parameters, copy manually to computer-macros, and pin SHA-256 in permissions.yaml.'}

    def success(self, name, args):
        cfg = self.config()
        if name not in cfg['allowed_tools']:
            return
        with self.lock():
            row = self.read()
            if not row['active']:
                return
            if len(row['steps']) >= cfg['max_steps']:
                raise ValueError('Recording step budget exceeded; stop and review the draft')
            definitions = row['parameters']
            def sanitize(value, key=''):
                if key in {'text', 'content', 'body', 'message', 'fields', 'parameters'}:
                    if isinstance(value, dict):
                        return {k: sanitize(v, 'text') for k, v in value.items()}
                    if isinstance(value, str):
                        parameter = 'input_' + str(len(definitions) + 1)
                        definitions[parameter] = {'type': 'string', 'max_length': min(4000, max(500, len(value)))}
                        return '${' + parameter + '}'
                if isinstance(value, dict):
                    return {k: sanitize(v, k) for k, v in value.items() if k != '_review'}
                if isinstance(value, list):
                    return [sanitize(v) for v in value]
                return value
            step = {'tool': name, 'args': sanitize(args)}
            from .github_safety import scan_text
            scan_text(json.dumps(step), workspace=self.root)
            row['steps'].append(step)
            if len(json.dumps(row).encode()) > 60000:
                raise ValueError('Recording byte budget exceeded')
            self._write(row)


def record_success(ctx, name, args):
    if not ctx.gate.permissions or ctx.state.get('computer_replay'):
        return
    cfg = ctx.gate.permissions.load()['computer']['workflows']
    path = safe_local(ctx.workspace, '.nexus/computer/recording.json')
    if cfg['enabled'] and path.exists():
        Recorder(ctx.workspace).success(name, args)


def voice_request(root, utterance):
    from .permissions import Permissions
    cfg = Permissions(root).load()['computer']
    phrase = ' '.join(utterance.strip().casefold().split())
    match = VOICE_RUN.fullmatch(phrase)
    if match:
        if not cfg['workflows']['enabled'] or not cfg['workflows']['voice']:
            raise ValueError('Voice macros are disabled in operator YAML')
        name = match[1].replace(' ', '-')
        if name not in cfg['workflows']['approved']:
            raise ValueError('No operator-reviewed macro with that exact name')
        return 'workflow_run', {'name': name, 'parameters': {}}
    command = cfg['smart_home']['voice_commands'].get(phrase)
    if command is not None:
        if not cfg['smart_home']['enabled'] or not cfg['smart_home']['voice']:
            raise ValueError('Smart-home voice commands are disabled in operator YAML')
        return 'home_action', dict(command)
    return None
