"""Actual opt-in probes. No project code, Git filters, inbox text or shell execution."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import quote

from ..permissions import Permissions


class Unavailable(ValueError):
    pass


def bounded(path, limit=64000):
    if path.is_symlink():
        raise ValueError('Probe symlink refused')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError('Probe requires bounded regular input')
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    if len(data) > limit or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Probe input changed or size budget exceeded')
    return data



def github(root, repo, ref):
    import httpx
    try:
        return _github(root, repo, ref)
    except (httpx.HTTPError, KeyError, TypeError):
        raise Unavailable('CI probe unavailable; retry after reconnection/operator attention') from None


def _github(root, repo, ref):
    import httpx

    from ..github_safety import token_from_environment
    token = token_from_environment(root)
    with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
        def get(path):
            with client.stream('GET', 'https://api.github.com/repos/' + repo + '/' + path,
                               headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'}) as response:
                if response.status_code != 200:
                    raise Unavailable('CI API unavailable; credentials/permissions/rate may need operator attention')
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 1000000:
                        raise ValueError('CI response budget exceeded')
                return json.loads(body)
        head = get('commits/' + quote(ref, safe=''))['sha']
        if not re.fullmatch(r'[a-fA-F0-9]{40}', head):
            raise ValueError('Invalid CI head')
        data = get('commits/' + head + '/check-runs?per_page=100')
        runs = data.get('check_runs')
        if not isinstance(runs, list) or len(runs) > 100 or data.get('total_count') != len(runs):
            raise ValueError('Incomplete CI response')
        # Names/logs are untrusted and unnecessary. Never persist/upload them.
        states = sorted((str(r.get('id')), r.get('status'), r.get('conclusion')) for r in runs)
        failing = any(state == 'completed' and result in {'failure', 'cancelled', 'timed_out', 'action_required'} for _, state, result in states)
        return {'sha': head, 'checks': states}, failing


def git(root):
    # Pure metadata: `git status` can execute configured clean filters; do NOT run it.
    directory = Path(root) / '.git'
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Git probe requires a regular local .git directory')
    head = bounded(directory / 'HEAD', 4096).decode().strip()
    if head.startswith('ref: '):
        ref = head[5:]
        if not re.fullmatch(r'refs/[A-Za-z0-9_./-]{1,180}', ref) or '..' in ref.split('/'):
            raise ValueError('Invalid Git symbolic reference')
        file = directory / ref
        if not file.resolve().is_relative_to(directory.resolve()):
            raise ValueError('Git metadata escape')
        if file.exists():
            head = bounded(file, 4096).decode().strip()
        elif (directory / 'packed-refs').exists():
            head = next((line.split(' ', 1)[0] for line in bounded(directory / 'packed-refs', 2000000).decode().splitlines()
                         if line.endswith(' ' + ref)), 'unborn')
        else:
            head = 'unborn'
    if head != 'unborn' and not re.fullmatch(r'[a-fA-F0-9]{40,64}', head):
        raise ValueError('Invalid Git head')
    index = directory / 'index'
    return {'head': head, 'index': hashlib.sha256(bounded(index, 2000000)).hexdigest() if index.exists() else None}


def sample(queue, trigger, previous):
    kind = trigger['kind']
    if kind in {'file', 'todo', 'build'}:
        path = Permissions(queue.root).path(trigger['path'])
        data = bounded(path) if path.exists() else b''
        state = {'digest': hashlib.sha256(data).hexdigest(), 'exists': path.exists()}
        condition = None
        if kind == 'todo':
            condition = bool(re.search(rb'\b(?:TODO|FIXME)\b', data))
        elif kind == 'build':
            report = json.loads(data) if data else {}
            code = report.get('exit_code')
            condition = type(code) is int and code != 0
        return state, condition, None
    if kind == 'git':
        return git(queue.root), None, None
    if kind == 'ci':
        if queue.policy()['runtime']['connectivity'] != 'online':
            raise Unavailable('Offline CI probe deferred')
        state, condition = github(queue.root, trigger['repo'], trigger.get('ref', 'HEAD'))
        return state, condition, None
    if kind == 'system':
        import psutil
        metric = trigger['metric']
        value = (psutil.cpu_percent(interval=None) if metric == 'cpu' else psutil.virtual_memory().percent if metric == 'memory'
                 else 100 * psutil.disk_usage(str(queue.root)).free / psutil.disk_usage(str(queue.root)).total)
        critical = value <= trigger['threshold'] if metric == 'disk_free' else value >= trigger['threshold']
        return {'critical': critical}, critical, None
    if kind == 'inbox':
        notices = [r for r in queue.studio.items('jarvis-notice') if not r['read']]
        seen = set(previous.get('observed_ids', []))
        fresh = [r for r in notices if r['id'] not in seen]
        ids = sorted(seen | {r['id'] for r in notices})[-1000:]
        parent = max(fresh, key=lambda r: r['created']).get('job_id') if fresh else None
        if parent and not any(j['id'] == parent for j in queue._jobs()):
            parent = None
        return {'observed_ids': ids}, bool(fresh), parent
    if kind == 'idle':
        rows = queue._jobs()
        active = any(r['status'] in {'awaiting_approval', 'approved', 'running'} for r in rows)
        latest = max((r.get('finished', r['created']) for r in rows), default=previous.get('baseline', time.time()))
        ready = not active and time.time() - latest >= trigger['after_seconds']
        return {'idle': ready, 'baseline': previous.get('baseline', time.time())}, ready, None
    raise ValueError('Unsupported sample')
