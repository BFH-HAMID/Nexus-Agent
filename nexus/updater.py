"""Explicit, trusted-source updater. Never runs at startup or inside an agent tool."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

from filelock import FileLock, Timeout

REPOSITORY = 'https://github.com/BFH-HAMID/Nexus-Agent.git'
API = 'https://api.github.com/repos/BFH-HAMID/Nexus-Agent/git/ref/heads/main'
EXTRAS = {'context', 'mcp', 'hud', 'dev', 'orchestrator', 'computer', 'secure-state', 'anthropic'}


def run(argv: list[str], *, timeout: int = 60) -> str:
    """No shell or raw subprocess output in CLI receipts (may contain private data)."""
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'PIP_NO_INPUT': '1'}
    try:
        result = subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError('Command unavailable or timed out; no automatic retry/rollback.') from exc
    if result.returncode:
        raise UpdateError('Command failed; private subprocess output withheld. Check network, Git/Python permissions and environment health.')
    return result.stdout.strip()


class UpdateError(RuntimeError):
    """A safe, user-visible updater failure."""


def active_nexus() -> bool:
    """Best-effort local CLI/module detection; not proof about every custom launcher."""
    import psutil

    for process in psutil.process_iter(['pid', 'cmdline']):
        if process.info['pid'] == os.getpid():
            continue
        argv = process.info.get('cmdline') or []
        for index, arg in enumerate(argv):
            if Path(arg).name.lower() in {'nexus', 'nexus.exe', 'nexus-script.py'}:
                return True
            if arg == '-m' and index + 1 < len(argv) and (argv[index + 1] == 'nexus' or argv[index + 1].startswith('nexus.')):
                return True
    return False


def runtime_active() -> bool:
    if os.name != 'nt':
        return active_nexus()
    # Keep psutil's native Windows DLL out of the updater process so pip can
    # replace it. The probe exits before installation begins.
    result = run([sys.executable, '-c',
                  'from nexus.updater import active_nexus; print(int(active_nexus()))'])
    if result not in {'0', '1'}:
        raise UpdateError('Cannot determine local Nexus process state.')
    return result == '1'


def windows_handoff(root: Path, *, pull: bool, reinstall: bool, extras: str) -> Path:
    """Let the console launcher exit before pip tries to replace nexus.exe."""
    import tempfile

    directory = lock_directory() / 'update-jobs'
    directory.mkdir(parents=True, exist_ok=True)
    job = Path(tempfile.mkdtemp(prefix='update-', dir=directory))
    worker = job / 'worker.py'
    worker.write_bytes(Path(__file__).read_bytes())
    receipt = job / 'result.json'
    # This generated worker uses only validated local values serialized by repr.
    # Wait on the parent process handle, not a blind delay; no shell/elevation.
    script = f"""
if __name__ == '__main__':
    import ctypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    from ctypes import wintypes
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, {os.getpid()!r})
    ready = not handle and ctypes.get_last_error() == 87  # parent already exited
    if handle:
        ready = kernel.WaitForSingleObject(handle, 120000) == 0
        kernel.CloseHandle(handle)
    if ready:
        ok, messages = update(Path({str(root)!r}), pull={pull!r}, reinstall={reinstall!r}, extras={extras!r}, log=print)
    else:
        ok, messages = False, ['Console launcher did not exit; no update started.']
    Path({str(receipt)!r}).write_text(json.dumps({{'ok': ok, 'messages': messages}}), encoding='utf-8')
    print('\\n'.join(messages), flush=True)
    print('Update receipt: ' + {str(receipt)!r}, flush=True)
    raise SystemExit(0 if ok else 1)
"""
    with worker.open('a', encoding='utf-8') as handle:
        handle.write(script)
    subprocess.Popen([sys.executable, str(worker)], cwd=job,
                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return receipt


def latest_commit() -> str:
    request = urllib.request.Request(API, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'Nexus-updater'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError('Response too large')
        sha = json.loads(payload)['object']['sha']
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha):
            raise ValueError('Invalid commit')
        return sha
    except Exception as exc:
        raise UpdateError('Cannot resolve trusted GitHub main revision. No package installed.') from exc


def lock_directory() -> Path:
    # One lock per OS user, regardless of workspace/NEXUS_HOME overrides.
    return Path.home() / '.config' / 'nexus'


def update(root: Path, *, pull: bool = True, reinstall: bool = True,
           check: bool = False, extras: str = '', log: Callable[[str], None] | None = None) -> tuple[bool, list[str]]:
    """Install latest trusted main in this interpreter, with honest partial outcomes."""
    messages: list[str] = []
    log = log or (lambda message: None)
    selected = sorted(set(filter(None, (part.strip() for part in extras.split(',')))))
    if set(selected) - EXTRAS:
        return False, ['Unknown extras. Choose from: ' + ', '.join(sorted(EXTRAS))]
    suffix = '[' + ','.join(selected) + ']' if selected else ''
    try:
        home = lock_directory()
        home.mkdir(parents=True, exist_ok=True)
        with FileLock(str(home / 'self-update.lock'), timeout=0):
            checkout = (root / '.git').exists()
            if not check and runtime_active():
                raise UpdateError('Another local Nexus CLI/module process is running. Stop GUI, workers, Telegram and services before updating.')
            if checkout:
                git = ['git', '-C', str(root)]
                if Path(run(git + ['rev-parse', '--show-toplevel'])).resolve() != root.resolve():
                    raise UpdateError('Installation is not the checkout root.')
                if run(git + ['symbolic-ref', '--short', 'HEAD']) != 'main':
                    raise UpdateError('Updates require the main branch. Feature branches and detached HEAD are never switched automatically.')
                if run(git + ['status', '--porcelain', '--untracked-files=normal']):
                    raise UpdateError('Local changes/untracked files found. Commit or back them up yourself; no stash, reset or clean is performed.')
                origin = run(git + ['remote', 'get-url', 'origin'])
                if origin not in {REPOSITORY, REPOSITORY[:-4], 'git@github.com:BFH-HAMID/Nexus-Agent.git', 'ssh://git@github.com/BFH-HAMID/Nexus-Agent.git'}:
                    raise UpdateError('Origin is not the trusted BFH-HAMID/Nexus-Agent repository.')
                before = run(git + ['rev-parse', 'HEAD'])
                if check:
                    remote = run(git + ['ls-remote', REPOSITORY, 'refs/heads/main'], timeout=180).split()
                    if not remote or not re.fullmatch('[0-9a-f]{40}', remote[0]):
                        raise UpdateError('Trusted main revision unavailable.')
                    return True, ['Update available.' if remote[0] != before else 'Code matches trusted main.', 'Check only: no fetch, merge or dependency installation.']
                if pull:
                    log('Fetching trusted GitHub main (fast-forward only).')
                    run(git + ['fetch', '--no-tags', REPOSITORY, 'refs/heads/main'], timeout=180)
                    revision = run(git + ['rev-parse', 'FETCH_HEAD'])
                    if not re.fullmatch('[0-9a-f]{40}', revision):
                        raise UpdateError('Invalid fetched revision.')
                    # ff-only alone accepts a local-ahead checkout without changing it.
                    run(git + ['merge-base', '--is-ancestor', before, revision])
                    run(git + ['merge', '--ff-only', '--no-overwrite-ignore', revision], timeout=180)
                    messages.append('Checkout is now at trusted main revision ' + revision[:12] + '. No local files were force-cleaned.')
                else:
                    messages.append('Git fetch/merge skipped (--no-pull); reinstalling current official-origin checkout only (upstream revision not verified).')
                source = str(root) + suffix
                install = ['-e', source]
            else:
                if not pull or not reinstall:
                    raise UpdateError('--no-pull/--no-install apply only to Git checkouts; packaged installs use the trusted GitHub archive.')
                revision = latest_commit()
                messages.append('Trusted GitHub main revision: ' + revision[:12])
                if check:
                    return True, messages + ['Check only: no installation. This reports latest source, not installed commit equivalence.']
                source = f'nexus-agent{suffix} @ https://github.com/BFH-HAMID/Nexus-Agent/archive/{revision}.zip'
                # Force rebuild even if upstream has not bumped the package version.
                install = ['--force-reinstall', source]
            if not reinstall:
                return True, messages + ['CODE ONLY: dependency installation and health checks skipped (--no-install). Restart after completing installation.']
            log('Installing code and dependencies using the current Python interpreter.')
            run([sys.executable, '-m', 'pip', 'install', '--upgrade', *install], timeout=900)
            messages.append('Package/dependency installation command completed.')
            run([sys.executable, '-m', 'pip', 'check'], timeout=120)
            smoke = 'import nexus, nexus.cli; from importlib.metadata import version; assert version("nexus-agent"); assert callable(nexus.cli.main)'
            run([sys.executable, '-I', '-c', smoke], timeout=60)
            messages.extend(['Dependency and isolated CLI import checks passed (not a live-provider test).',
                             'Update completed. Restart Nexus/GUI and restart this terminal session if using the REPL.'])
            return True, messages
    except Timeout:
        return False, ['Another updater holds the update lock. No second update started.']
    except (UpdateError, OSError) as exc:
        reason = str(exc) if isinstance(exc, UpdateError) else 'Local update files unavailable; check permissions.'
        return False, messages + [reason, 'Update incomplete. Git or dependencies may already have changed; no automatic rollback. Keep Nexus stopped and retry after resolving the problem.']
