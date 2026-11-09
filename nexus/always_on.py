"""Explicit opt-in GUI supervisor; owns only the child it launches.

No new schedule, Telegram poller, microphone or capture is enabled here. Existing
operator-configured workflow schedules keep their original gates. Crash
restart restores the HTTP service, NOT in-flight commands/approval grants. Logs
contain lifecycle metadata only; unbounded child stdout is never captured.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from filelock import FileLock

from .hud.presence_policy import load
from .hud.presence_store import Store, directory, safe_file


def require_service(root):
    cfg = load(root)['service']
    if not cfg['enabled']:
        raise ValueError('Always-on service is OFF in operator hud.yaml')
    return cfg


def backoff(attempt, cfg):
    return min(cfg['restart_max_seconds'], cfg['restart_initial_seconds'] * 2 ** min(max(0, attempt - 1), 20))


def health(root, *, now=None):
    cfg = load(root)
    row = Store(root, 'service', lambda: {}).read()
    stamp = time.time() if now is None else now
    fresh = isinstance(row.get('at'), (int, float)) and 0 <= stamp - row['at'] < 15
    return {'enabled': cfg['service']['enabled'], 'state': row.get('state', 'not_started') if fresh else 'not_observed',
            'heartbeat_fresh': fresh, 'child_running': bool(fresh and row.get('child_running')),
            'presence_enabled': cfg['presence']['enabled'],
            'auth_configured_in_this_process': bool(os.environ.get('NEXUS_SERVE_TOKEN')),
            'note': 'Local supervisor observation, not a network/model/device health check. No PID is signalled.'}


def run(root, *, popen=None, stop_event=None, clock=time.monotonic):
    root = Path(root).resolve()
    cfg = require_service(root)
    # Service managers do not normally inherit an interactive shell's environment.
    from dotenv import load_dotenv
    load_dotenv(root / '.env', override=False)
    from .gui_session import launch_credential
    token = launch_credential(root)
    if not token:
        raise ValueError('Always-on HTTP requires NEXUS_SERVE_TOKEN or an enrolled native vault credential')
    stop = stop_event if stop_event is not None else threading.Event()
    spawn = popen or subprocess.Popen
    store = Store(root, 'service', lambda: {})
    folder = directory(root)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'posix' and folder.stat().st_mode & 0o077:
        raise ValueError('Private supervisor directory required')
    lock_path, log_path = folder / 'supervisor.lock', folder / 'service.log'
    for path in [lock_path, log_path, *[folder / f'service.log.{i}' for i in range(1, cfg['log_backups'] + 1)]]:
        safe_file(path, max_bytes=cfg['log_bytes'] + 4096)
    # Ensure logging does not create a world-readable file under a permissive umask.
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(fd)
    class PrivateLog(RotatingFileHandler):
        def _open(self):
            path = Path(self.baseFilename)
            safe_file(path, max_bytes=cfg['log_bytes'] + 4096)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            return os.fdopen(fd, 'a', encoding='utf-8')
    handler = PrivateLog(log_path, maxBytes=cfg['log_bytes'], backupCount=cfg['log_backups'], encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger = logging.Logger('nexus-presence-supervisor', level=logging.INFO)
    logger.addHandler(handler)
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, lambda *_args: stop.set())
    child = None
    attempt = 0
    owned = False
    def report(state, **fields):
        store.update(lambda row: row.update(at=time.time(), state=state,
                                           child_running=bool(child is not None and child.poll() is None), **fields))
    try:
        with FileLock(str(lock_path), timeout=0, mode=0o600):
            owned = True
            while not stop.is_set():
                if require_service(root) != cfg:
                    raise ValueError('Service policy changed; explicit manager restart required')
                argv = [sys.executable, '-I', '-m', 'nexus', 'serve', '-C', str(root),
                        '--host', cfg['host'], '--port', str(cfg['port']), '--quiet']
                env = {**os.environ, 'NEXUS_SERVE_TOKEN': token}
                env.pop('NEXUS_ASSISTANT_SCHEDULER', None)
                started = clock()
                try:
                    child = spawn(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, env=env, close_fds=True,
                                  start_new_session=os.name == 'posix',
                                  creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) if os.name == 'nt' else 0)
                    logger.info('child started; no command/task replay')
                    while not stop.is_set() and child.poll() is None:
                        if require_service(root) != cfg:
                            stop.set()
                            break
                        report('running', attempt=attempt)
                        stop.wait(2)
                    if stop.is_set():
                        break
                    code = child.wait(timeout=5)
                except OSError:
                    code = -1
                if code == 0:
                    logger.info('child exited normally; no restart')
                    break
                attempt = 1 if clock() - started >= 60 else attempt + 1
                delay = backoff(attempt, cfg)
                logger.warning('child exited code=%d; backoff_seconds=%d', code, delay)
                deadline = clock() + delay
                while not stop.is_set() and clock() < deadline:
                    if require_service(root) != cfg:
                        stop.set()
                        break
                    report('backoff', attempt=attempt, retry_in_seconds=round(max(0, deadline - clock()), 1))
                    stop.wait(min(2, max(0, deadline - clock())))
            return 0
    finally:
        if child is not None and child.poll() is None:
            from .live_process import terminate_group
            terminate_group(child)  # Only an owned Popen; never a recorded/reused PID.
        if owned:
            report('stopped', attempt=attempt)
            logger.info('supervisor stopped')
        handler.close()
        for signum, old in previous.items():
            signal.signal(signum, old)


def command(args):
    root = Path(args.workspace or '.').resolve()
    try:
        if args.operation == 'run':
            return run(root)
        import hashlib
        import json

        from .jarvis import service_control, services
        if args.operation == 'health':
            result = health(root)
        elif args.operation in {'export', 'install'}:
            result = services.manifest(root, args.platform, args.user, target='presence')
            if args.operation == 'install':
                print(json.dumps(result, indent=2))
                phrase = 'INSTALL SERVICE ' + result['filename'] + ' ' + hashlib.sha256(result['content'].encode()).hexdigest()[:16]
                print(phrase)
                result = services.install(root, args.platform, args.user, target='presence',
                                          confirmation=input('Type the exact reviewed service confirmation: '))
        elif args.operation == 'status':
            result = service_control.inspect(root, args.platform, args.user, target='presence')
        else:
            review = service_control.inspect(root, args.platform, args.user, target='presence')
            print(json.dumps(review, indent=2))
            result = service_control.remove(root, args.platform, args.user, target='presence',
                                            confirmation=input('Type the exact current removal confirmation: '))
        print(json.dumps(result, indent=2))
        return 2 if result.get('installed') is False and args.operation == 'install' or result.get('removal_commands_accepted') is False else 0
    except (ValueError, OSError, TimeoutError, EOFError, ImportError):
        print('Service refused/stopped. Inspect operator hud.yaml, authentication and private state. No automatic install or elevation.')
        return 2


def add_parser(sub):
    parser = sub.add_parser('service', help='opt-in always-on authenticated GUI; reviewed user-service installation')
    parser.add_argument('-C', '--workspace')
    actions = parser.add_subparsers(dest='operation', required=True)
    for operation in ('export', 'install', 'status', 'remove'):
        child = actions.add_parser(operation)
        child.add_argument('--platform', choices=('systemd', 'windows'), required=True)
        child.add_argument('--user', default='', help='exact Windows local/domain user; no passwords')
    actions.add_parser('run')
    actions.add_parser('health')
    parser.set_defaults(func=command)
