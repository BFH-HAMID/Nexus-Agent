"""Bounded inspection and reviewed removal of this workspace's user service only.

Never elevates, overwrites a unit or removes an unverified registration. Manager
output is private: only allowlisted state and ownership facts leave this module.
"""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from .services import manifest


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _run(argv):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=15, check=False)
    if len(result.stdout) > 64000:
        raise ValueError('Manager output exceeds review budget')
    return result.returncode, result.stdout


def _file(path):
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError('Service file/parent symlink refused')
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > 64000 or before.st_nlink != 1:
        raise ValueError('Bounded regular service file required')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        current = os.fstat(fd)
        if (not stat.S_ISREG(current.st_mode) or current.st_size > 64000 or current.st_nlink != 1
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError('Service file changed or unsafe')
        body = os.read(fd, 64001)
    finally:
        os.close(fd)
    if len(body) > 64000:
        raise ValueError('Service file exceeds budget')
    return body, (current.st_dev, current.st_ino)


def _windows_owned(body, expected):
    if b'<!DOCTYPE' in body.replace(b'\x00', b'').upper() or b'<!ENTITY' in body.replace(b'\x00', b'').upper():
        return False
    current, template = ET.fromstring(body), ET.fromstring(expected)
    if current.tag != template.tag:
        return False
    ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    # Scheduler adds defaults. Bind all execution/identity/trigger fields, reject
    # extra actions/triggers/principals, and require least-privilege interactive use.
    for container in ('Actions', 'Triggers', 'Principals'):
        a, b = current.find('t:' + container, ns), template.find('t:' + container, ns)
        if (a is None or b is None or len(current.findall('t:' + container, ns)) != 1
                or len(a) != 1 or a[0].tag != b[0].tag):
            return False
    # Task Scheduler may normalize XML IDs (Operator -> Author). Bind the
    # action to the sole observed principal, not to a cosmetic template ID.
    principal_id = current.find('t:Principals/t:Principal', ns).get('id')
    if not principal_id or current.find('t:Actions', ns).attrib != {'Context': principal_id}:
        return False
    for parent, allowed in [('Actions/Exec', {'Command', 'Arguments', 'WorkingDirectory'}),
                            ('Principals/Principal', {'UserId', 'LogonType', 'RunLevel'})]:
        query = '/'.join('t:' + part for part in parent.split('/'))
        children = list(current.find(query, ns))
        if (len({child.tag for child in children}) != len(children)
                or any(child.tag not in {'{' + ns['t'] + '}' + tag for tag in allowed} for child in children)):
            return False
    fields = ['Actions/Exec/Command', 'Actions/Exec/Arguments', 'Actions/Exec/WorkingDirectory',
              'Principals/Principal/UserId', 'Principals/Principal/LogonType', 'Principals/Principal/RunLevel',
              'Triggers/LogonTrigger/UserId', 'Triggers/LogonTrigger/Enabled',
              'Settings/MultipleInstancesPolicy', 'Settings/ExecutionTimeLimit']
    for field in fields:
        query = '/'.join('t:' + part for part in field.split('/'))
        if current.findtext(query, default='', namespaces=ns) != template.findtext(query, default='', namespaces=ns):
            return False
    return True


def _windows_state(name):
    # name is derived only from the fixed workspace hash, never user script input.
    task = Path(name).stem
    script = ("$ErrorActionPreference='Stop';$s=New-Object -ComObject Schedule.Service;"
              "$s.Connect();$t=$s.GetFolder('\\').GetTask('" + task + "');[Console]::Write([int]$t.State)")
    code, body = _run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script])
    value = body.decode('ascii').strip()
    if code or value not in {'1', '2', '3', '4'}:
        raise ValueError('Task state unavailable')
    return int(value)


def inspect(workspace, platform, user=''):
    data = manifest(workspace, platform, user)
    name = data['filename']
    result = {'filename': name, 'platform': platform, 'supported': False, 'removable': False,
              'notice': 'Read-only inspection. Registration/manager state is not daemon task readiness.'}
    if ((platform == 'windows' and sys.platform != 'win32')
            or (platform == 'systemd' and not sys.platform.startswith('linux'))):
        return {**result, 'reason': 'Inspect/remove on the actual target OS; no cross-platform commands executed.'}
    result['supported'] = True
    try:
        if platform == 'systemd':
            path = Path.home() / '.config/systemd/user' / name
            code, body = _run(['systemctl', '--user', 'show', name, '--property=LoadState,ActiveState,UnitFileState,FragmentPath,DropInPaths,Transient,NeedDaemonReload'])
            if code:
                raise ValueError('Manager query failed')
            props = dict(line.split('=', 1) for line in body.decode('utf-8').splitlines() if '=' in line)
            allowed = {'loaded', 'not-found', 'masked', 'error', 'active', 'inactive', 'failed', 'activating',
                       'deactivating', 'enabled', 'disabled', 'static', 'indirect', 'enabled-runtime'}
            result['states'] = {k: v if v in allowed else 'unknown' for k, v in props.items()
                                if k in {'LoadState', 'ActiveState', 'UnitFileState'}}
            if (props.get('LoadState') != 'loaded' or props.get('FragmentPath') != str(path)
                    or props.get('DropInPaths') or props.get('Transient') == 'yes'
                    or props.get('NeedDaemonReload') != 'no'):
                return {**result, 'reason': 'No matching loaded user-unit fragment. No removal authorized.'}
            contents, identity = _file(path)
            if contents != data['content'].encode():
                return {**result, 'reason': 'Unit differs from the current generated workspace manifest. Inspect locally; no overwrite/delete.'}
            binding = body + contents + repr(identity).encode()
        else:
            code, body = _run(['schtasks.exe', '/Query', '/TN', Path(name).stem, '/XML'])
            if code:
                raise ValueError('Task query failed')
            # /Query XML may use UTF-16; ElementTree honors XML declarations.
            if not _windows_owned(body, data['content'].encode()):
                return {**result, 'reason': 'Task execution/identity differs. Use its exact exported user/SID and inspect locally; no removal authorized.'}
            state = _windows_state(name)
            result['states'] = {'registration': 'matching task XML observed',
                                'task_state': {1: 'disabled', 2: 'queued', 3: 'ready', 4: 'running'}[state]}
            binding = body + str(state).encode()
        result.update(removable=True, revision=_digest(binding))
        result['confirmation'] = 'REMOVE SERVICE ' + name + ' ' + result['revision'][:16]
        result['reason'] = 'Exact workspace registration observed. Removal will stop/end and unregister it; already-started work cannot be undone.'
        return result
    except Exception:
        return {**result, 'reason': 'Manager/file query unavailable or unsafe. Private output withheld; no removal authorized.'}


def remove(workspace, platform, user='', *, confirmation=''):
    before = inspect(workspace, platform, user)
    if not before['removable'] or confirmation != before['confirmation']:
        raise ValueError('Fresh exact observed-registration removal confirmation required')
    name = before['filename']
    outcomes = []
    try:
        if platform == 'systemd':
            code, _ = _run(['systemctl', '--user', 'disable', '--now', name])
            outcomes.append({'step': 'stop-and-disable', 'accepted': code == 0})
            if code:
                raise ValueError('Manager refused disable/stop')
        else:
            code, _ = _run(['schtasks.exe', '/Change', '/TN', Path(name).stem, '/DISABLE'])
            outcomes.append({'step': 'disable-task', 'accepted': code == 0})
            if code:
                raise ValueError('Manager refused disable')
            if _windows_state(name) in {2, 4}:
                code, _ = _run(['schtasks.exe', '/End', '/TN', Path(name).stem])
                outcomes.append({'step': 'end-task', 'accepted': code == 0})
                if code:
                    raise ValueError('Manager refused end')
            if _windows_state(name) != 1:
                raise ValueError('Task still queued/running; preserve registration')
            checked = inspect(workspace, platform, user)
            if not checked['removable'] or checked.get('states', {}).get('task_state') != 'disabled':
                raise ValueError('Task ownership/state changed during removal')
            code, _ = _run(['schtasks.exe', '/Delete', '/TN', Path(name).stem, '/F'])
            outcomes.append({'step': 'delete-task', 'accepted': code == 0})
            if code:
                raise ValueError('Manager refused deletion')
        if platform == 'systemd':
            checked = inspect(workspace, platform, user)
            if (not checked['removable'] or checked.get('states', {}).get('ActiveState') not in {'inactive', 'failed'}
                    or checked.get('states', {}).get('UnitFileState') != 'disabled'):
                raise ValueError('Unit ownership/overrides/stop state changed during removal')
            path = Path.home() / '.config/systemd/user' / name
            current, _ = _file(path)
            if current != manifest(workspace, platform, user)['content'].encode():
                raise ValueError('Unit changed during removal; preserve it')
            path.unlink()
            outcomes.append({'step': 'remove-unit-file', 'accepted': True})
            code, _ = _run(['systemctl', '--user', 'daemon-reload'])
            outcomes.append({'step': 'daemon-reload', 'accepted': code == 0})
            if code:
                raise ValueError('Reload refused')
        return {'removal_commands_accepted': True, 'outcomes': outcomes, 'needs_manual_review': True,
                'notice': 'Refresh inspection to verify manager state. No global worker/process-stop claim. Windows exported XML is preserved.'}
    except Exception:
        return {'removal_commands_accepted': False, 'outcomes': outcomes, 'needs_manual_review': True,
                'notice': 'Partial manager outcome possible. Inspect before retry; no automatic rollback/overwrite. Private errors withheld.'}
