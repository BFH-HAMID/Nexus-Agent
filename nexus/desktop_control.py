"""Allowlisted native desktop adapters. No raw global input or shell fallback."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from .computer_actions import check, exclusive, identity, runtime
from .computer_policy import DESKTOP_ACTIONS, integer, mapping, text

KEYS = {'ctrl', 'alt', 'shift', 'super', 'enter', 'escape', 'tab', 'space', 'backspace', 'delete',
        'home', 'end', 'up', 'down', 'left', 'right', 'pageup', 'pagedown',
        *'abcdefghijklmnopqrstuvwxyz0123456789', *(f'f{i}' for i in range(1, 13))}


def environment():
    from .computer import environment as base
    return {**base(), **{k: os.environ[k] for k in ('DISPLAY', 'XAUTHORITY', 'HOME', 'USERPROFILE',
             'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR') if k in os.environ}}


def process(pid):
    import psutil
    try:
        proc = psutil.Process(pid)
        return {'pid': pid, 'executable': str(Path(proc.exe()).resolve()), 'started': proc.create_time()}
    except (psutil.Error, OSError):
        raise ValueError('Window process identity unavailable') from None


class X11Desktop:
    def __init__(self, ctx):
        self.ctx = ctx
        if not os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
            raise ValueError('Verified X11 desktop required; Wayland/ydotool global-input fallback is forbidden')
        self.commands = {name: shutil.which(name) for name in ('xdotool', 'wmctrl')}
        if not all(self.commands.values()):
            raise ValueError('Install native xdotool and wmctrl for Linux desktop control')
        for command in self.commands.values():
            if Path(command).resolve().is_relative_to(ctx.workspace):
                raise ValueError('Native desktop helpers must not come from the agent workspace')

    def command(self, name, *args):
        from .live_process import stream_shell
        getattr(self, 'guard', lambda: check(self.ctx))()
        output, code = stream_shell([self.commands[name], *map(str, args)], Path(tempfile.gettempdir()),
            timeout=5, max_chars=64000, emit=lambda _event: None, cancel=self.ctx.cancel_event, env=environment())
        if code or '[output capped at' in output:
            raise ValueError('Native desktop command failed; no fallback')
        return output.rsplit('[exit ', 1)[0].strip()

    def locate(self, app, window=None):
        rows = []
        active = int(self.command('xdotool', 'getactivewindow').strip())
        for line in self.command('wmctrl', '-lpGx').splitlines():
            parts = line.split(None, 9)
            if len(parts) < 9 or parts[7] != app['window_class']:
                continue
            try:
                handle, pid = int(parts[0], 16), int(parts[2])
                owner = process(pid)
                if owner['executable'] != str(Path(app['executable']).resolve()) or window and str(handle) != window:
                    continue
                x, y, width, height = map(int, parts[3:7])
                rows.append({**owner, 'handle': handle, 'class': parts[7], 'foreground': handle == active,
                             'bounds': {'left': x, 'top': y, 'width': width, 'height': height}})
            except (ValueError, OSError):
                continue
        if len(rows) != 1:
            raise ValueError('Exactly one verified allowlisted app window is required; supply its window handle if ambiguous')
        return rows[0]

    def act(self, action, args, window):
        handle = str(window['handle'])
        if action == 'focus':
            self.command('wmctrl', '-ia', hex(window['handle']))
        elif action == 'close':
            self.command('wmctrl', '-ic', hex(window['handle']))  # WM_DELETE, never SIGKILL
        elif action == 'type':
            self.command('xdotool', 'type', '--clearmodifiers', '--delay', '1', '--', args['text'])
        elif action == 'hotkey':
            names = {'ctrl': 'ctrl', 'super': 'super', 'enter': 'Return', 'escape': 'Escape',
                     'space': 'space', 'pageup': 'Prior', 'pagedown': 'Next'}
            chord = '+'.join(names.get(k, k.capitalize() if len(k) > 1 else k) for k in args['keys'])
            self.command('xdotool', 'key', '--clearmodifiers', chord)
        elif action in {'click', 'move'}:
            if 'label' in args:
                raise ValueError('X11 accessibility labels are unavailable; use reviewed window-relative coordinates')
            self.command('xdotool', 'mousemove', '--sync', '--window', handle, args['x'], args['y'])
            if action == 'click':
                cfg = check(self.ctx)['computer']['desktop']
                if self.locate(cfg['apps'][args['app']], args.get('window')) != window:
                    raise ValueError('Focus/window changed before click; refused')
                self.command('xdotool', 'click', str(args.get('button', 1)))
        elif action == 'scroll':
            self.command('xdotool', 'click', '--repeat', abs(args['clicks']), '--delay', '80',
                         '4' if args['clicks'] > 0 else '5')


class WindowsDesktop:
    def __init__(self, ctx):
        self.ctx = ctx
        try:
            from pywinauto import Desktop, keyboard, mouse
        except ImportError:
            raise ValueError('Install Windows desktop support: pip install -e ".[desktop]"') from None
        self.desktop, self.keyboard, self.mouse = Desktop(backend='uia'), keyboard, mouse

    @staticmethod
    def active():
        import ctypes
        from ctypes import wintypes
        user = ctypes.windll.user32
        user.GetForegroundWindow.restype = wintypes.HWND
        return int(user.GetForegroundWindow() or 0)

    def locate(self, app, window=None):
        rows = []
        for candidate in self.desktop.windows():
            if not candidate.is_visible():
                continue
            owner = process(candidate.process_id())
            if (os.path.normcase(owner['executable']) != os.path.normcase(str(Path(app['executable']).resolve()))
                    or window and str(candidate.handle) != window
                    or app['window_class'] and candidate.class_name() != app['window_class']):
                continue
            bounds = candidate.rectangle()
            rows.append({**owner, 'handle': candidate.handle, 'class': candidate.class_name(),
                         'foreground': candidate.handle == self.active(),
                         'bounds': {'left': bounds.left, 'top': bounds.top,
                                    'width': bounds.width(), 'height': bounds.height()}})
        if len(rows) != 1:
            raise ValueError('Exactly one verified allowlisted Windows app window is required')
        return rows[0]

    @staticmethod
    def literal(value):
        # pywinauto interprets braces/modifiers. Escape every metacharacter rather
        # than treating caller text as its send_keys mini-language.
        escapes = {'{': '{{}', '}': '{}}'}
        return ''.join(escapes.get(c, '{' + c + '}' if c in '+^%~()' else c) for c in value)

    def act(self, action, args, window):
        check(self.ctx)
        candidate = self.desktop.window(handle=window['handle']).wrapper_object()
        if action == 'focus':
            candidate.set_focus()
        elif action == 'close':
            candidate.close()
        elif action == 'type':
            self.keyboard.send_keys(self.literal(args['text']), pause=.01, with_spaces=True,
                                    with_tabs=False, with_newlines=False, vk_packet=True)
        elif action == 'hotkey':
            modifiers = {'ctrl': '^', 'alt': '%', 'shift': '+', 'super': '{LWIN down}'}
            names = {'enter': 'ENTER', 'escape': 'ESC', 'space': 'SPACE', 'backspace': 'BACKSPACE',
                     'pageup': 'PGUP', 'pagedown': 'PGDN'}
            chord = ''.join(modifiers[k] if k in modifiers else '{' + names.get(k, k.upper()) + '}' for k in args['keys'])
            self.keyboard.send_keys(chord + ('{LWIN up}' if 'super' in args['keys'] else ''))
        elif action in {'click', 'move'}:
            if 'label' in args:
                matches = [c for c in candidate.descendants(control_type='Button')
                           if c.window_text() == args['label'] and c.is_visible() and c.is_enabled()]
                if len(matches) != 1:
                    raise ValueError('Exactly one accessible button label required')
                matches[0].click_input()
            else:
                coordinates = (window['bounds']['left'] + args['x'], window['bounds']['top'] + args['y'])
                if action == 'move':
                    self.mouse.move(coords=coordinates)
                else:
                    self.mouse.click(button={1: 'left', 2: 'middle', 3: 'right'}[args.get('button', 1)], coords=coordinates)
        elif action == 'scroll':
            self.mouse.wheel(wheel_dist=args['clicks'])


def backend(ctx, cfg):
    name = cfg['backend']
    if name == 'auto':
        name = 'pywinauto' if os.name == 'nt' else 'xdotool'
    if name == 'pywinauto' and os.name != 'nt':
        raise ValueError('pywinauto requires Windows')
    return WindowsDesktop(ctx) if name == 'pywinauto' else X11Desktop(ctx)


def plan(ctx, args, cfg):
    mapping(args, {'action', 'app', 'window', 'text', 'keys', 'x', 'y', 'label', 'button', 'clicks'}, 'desktop action')
    desktop = cfg['desktop']
    if not desktop['enabled']:
        raise ValueError('Desktop control is disabled in permissions.yaml')
    app = desktop['apps'].get(args.get('app'))
    action = args.get('action')
    if not app or action not in DESKTOP_ACTIONS or action not in app['actions']:
        raise ValueError('App/action is outside the operator desktop allowlist')
    allowed = {'open': set(), 'close': set(), 'focus': set(), 'screenshot': set(), 'type': {'text'},
               'hotkey': {'keys'}, 'click': {'x', 'y', 'label', 'button'}, 'move': {'x', 'y'}, 'scroll': {'clicks'}}
    if set(args) - {'action', 'app', 'window'} - allowed[action]:
        raise ValueError('Unexpected desktop arguments')
    if 'window' in args and (not isinstance(args['window'], str) or not re.fullmatch(r'[1-9][0-9]{0,19}', args['window'])):
        raise ValueError('Use a decimal window handle')
    executable = Path(app['executable'])
    if not executable.is_file() or executable.is_symlink():
        raise ValueError('Trusted app executable is missing or is a symlink')
    if action == 'open':
        return {'executable': app['executable'], 'argv': app['args'], 'identity': identity(executable)}
    native = backend(ctx, desktop)
    window = native.locate(app, args.get('window'))
    bounds = window['bounds']
    if not (0 < bounds['width'] <= 16000 and 0 < bounds['height'] <= 16000):
        raise ValueError('Invalid window geometry')
    if action not in {'focus', 'close'} and not window['foreground']:
        raise ValueError('Input/capture requires the approved app already in the foreground; focus it separately')
    if action == 'type':
        text(args.get('text'), 2000)  # no control keys/newlines hidden in typed text
    if action == 'hotkey':
        keys = args.get('keys')
        if not isinstance(keys, list) or not 1 <= len(keys) <= 5 or any(k not in KEYS for k in keys):
            raise ValueError('Use 1–5 explicit key names, not a keyboard script')
        if len(set(keys)) != len(keys):
            raise ValueError('Duplicate chord keys')
    if action in {'click', 'move'}:
        if 'label' in args:
            text(args['label'], 120)
            if 'x' in args or 'y' in args or action != 'click' or isinstance(native, X11Desktop):
                raise ValueError('Accessibility labels require Windows UIA; do not mix with coordinates')
        else:
            integer(args.get('x'), 0, bounds['width'] - 1, 'Window-relative x')
            integer(args.get('y'), 0, bounds['height'] - 1, 'Window-relative y')
        integer(args.get('button', 1), 1, 3, 'Mouse button')
    if action == 'scroll':
        integer(args.get('clicks'), -20, 20, 'Scroll clicks')
        if args['clicks'] == 0:
            raise ValueError('Nonzero scroll clicks required')
    if action == 'screenshot':
        from .context.core import Context, settings
        from .vision.state import check_running
        check_running(ctx.workspace)
        config = settings(ctx.workspace)
        if not config['enabled'] or not config['screenshots']:
            raise ValueError('Screenshot requires context.yaml enabled + screenshots')
        foreground = Context(ctx.workspace).desktop()
        if foreground['bounds'] != bounds or foreground.get('handle', window['handle']) != window['handle']:
            raise ValueError('Capture foreground does not match the allowlisted app')
        window = {**window, 'capture_policy': config,
                  'destination': 'private local .nexus/computer/captures; no cloud/model upload'}
    return {'window': window, 'executable_identity': identity(executable)}


def perform(ctx, args, reviewed, guard, registry):
    cfg = ctx.gate.permissions.load()['computer']['desktop']
    app, action = cfg['apps'][args['app']], args['action']
    guard()
    if action == 'open':
        try:
            child = subprocess.Popen([app['executable'], *app['args']], cwd=tempfile.gettempdir(), env=environment(),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        except OSError:
            raise ValueError('Approved app launch failed; no launcher/shell fallback') from None
        return {'action': action, 'app': args['app'], 'pid': child.pid,
                'note': 'Launch requested, not proof of a visible/ready app. Closing is a separately approved action.'}
    native = backend(ctx, cfg)
    window = reviewed['window']
    current = native.locate(app, args.get('window'))
    if current != {k: v for k, v in window.items() if k not in {'capture_policy', 'destination'}}:
        raise ValueError('App/window identity or focus changed; input refused')
    if action == 'screenshot':
        from .context.core import Context
        from .context.vision import capture
        from .vision.state import check_running, control
        epoch = control(ctx.workspace)['epoch']
        directory = runtime(ctx.workspace, 'captures')
        if sum(1 for _ in directory.iterdir()) >= 20:
            raise ValueError('Local screenshot quota reached; operator must remove captures explicitly')
        data = capture(Context(ctx.workspace), ctx.gate)  # real independent privacy approval + OCR + lease
        try:
            guard()
            check_running(ctx.workspace, epoch)
            if native.locate(app, args.get('window')) != current:
                raise ValueError('Foreground changed during capture; image discarded')
            output = directory / (uuid.uuid4().hex + '.jpg')
            exclusive(output, data)
            return {'action': action, 'app': args['app'], 'path': str(output.relative_to(ctx.workspace)),
                    'bytes': len(data), 'local_only': True, 'untrusted': True}
        finally:
            data[:] = b'\0' * len(data)
    guard()
    native.guard = guard
    native.act(action, args, current)
    return {'action': action, 'app': args['app'], 'requested': True,
            'note': 'Native request sent, not independent proof of app-specific success. Inspect uncertain effects before retry.'}
