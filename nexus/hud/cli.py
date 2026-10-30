"""Compatibility command: opens the single main Nexus GUI, never a second app."""
import os
from argparse import Namespace
from pathlib import Path


def command(args):
    from ..cli import cmd_panel
    root = Path(args.workspace or '.').resolve()
    token = os.environ.get('NEXUS_SERVE_TOKEN', '') or os.environ.get('NEXUS_HUD_TOKEN', '')
    print('nexus hud is now an alias for nexus gui. Operations and weather are inside /dashboard.')
    return cmd_panel(Namespace(workspace=str(root), host='0.0.0.0' if args.expose else '127.0.0.1',
                               port=8787, token=token, yes=False, quiet=False, demo=False, open=True,
                               remember_token=args.remember_token))


def add_parser(sub):
    parser = sub.add_parser('hud', help='compatibility alias for the single Nexus GUI')
    parser.add_argument('-C', '--workspace')
    parser.add_argument('--tui', action='store_true', help='deprecated; opens the main GUI instead')
    parser.add_argument('--remember-token', action='store_true', help='enroll the shared GUI credential in the native OS vault')
    parser.add_argument('--expose', action='store_true')
    parser.add_argument('--allow-host', action='append', default=[], help='deprecated; use NEXUS_ALLOWED_HOSTS')
    parser.set_defaults(func=command)
