"""Operations and public information inside the existing GUI server/state.

No secondary HTTP server, scheduler, credential prompt or learning store.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque


class GuiModules:
    def __init__(self, state):
        self.state = state
        self.lock = threading.RLock()
        self._operations = None
        self._world = None
        self.slots = {'operations': threading.BoundedSemaphore(2), 'world': threading.BoundedSemaphore(3)}
        self.requests = {'operations': deque(), 'world': deque()}

    def operations(self):
        with self.lock:
            if self._operations is None:
                from .hud.core import HUD
                self._operations = HUD(self.state.workbench.studio, secret_values=[self.state.token] if self.state.token else [])
            return self._operations

    def world(self):
        with self.lock:
            if self._world is None:
                from .hud.world.core import World
                self._world = World(self.state.workspace)
            return self._world

    def call(self, method, path, query, payload=None):
        group = 'operations' if path.startswith('/api/operations/') else 'world'
        with self.lock:
            now = time.monotonic()
            bucket = self.requests[group]
            while bucket and now - bucket[0] > 60:
                bucket.popleft()
            if len(bucket) >= 120:
                return 429, {'error': 'Too many requests; retry shortly'}
            bucket.append(now)
        slot = self.slots[group]
        if not slot.acquire(blocking=False):
            return 429, {'error': 'Information service busy; operational controls use a separate budget'}
        try:
            return 200, self.dispatch(method, path, query, payload)
        except (ValueError, OSError, KeyError, TypeError):
            return 409, {'error': 'Request refused. Inspect current policy, exact confirmation and selected record.'}
        finally:
            slot.release()

    def dispatch(self, method, path, query, payload):
        if path.startswith('/api/operations/'):
            service = self.operations()
            route = path.removeprefix('/api/operations/')
            if method == 'POST' and route.startswith('action/'):
                if self.state.demo:
                    raise ValueError('Restart in live mode')
                return service.action(route.removeprefix('action/'), payload)
            if method != 'GET':
                raise ValueError('Unsupported method')
            if route == 'state':
                result = service.snapshot()
                # World is not initialized or fetched just by opening operations.
                result['world'] = self._world.revisions() if self._world else None
                return result
            kind, identifier = route.split('/', 1)
            if kind == 'trace':
                operation = query.get('operation', ['inspect'])[0]
                if operation not in {'inspect', 'why', 'replay'}:
                    raise ValueError('Inspection only')
                step = int(query['step'][0]) if 'step' in query else None
                return service.trace(identifier, operation, step)
            if kind in {'job', 'approval'}:
                return getattr(service, kind)(identifier)
            raise ValueError('Unknown operations view')
        if method != 'GET':
            raise ValueError('Public information is read-only')
        world = self.world()
        if path == '/api/countries':
            return {'items': world.countries, 'config': world.config, 'source': 'Bundled Natural Earth admin-0'}
        if not world.config['enabled']:
            raise ValueError('World widget disabled by operator')
        if path == '/api/weather':
            if ('latitude' in query) != ('longitude' in query):
                raise ValueError('Both coordinates required')
            lat = float(query['latitude'][0]) if 'latitude' in query else None
            lon = float(query['longitude'][0]) if 'longitude' in query else None
            if lat is not None and (not math.isfinite(lat) or not math.isfinite(lon) or abs(lat) > 90 or abs(lon) > 180):
                raise ValueError('Invalid coordinates')
            return world.weather(lat, lon)
        if path == '/api/cities':
            q = query.get('q', [''])[0]
            if not 2 <= len(q) <= 80:
                raise ValueError('City search needs 2–80 characters')
            return world.cities(q)
        if path in {'/api/news', '/api/crypto', '/api/quakes'}:
            return getattr(world, path.removeprefix('/api/'))()
        raise ValueError('Unknown public information view')


def module_path(path):
    return path.startswith('/api/operations/') or path in {
        '/api/countries', '/api/news', '/api/crypto', '/api/weather', '/api/cities', '/api/quakes'
    }
