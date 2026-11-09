"""Opt-in computer adapters; all calls use the existing registry and approval gate."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path


def settings(value):
    if not isinstance(value, dict) or set(value) - {'browser', 'editor', 'office', 'browser_origins', 'shell_backend', 'image',
                                                      'desktop', 'files', 'workflows', 'browser_controls', 'smart_home'}:
        raise ValueError('Invalid computer configuration')
    for key in ('browser', 'editor', 'office'):
        if type(value.get(key, False)) is not bool:
            raise ValueError('Computer adapters require boolean opt-in')
    origins = value.get('browser_origins', [])
    if not isinstance(origins, list) or len(origins) > 20:
        raise ValueError('Bounded browser origin allowlist required')
    from .computer_policy import extensions, origin
    for item in origins:
        origin(item)
    backend = value.get('shell_backend', 'host')
    if not isinstance(backend, str) or backend not in {'host', 'docker'}:
        raise ValueError('Unknown shell backend')
    image = value.get('image', '')
    if not isinstance(image, str):
        raise ValueError('Invalid image')
    if backend == 'docker' and (not isinstance(image, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:@-]{0,150}@sha256:[a-f0-9]{64}', image)):
        raise ValueError('Docker requires an explicitly installed digest-pinned image')
    return {**{'browser': False, 'editor': False, 'office': False, 'browser_origins': [],
               'shell_backend': 'host', 'image': ''}, **value, **extensions(value)}


def environment():
    # No cloud/model/GitHub tokens, user browser profiles or inherited plugin config.
    return {k: v for k, v in os.environ.items() if k in {'PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'LANG', 'LD_LIBRARY_PATH', 'CHROMIUM_EXECUTABLE_PATH'}}


def execute(ctx, argv, timeout=60):
    from .live_process import stream_shell
    from .types import Event, EventType
    def metadata_only(event):
        if event.type != EventType.TOOL_OUTPUT:
            ctx.emit(Event(event.type, {**event.data, 'command': 'computer worker [reviewed arguments withheld]'}))
    output, code = stream_shell(argv, ctx.workspace, timeout=timeout, emit=metadata_only,
                                cancel=ctx.cancel_event, call_id=ctx.state.get('active_call_id', ''),
                                max_chars=ctx.max_output_chars, tool=ctx.state.get('active_tool', 'computer'), env=environment())
    if code:
        raise ValueError('Computer worker failed; no host/unsafe fallback is permitted')
    from .github_safety import GitHubSafetyError, safe_output, token_from_environment
    from .studio import redact
    try:
        return safe_output(output, {'GITHUB_TOKEN': token_from_environment(ctx.workspace)})
    except GitHubSafetyError:
        return redact(output)


def config(ctx):
    return ctx.gate.permissions.load()['computer']


def office(ctx, args):
    if not config(ctx)['office']:
        raise ValueError('Office adapter is disabled')
    from .office import operate
    from .snapshots import Snapshots
    path = ctx.gate.permissions.path(args['path'])
    if path.exists() and path.stat().st_size > 500_000:
        raise ValueError('Office file budget exceeded')
    before = path.read_bytes() if path.exists() else None
    output, planned = operate(path, args)
    if planned is not None:
        with Snapshots(ctx.workspace).change(path, 'office_file', planned, expected=before) as identifier:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(planned)
        ctx.state['last_snapshot'] = identifier
        ctx.state.setdefault('changed_files', set()).add(str(path.relative_to(ctx.workspace)))
        from .tools import emit_file_change
        emit_file_change(ctx, path, 'Bounded OOXML update; binary diff omitted; snapshot available', 'updated' if before else 'created')
    return output


def editor(ctx, args):
    if not config(ctx)['editor']:
        raise ValueError('Editor adapter is disabled')
    path = ctx.gate.permissions.path(args['path'])
    if not path.is_file() or path.suffix.lower() == '.code-workspace':
        raise ValueError('Open an existing workspace file, not a repository folder')
    line = args.get('line', 1)
    if type(line) is not int or not 1 <= line <= 1000000:
        raise ValueError('Bounded line required')
    code = shutil.which('code')
    if not code:
        raise ValueError('VS Code CLI is not installed')
    # Isolated profile/extensions; no workspace .vscode tasks/extensions are opened.
    with tempfile.TemporaryDirectory(prefix='nexus-editor-') as temporary:
        return execute(ctx, [code, '--new-window', '--wait', '--disable-extensions',
                             '--user-data-dir', temporary, '--goto', f'{path}:{line}'], 120)


def browser(ctx, args):
    """Compatibility entry point; direct callers must also use the shared gate."""
    from .tools import build_tools
    output, error = build_tools(ctx).call('browser_action', args)
    if error:
        raise ValueError(output)
    return output


def sandbox(ctx, command, cwd, timeout):
    """Run a disposable copy only: never bind the actual source or credentials."""
    policy = ctx.gate.permissions.load()
    cfg = policy['computer']
    if cfg['shell_backend'] != 'docker':
        raise ValueError('Docker backend not configured')
    docker = shutil.which('docker')
    if not docker:
        raise ValueError('Docker unavailable; refusing host fallback')
    from .github_safety import scan_text
    from .live_process import stream_shell
    from .sandbox import SANDBOX_DEFAULTS, docker_host, docker_run_argv
    from .tools import walk_files
    sandbox_cfg = policy.get('sandbox') or dict(SANDBOX_DEFAULTS)
    effective_timeout = min(float(timeout), float(sandbox_cfg.get('timeout_seconds', timeout)))
    ident = 'nexus-computer-' + uuid.uuid4().hex[:16]
    with tempfile.TemporaryDirectory(prefix='nexus-sandbox-') as temporary:
        root = Path(temporary) / 'source'
        root.mkdir(mode=0o755)
        total = 0
        files = walk_files(cwd, ctx.excludes, 201)
        if len(files) > 200:
            raise ValueError('Sandbox source budget exceeded')
        for relative in files:
            try:
                path = ctx.gate.permissions.path(str(cwd / relative))
            except ValueError:
                continue  # Private/runtime files are never mounted/copied.
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 500_000 or path.stat().st_nlink != 1:
                raise ValueError('Sandbox source requires bounded regular text files')
            data = path.read_bytes()
            total += len(data)
            if total > 2_000_000:
                raise ValueError('Sandbox source budget exceeded')
            scan_text(data.decode('utf-8'), workspace=ctx.workspace)
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        host = docker_host()
        base = [docker, '--host', host]
        # Explicit no-pull: inspect uses the same constrained environment/timeout.
        execute(ctx, [*base, 'image', 'inspect', cfg['image']], 10)
        argv = docker_run_argv(cfg['image'], root, ident, command, sandbox_cfg, docker_bin=docker, host_arg=host)
        try:
            return stream_shell(argv, cwd, timeout=effective_timeout, emit=ctx.emit, cancel=ctx.cancel_event,
                                max_chars=ctx.max_output_chars, env=environment())
        finally:
            # Stop the container even when its client process group was cancelled.
            import subprocess
            subprocess.run([*base, 'rm', '-f', ident], env=environment(), capture_output=True, timeout=10)


def register(reg, ctx):
    if ctx.gate.permissions is None:
        return
    try:
        cfg = config(ctx)
    except (ValueError, OSError, TimeoutError):
        return  # Existing gate still denies every call under malformed policy.
    from .types import Risk
    def schema(props, required=None):
        return {'type': 'object', 'properties': props, 'required': required or ['path']}
    if cfg['office']:
        reg.register('office_file', 'Read/write macro-free local DOCX/PPTX text or literal XLSX cells; snapshot writes.',
                     schema({'path': {'type': 'string'}, 'action': {'enum': ['read', 'write', 'append', 'set']},
                             'text': {'type': 'string'}, 'sheet': {'type': 'string'}, 'cell': {'type': 'string'}, 'value': {}}, ['path', 'action']),
                     lambda args: office(ctx, args), risk=Risk.WRITE, readonly_actions=['read'])
    if cfg['editor']:
        reg.register('vscode_open', 'Open one file in isolated VS Code; manual edits are user edits, not auto-undoable.',
                     schema({'path': {'type': 'string'}, 'line': {'type': 'integer'}}),
                     lambda args: editor(ctx, args), risk=Risk.DANGEROUS)
    from .computer_actions import perform, prepare
    controls = {
        'desktop_action': ('desktop', 'Allowlisted app/window control; all input, focus, launch and capture require fresh approval.',
            {'action': {'type': 'string'}, 'app': {'type': 'string'}, 'window': {'type': 'string'},
             'text': {'type': 'string'}, 'keys': {'type': 'array', 'items': {'type': 'string'}},
             'x': {'type': 'integer'}, 'y': {'type': 'integer'}, 'label': {'type': 'string'},
             'button': {'type': 'integer'}, 'clicks': {'type': 'integer'}}, ['app', 'action']),
        'file_action': ('files', 'Scoped organize/rename/backup/restore_backup/duplicates/delete/restore. Deletes go to private trash.',
            {'action': {'type': 'string'}, 'path': {'type': 'string'}, 'destination': {'type': 'string'},
             'id': {'type': 'string'}, 'moves': {'type': 'array', 'items': {'type': 'object'}}}, ['action']),
        'workflow_run': ('workflows', 'Replay an operator-pinned YAML macro. Every step needs fresh approval and the same role tool ceiling.',
            {'name': {'type': 'string'}, 'parameters': {'type': 'object'}}, ['name']),
        'home_action': ('smart_home', 'Exact allowlisted HA/MQTT device action; all physical changes and phone notifications are DANGEROUS.',
            {'adapter': {'enum': ['home_assistant', 'mqtt']}, 'device': {'type': 'string'},
             'action': {'type': 'string'}, 'message': {'type': 'string'}}, ['adapter', 'device', 'action']),
    }
    if cfg['browser']:
        from .computer_browser import ACTIONS
        controls['browser_action'] = ('browser',
            'Ephemeral scriptless browser; exact origins, no credential autofill. Submit requires a separately reviewed draft digest.',
            {'url': {'type': 'string'}, 'action': {'enum': sorted(ACTIONS)}, 'selector': {'type': 'string'},
             'text': {'type': 'string'}, 'query': {'type': 'string'}, 'form': {'type': 'string'},
             'fields': {'type': 'object'}, 'form_sha256': {'type': 'string'}, 'target': {'type': 'string'},
             'method': {'enum': ['GET', 'POST']}, 'selectors': {'type': 'object'}}, ['action'])
    for name, (feature, description, props, required) in controls.items():
        enabled = cfg[feature] if feature == 'browser' else cfg[feature]['enabled']
        if enabled:
            reg.register(name, description, schema(props, required),
                         lambda args, name=name: perform(ctx, name, args, reg), risk=Risk.DANGEROUS,
                         prepare=lambda args, name=name: prepare(ctx, name, args))
