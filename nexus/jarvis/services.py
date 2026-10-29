"""Reviewable user-service manifests. Export only: never install/elevate implicitly."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from xml.etree import ElementTree as ET


def manifest(workspace, platform, user=''):
    workspace = str(workspace)
    python = sys.executable
    if any(c in workspace + python + user for c in '\r\n\x00'):
        raise ValueError('Service values cannot contain control characters')
    name = 'nexus-jarvis-' + hashlib.sha256(workspace.encode()).hexdigest()[:12]
    argv = [python, '-I', '-m', 'nexus', 'jarvis', '-C', workspace, 'supervise']
    if platform == 'systemd':
        def quote(value):
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
        content = '\n'.join(['[Unit]', 'Description=Nexus Jarvis (explicit runtime policy)',
                             'StartLimitIntervalSec=300', 'StartLimitBurst=3', '[Service]', 'Type=simple',
                             'ExecStart=' + ' '.join(quote(a) for a in argv), 'Restart=on-failure', 'RestartSec=30',
                             'TimeoutStopSec=10', 'KillMode=control-group', 'UMask=0077', '[Install]',
                             'WantedBy=default.target', ''])
        return {'filename': name + '.service', 'content': content, 'installed': False,
                'instructions': ['Review the unit; install Nexus into the referenced Python environment first.',
                                 'Save under ~/.config/systemd/user/' + name + '.service',
                                 'systemctl --user daemon-reload', f'systemctl --user enable --now {name}.service',
                                 f'systemctl --user disable --now {name}.service']}
    if platform != 'windows' or not user or '%' in workspace + python:
        raise ValueError('Windows manifest requires an explicit local/domain user, without environment-expanded paths')
    namespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('', namespace)
    def child(parent, tag, text=None, **attributes):
        node = ET.SubElement(parent, '{' + namespace + '}' + tag, attributes)
        node.text = text
        return node
    root = ET.Element('{' + namespace + '}Task', {'version': '1.2'})
    trigger = child(child(root, 'Triggers'), 'LogonTrigger')
    child(trigger, 'Enabled', 'true')
    child(trigger, 'UserId', user)
    principal = child(child(root, 'Principals'), 'Principal', id='Operator')
    child(principal, 'UserId', user)
    child(principal, 'LogonType', 'InteractiveToken')
    child(principal, 'RunLevel', 'LeastPrivilege')
    settings = child(root, 'Settings')
    child(settings, 'MultipleInstancesPolicy', 'IgnoreNew')
    child(settings, 'ExecutionTimeLimit', 'PT0S')
    action = child(child(root, 'Actions', Context='Operator'), 'Exec')
    child(action, 'Command', python)
    child(action, 'Arguments', subprocess.list2cmdline(argv[1:]))
    return {'filename': name + '.xml', 'content': ET.tostring(root, encoding='unicode'), 'installed': False,
            'instructions': ['Review generated XML on Windows; no credentials are stored.',
                             f'schtasks /Create /TN {name} /XML "{name}.xml"',
                             f'schtasks /Run /TN {name}', f'schtasks /Delete /TN {name} /F']}


def install(workspace, platform, user='', *, confirmation=''):
    """Explicit CLI-only user install; exact reviewed manifest, never model authority."""
    import os
    from pathlib import Path

    from ..config import nexus_home
    data = manifest(workspace, platform, user)
    digest = hashlib.sha256(data['content'].encode()).hexdigest()[:16]
    expected = 'INSTALL SERVICE ' + data['filename'] + ' ' + digest
    if confirmation != expected:
        raise ValueError('Exact service manifest confirmation required')
    if (platform == 'windows') != (sys.platform == 'win32') or (platform == 'systemd' and not sys.platform.startswith('linux')):
        raise ValueError('Install on the actual target OS')
    directory = Path.home() / '.config/systemd/user' if platform == 'systemd' else nexus_home() / 'jarvis-services'
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError('Service directory symlink refused')
    target = directory / data['filename']
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
        handle.write(data['content'])
    commands = ([['systemctl', '--user', 'daemon-reload'], ['systemctl', '--user', 'enable', '--now', data['filename']]]
                if platform == 'systemd' else [['schtasks.exe', '/Create', '/TN', target.stem, '/XML', str(target)]])
    try:
        for argv in commands:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=15, check=False)
            if result.returncode:
                raise ValueError('User service manager refused installation')
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return {'installed': False, 'configuration_written': str(target), 'error_type': type(exc).__name__,
                'needs_manual_review': True, 'note': 'Partial manager outcome possible; inspect before retry. No overwrite or automatic rollback.'}
    return {'installed': True, 'configuration_written': str(target), 'running_confirmed': False,
            'note': 'Manager accepted registration; verify actual daemon-status. Windows task starts at logon or via returned Run command.',
            'instructions': data['instructions']}
