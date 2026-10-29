"""Real bounded native probes. Unsupported/headless hosts fail closed, never fake."""
import ctypes
import os
import shutil


def command(argv, *, cwd=None):
    from pathlib import Path

    from ..live_process import stream_shell
    # Existing bounded process-group cleanup, no shell or inherited secrets.
    output, code = stream_shell(argv, cwd=Path(cwd or os.getcwd()), timeout=2, max_chars=4096, emit=lambda event: None,
                         env={k: os.environ[k] for k in ('PATH', 'SystemRoot', 'WINDIR', 'DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'HOME') if k in os.environ})
    if code != 0 or '[output capped at' in output:
        raise ValueError('Native context probe unavailable')
    return output.rsplit('[exit ', 1)[0].strip()[:4096]


def foreground():
    if os.name == 'nt':
        from ctypes import wintypes
        user = ctypes.windll.user32
        user.GetForegroundWindow.restype = wintypes.HWND
        user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        hwnd = user.GetForegroundWindow()
        if not hwnd:
            raise ValueError('No foreground window')
        if user.GetWindowTextLengthW(hwnd) > 1024:
            raise ValueError('Window identity exceeds privacy review budget')
        title = ctypes.create_unicode_buffer(1025)
        user.GetWindowTextW(hwnd, title, 1025)
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        import psutil
        rectangle = wintypes.RECT()
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        if not user.GetWindowRect(hwnd, ctypes.byref(rectangle)):
            raise ValueError('Window bounds unavailable')
        return {'title': title.value, 'app': psutil.Process(pid.value).name(), 'handle': int(hwnd),
                'bounds': {'left': rectangle.left, 'top': rectangle.top, 'width': rectangle.right - rectangle.left, 'height': rectangle.bottom - rectangle.top}}
    xprop = shutil.which('xprop')
    if not xprop or not os.environ.get('DISPLAY'):
        raise ValueError('Native desktop identity unavailable; ambient capture refused')
    import re
    active = command([xprop, '-root', '_NET_ACTIVE_WINDOW'])
    match = re.search(r'0x[0-9a-fA-F]+', active)
    if not match or int(match.group(), 16) == 0:
        raise ValueError('No active X11 window')
    data = command([xprop, '-id', match.group(), '_NET_WM_NAME', 'WM_CLASS'])
    parts = data.splitlines()
    info = {'title': next((p.split('=', 1)[1].strip() for p in parts if '_NET_WM_NAME' in p and '=' in p), ''),
            'app': next((p.split('=', 1)[1].strip() for p in parts if 'WM_CLASS' in p and '=' in p), '')}
    if not info['title'] or not info['app']:
        raise ValueError('Desktop identity incomplete; ambient capture refused')
    xwininfo = shutil.which('xwininfo')
    if not xwininfo:
        raise ValueError('X11 window geometry unavailable')
    geometry = command([xwininfo, '-id', match.group(), '-stats'])
    values = [re.search(re.escape(label) + r':\s*(-?\d+)', geometry) for label in ('Absolute upper-left X', 'Absolute upper-left Y', 'Width', 'Height')]
    if not all(values):
        raise ValueError('Window geometry incomplete')
    info['bounds'] = dict(zip(('left', 'top', 'width', 'height'), [int(v.group(1)) for v in values], strict=True))
    return info


def clipboard():
    if os.name == 'nt':
        from ctypes import wintypes
        user, kernel = ctypes.windll.user32, ctypes.windll.kernel32
        user.GetClipboardData.restype = wintypes.HANDLE
        kernel.GlobalLock.restype = ctypes.c_void_p
        kernel.GlobalLock.argtypes = [wintypes.HANDLE]
        kernel.GlobalSize.argtypes = [wintypes.HANDLE]
        kernel.GlobalSize.restype = ctypes.c_size_t
        kernel.GlobalUnlock.argtypes = [wintypes.HANDLE]
        if not user.OpenClipboard(None):
            raise ValueError('Clipboard unavailable')
        try:
            handle = user.GetClipboardData(13)
            if not handle or kernel.GlobalSize(handle) > 8192:
                raise ValueError('Clipboard must be bounded Unicode text')
            pointer = kernel.GlobalLock(handle)
            if not pointer:
                raise ValueError('Clipboard unavailable')
            try:
                return ctypes.wstring_at(pointer, kernel.GlobalSize(handle) // 2).split('\x00', 1)[0][:4096]
            finally:
                kernel.GlobalUnlock(handle)
        finally:
            user.CloseClipboard()
    executable = shutil.which('xclip')
    if not executable:
        raise ValueError('Install xclip on X11; clipboard otherwise refused')
    return command([executable, '-selection', 'clipboard', '-out'])


def git_status(root):
    git = shutil.which('git')
    if not git:
        raise ValueError('Git unavailable')
    return command([git, '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false', 'status', '--porcelain=v1', '--untracked-files=no'], cwd=str(root))
