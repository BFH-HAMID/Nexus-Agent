"""Explicitly enabled native desktop submission. Counts only; no task text/secrets."""
import os
import shutil
import subprocess
import sys


def submit(count, approvals):
    message = f'Nexus Jarvis: {count} unread results, {approvals} pending task/action reviews. Open nexus jarvis inbox / approvals.'
    if sys.platform == 'win32':
        executable = shutil.which('powershell.exe')
        script = ("$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.Windows.Forms; "
                  "Add-Type -AssemblyName System.Drawing; $n=New-Object System.Windows.Forms.NotifyIcon; "
                  "$n.Icon=[System.Drawing.SystemIcons]::Information; $n.Visible=$true; "
                  "try {$n.ShowBalloonTip(4000,'Nexus Jarvis',$env:NEXUS_JARVIS_NOTIFICATION,"
                  "[System.Windows.Forms.ToolTipIcon]::Info); Start-Sleep -Seconds 5} finally {$n.Dispose()}")
        argv = [executable, '-NoProfile', '-NonInteractive', '-Command', script]
    elif sys.platform == 'darwin':
        executable = shutil.which('osascript')
        argv = [executable, '-e', 'on run argv\ndisplay notification (item 1 of argv) with title "Nexus Jarvis"\nend run', message]
    else:
        executable = shutil.which('notify-send')
        argv = [executable, '--app-name=Nexus Jarvis', '--', 'Nexus Jarvis', message]
    if not executable:
        raise ValueError('Native notifier unavailable; inbox remains authoritative')
    keep = {'PATH', 'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP',
            'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR', 'DISPLAY', 'WAYLAND_DISPLAY', 'LANG'}
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env['NEXUS_JARVIS_NOTIFICATION'] = message
    result = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=8, check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0)
    if result.returncode:
        raise ValueError('Native notifier refused submission; use local inbox')
    return {'submitted': True, 'human_read_confirmed': False, 'content': 'counts only; no task text'}
