"""Foreground private-chat polling; explicit opt-in, no webhook registration."""
from pathlib import Path


def command(args):
    from ..server import ServerState
    from .bot import Bot
    bot = None
    try:
        bot = Bot(ServerState(Path(args.workspace or '.').resolve(), quiet=True).workbench.studio)
        bot.run()
        return 0
    except (ValueError, OSError, TimeoutError, ImportError, KeyboardInterrupt):
        print('Telegram polling stopped/refused. Inspect private env references, whitelist and policy. No token/PIN/error body shown.')
        return 2
    finally:
        if bot:
            bot.close()


def add_parser(sub):
    parser = sub.add_parser('telegram', help='explicit whitelisted private-chat long polling; PIN-protected approvals')
    parser.add_argument('-C', '--workspace')
    parser.set_defaults(func=command)
