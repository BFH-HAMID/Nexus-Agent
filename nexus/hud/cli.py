"""Explicit HUD launch. No daemon/model startup and no token on argv/URL."""
import os
from pathlib import Path


def command(args):
    try:

        from ..server import ServerState
        from .core import settings
        root = Path(args.workspace or '.').resolve()
        cfg = settings(root)
        if args.tui:
            from .tui import HUDApp
            HUDApp(ServerState(root, quiet=True).workbench.studio).run()
            return 0
        import uvicorn

        from .app import create_app
        hosts = ['127.0.0.1', 'localhost', '::1'] + args.allow_host
        app = create_app(ServerState(root, quiet=True).workbench.studio, os.environ.get('NEXUS_HUD_TOKEN', ''), allowed_hosts=hosts)
        uvicorn.run(app, host='0.0.0.0' if args.expose else cfg['host'], port=cfg['port'], access_log=False, proxy_headers=False,
                    ws_max_size=4096, ws_max_queue=4)
        return 0
    except ImportError:
        print('Install optional HUD dependencies: python -m pip install "nexus-agent[hud]"')
        return 2
    except (ValueError, OSError):
        print('HUD refused: inspect hud.yaml, explicit hosts and private NEXUS_HUD_TOKEN. No daemon started.')
        return 2


def add_parser(sub):
    parser = sub.add_parser('hud', help='authenticated live HUD; no implicit daemon/model execution')
    parser.add_argument('-C', '--workspace')
    parser.add_argument('--tui', action='store_true', help='local Textual UI with the same policies and exact review; no HTTP listener')
    parser.add_argument('--expose', action='store_true', help='explicit non-loopback bind; mandatory auth and exact allowed hosts still apply')
    parser.add_argument('--allow-host', action='append', default=[], help='explicit trusted hostname only, for a reverse proxy/preview; never wildcard')
    parser.set_defaults(func=command)
