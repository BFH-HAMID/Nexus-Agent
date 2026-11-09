"""Shared preparation/revalidation for tools, operator CLI, voice and Computer Studio.

A prepared review is data, not a saved grant. Only ToolRegistry calls the handler
(after ApprovalGate); every invocation prepares anew and binds live target identity.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from .computer_policy import CONTROL_TOOLS

PREPARED_TOOLS = CONTROL_TOOLS | {'browser_action'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def check(ctx, policy_digest=None):
    from .computer_control import EmergencyStop
    if EmergencyStop().stopped() or ctx.cancel_event is not None and ctx.cancel_event.is_set():
        raise ValueError('Computer action cancelled; no further effects permitted')
    permissions = ctx.gate.permissions
    if permissions is None or permissions.root != Path(ctx.workspace).resolve():
        raise ValueError('Computer control requires workspace Permissions and the shared approval gate')
    policy = permissions.load()
    if policy_digest is not None and digest(policy) != policy_digest:
        raise ValueError('Computer policy changed; fresh review required')
    return policy


def safe_local(root, name):
    """No path component may be a symlink, even a link back into an allowed root."""
    root = Path(root).resolve()
    raw = Path(name)
    path = raw if raw.is_absolute() else root / raw
    if not path.is_relative_to(root) or '..' in path.parts:
        raise ValueError('Path escapes the workspace')
    current = root
    for component in path.relative_to(root).parts:
        reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
        if ':' in component or component.endswith((' ', '.')) or component.split('.')[0].upper() in reserved:
            raise ValueError('Portable regular paths required; device names and alternate data streams are forbidden')
        current /= component
        if current.is_symlink():
            raise ValueError('Symlink path components are forbidden')
        if current != path and current.exists() and not current.is_dir():
            raise ValueError('A path parent is not a directory')
    if path.resolve() != path:
        raise ValueError('Link/junction path resolution is forbidden')
    if path.exists() and not path.is_file() and not path.is_dir():
        raise ValueError('Only regular files and directories are supported')
    if path.is_file() and path.stat().st_nlink != 1:
        raise ValueError('Hardlinked files are forbidden')
    return path


def runtime(root, part=''):
    base = safe_local(root, Path('.nexus') / 'computer')
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = safe_local(root, base / part)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError('Computer runtime must be a private directory')
    return path


def identity(path):
    if not path.exists():
        return None
    s = path.stat()
    if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):
        raise ValueError('Special filesystem object refused')
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, stat.S_IMODE(s.st_mode)]


def exclusive(path, data):
    """No overwrite, no executable bit. Caller must scope and recheck the parent."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)  # only this invocation's incomplete new artifact
        raise


def _module(name):
    if name == 'desktop_action':
        from . import desktop_control
        return desktop_control
    if name == 'file_action':
        from . import computer_files
        return computer_files
    if name == 'home_action':
        from . import smart_home
        return smart_home
    if name == 'workflow_run':
        from . import computer_workflows
        return computer_workflows
    if name == 'browser_action':
        from . import computer_browser
        return computer_browser
    raise ValueError('Unknown computer action')


def prepare(ctx, name, args):
    if not isinstance(args, dict) or '_review' in args or len(json.dumps(args).encode()) > 48000:
        raise ValueError('Bounded action arguments required; review metadata cannot be supplied')
    from .github_safety import scan_text
    scan_text(json.dumps(args), workspace=ctx.workspace)
    # Detach caller-owned nested objects; callbacks and UI data cannot silently
    # rewrite a pending plan in place.
    args = json.loads(json.dumps(args, allow_nan=False))
    policy = check(ctx)
    detail = _module(name).plan(ctx, args, policy['computer'])
    if len(json.dumps(detail, ensure_ascii=False).encode()) > 128000:
        raise ValueError('Computer review metadata budget exceeded; narrow the scope')
    return {**args, '_review': {'policy_sha256': digest(policy), 'arguments_sha256': digest(args), 'target': detail}}


def perform(ctx, name, args, registry):
    from . import audit
    reviewed = args.get('_review')
    raw = {k: v for k, v in args.items() if k != '_review'}
    if not reviewed or prepare(ctx, name, raw) != args:
        raise ValueError('Target, input, scope or policy changed since review; nothing executed')
    fingerprint = digest(args)
    # Audit failure BEFORE action refuses execution; failure AFTER is an uncertain
    # outcome, never a reason to silently retry an external effect.
    audit.record(ctx.workspace, 'computer.action', target=name, risk='danger', result='started',
                 detail={'arguments_sha256': fingerprint, 'action': raw.get('action', 'replay')})
    try:
        def guard():
            check(ctx, reviewed['policy_sha256'])
        guard()
        result = _module(name).perform(ctx, raw, reviewed['target'], guard, registry)
    except Exception as exc:
        audit.record(ctx.workspace, 'computer.action', target=name, risk='danger', result='failed-or-uncertain',
                     detail={'arguments_sha256': fingerprint})
        if type(exc) is ValueError:
            raise
        raise ValueError('Computer adapter failed; outcome may be uncertain. No automatic retry or unsafe fallback.') from None
    audit.record(ctx.workspace, 'computer.action', target=name, risk='danger', result='completed',
                 detail={'arguments_sha256': fingerprint})
    return json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result


def history_result(result):
    """Durable computer sessions keep receipts, not the exact interactive output.

    Live authenticated sessions retain the original value so form drafts and
    results can be inspected. This detached projection is never replay authority.
    """
    if not isinstance(result, dict):
        return result
    clean = dict(result)
    for key in ('text', 'error'):
        if key in clean:
            clean[key + '_sha256'] = digest(clean.pop(key))
            clean[key] = 'Computer output omitted from durable history; inspect the live session and local receipts.'
    return clean


def history_event(event, *, computer=False):
    """Body-free persistence copy; NEVER use it in place of a live human review."""
    clean = dict(event)
    if computer and clean.get('type') == 'done':
        clean['result'] = history_result(clean.get('result'))
    if ((computer or clean.get('tool') in PREPARED_TOOLS)
            and clean.get('type') in {'tool_call', 'tool_result', 'tool_output', 'approval', 'approval_request', 'process', 'file_change'}):
        for key in ('args', 'preview', 'output', 'text', 'proposal', 'command', 'diff'):
            if key in clean:
                clean[key + '_sha256'] = digest(clean.pop(key))
        clean['args'] = {'omitted': 'Exact computer arguments/results are available only in the live session.'}
        clean['preview'] = 'Computer action metadata only; not a saved approval or replay payload.'
        clean['replayable'] = False
        clean['history_redacted'] = True
    return clean


def registry_for(workspace, gate=None, *, cancel=None, emit=None):
    from .permissions import Permissions
    from .tools import ApprovalGate, PathGuard, ToolContext, build_tools
    root = Path(workspace).resolve()
    gate = gate or ApprovalGate()
    if gate.permissions is None:
        gate.permissions = Permissions(root)
    if gate.permissions.root != root:
        raise ValueError('Approval gate workspace mismatch')
    ctx = ToolContext(root, PathGuard(root), gate, cancel_event=cancel, emit=emit or (lambda _event: None))
    return build_tools(ctx)


def invoke(workspace, name, args, *, gate=None):
    reg = registry_for(workspace, gate)
    output, error = reg.call(name, args)
    if error:
        raise ValueError(output)
    try:
        return json.loads(output)
    except ValueError:
        return {'result': output}
