"""Bounded untrusted event admission. No event can grant execution permission."""
import hashlib
import json
import time

from ..permissions import Permissions

SOURCES = {'file', 'schedule', 'build', 'todo', 'git', 'ci', 'system', 'inbox', 'idle', 'user'}


def settings(values, workspace):
    from ..github_safety import scan_text
    if not isinstance(values, list) or len(values) > 10:
        raise ValueError('At most ten explicit triggers allowed')
    seen = set()
    for item in values:
        if not isinstance(item, dict) or set(item) - {'id', 'kind', 'path', 'every_seconds', 'task', 'priority', 'repo', 'ref', 'metric', 'threshold', 'after_seconds', 'poll_seconds'}:
            raise ValueError('Invalid trigger')
        ident = item.get('id')
        if not isinstance(ident, str) or not ident.isascii() or not ident.isidentifier() or len(ident) > 50 or ident in seen:
            raise ValueError('Unique bounded trigger ID required')
        seen.add(ident)
        if not isinstance(item.get('kind'), str) or item['kind'] not in SOURCES - {'user'}:
            raise ValueError('Unknown automatic trigger')
        task = item.get('task')
        if not isinstance(task, str) or not 0 < len(task) <= 8000:
            raise ValueError('Bounded trigger task required')
        scan_text(task, workspace=workspace)
        if type(item.get('priority', 5)) is not int or not 0 <= item.get('priority', 5) <= 9:
            raise ValueError('Invalid trigger priority')
        if type(item.get('poll_seconds', 60)) is not int or not 1 <= item.get('poll_seconds', 60) <= 3600:
            raise ValueError('Invalid probe poll interval')
        if item['kind'] in {'file', 'todo', 'build'}:
            if not isinstance(item.get('path'), str):
                raise ValueError('File trigger path required')
            Permissions(workspace).path(item['path'])
        elif item['kind'] == 'ci':
            import re
            if (not isinstance(item.get('repo'), str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', item['repo'])
                    or not isinstance(item.get('ref', 'HEAD'), str) or not 0 < len(item.get('ref', 'HEAD')) <= 180
                    or item.get('poll_seconds', 60) < 60):
                raise ValueError('CI requires exact repository/ref and at least 60-second polling')
        elif item['kind'] == 'system':
            if item.get('metric') not in ('cpu', 'memory', 'disk_free') or type(item.get('threshold')) is not int or not 1 <= item['threshold'] <= 100:
                raise ValueError('Invalid system threshold')
        elif item['kind'] == 'idle':
            if type(item.get('after_seconds')) is not int or not 60 <= item['after_seconds'] <= 604800:
                raise ValueError('Invalid queue idle interval')
        elif item['kind'] == 'schedule' and (type(item.get('every_seconds')) is not int or not 60 <= item['every_seconds'] <= 604800):
            raise ValueError('Schedule interval must be 60 seconds–7 days')
    return values


class Events:
    def __init__(self, queue):
        self.queue = queue
        self.studio = queue.studio

    def publish(self, source, task, parent=None, priority=5):
        if not isinstance(source, str) or source not in SOURCES:
            raise ValueError('Unknown event source')
        # Existing depth/duplicate/TTL/secret/emergency/admission policy is authoritative.
        return self.queue.enqueue(task, priority=priority, parent=parent, source='event:' + source)

    def poll(self):
        policy = self.queue.policy()
        if policy['level'] == 'L0':
            return []
        emitted = []
        for trigger in policy['triggers']:
            key = 'jarvis-trigger-' + hashlib.sha256((str(self.queue.root) + ':' + trigger['id']).encode()).hexdigest()
            previous = next((r for r in self.studio.items('jarvis-trigger') if r['id'] == key), {})
            cfg = hashlib.sha256(json.dumps(trigger, sort_keys=True).encode()).hexdigest()
            current = {**(previous if previous.get('config') == cfg else {}), 'config': cfg, 'seen': time.time(), 'last_emit': previous.get('last_emit', 0),
                       'last_attempt': previous.get('last_attempt', 0),
                       'pending': previous.get('pending', False) if previous.get('config') == cfg else False}
            changed = False
            try:
                if trigger['kind'] == 'schedule':
                    changed = previous.get('config') == cfg and time.time() - previous.get('seen', time.time()) >= trigger['every_seconds']
                    if not changed and previous.get('config') == cfg:
                        current['seen'] = previous['seen']
                    parent = None
                else:
                    from .sources import sample
                    interval = trigger.get('poll_seconds', 60) if trigger['kind'] not in {'file', 'todo', 'build'} else trigger.get('poll_seconds', 0)
                    if previous.get('config') == cfg and time.time() - previous.get('polled', 0) < interval:
                        continue
                    state, condition, parent = sample(self.queue, trigger, previous)
                    current['polled'] = time.time()
                    current.update(state)
                    current['digest'] = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
                    changed = (current['digest'] != previous.get('digest') and
                               (previous.get('config') == cfg or condition is True))
                    if condition is not None:
                        changed = changed and condition
                    current['deferred'] = False
                if changed:
                    current['pending_parent'] = parent
                current['pending'] = current['pending'] or changed
                if current['pending'] and time.time() - previous.get('last_attempt', 0) >= policy['cooldown_seconds']:
                    if trigger not in self.queue.policy()['triggers']:
                        continue  # Revoked while a network probe was in flight.
                    job = self.publish(trigger['kind'], trigger['task'], parent=current.get('pending_parent'), priority=trigger.get('priority', 5))
                    emitted.append(job['id'])
                    current.update(last_emit=time.time(), last_attempt=time.time(), seen=time.time(), pending=False)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                # No raw file contents/remote errors in the state. Back off failures.
                current.update(error_type=type(exc).__name__, last_attempt=time.time(), deferred=True, polled=time.time())
            self.studio.put('jarvis-trigger', current, key)
        return emitted
