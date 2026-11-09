"""Read-only adapter for the existing World widget and its existing public cache.

The mobile HUD cannot call World.fetch (even on cache miss). Cache refresh stays
on the operator's original reviewed World surface. Public data remains display
only; it is never concatenated into a conversation or a permission request.
"""
from __future__ import annotations

import copy

from .presence_policy import require
from .world.core import coordinates, settings
from .world.sources import SOURCES


def cached(state, path, query):
    require(state.workspace, 'world_enabled')
    cfg = settings(state.workspace)
    world = state.gui.world()
    if path == 'countries':
        if query:
            raise ValueError('Countries view takes no query')
        return {'items': world.countries, 'config': cfg, 'source': 'Bundled Natural Earth admin-0; cache-only HUD'}
    if not cfg['enabled']:
        raise ValueError('World is OFF in widget.yaml')
    if path not in {'news', 'crypto', 'quakes', 'weather', 'cities'}:
        raise ValueError('Unknown cached World view')
    allowed = {'weather': {'latitude', 'longitude'}, 'cities': {'q'}}.get(path, set())
    if set(query) - allowed or any(len(v) != 1 for v in query.values()):
        raise ValueError('Unexpected World query')
    with world.cache.lock:
        rows = copy.deepcopy(world.cache.rows)
    now = world.cache.clock()
    def row(key, ttl):
        found = rows.get(key) or {}
        at = found.get('at')
        fresh = type(at) in (int, float) and 0 <= now - at < ttl
        return {'at': at, 'data': found.get('data'), 'status': 'cached' if fresh else 'offline',
                'offline': not fresh, 'stale': bool(found) and not fresh, 'cache_only': True}
    if path == 'weather':
        if ('latitude' in query) != ('longitude' in query):
            raise ValueError('Both coordinates required')
        lat = float(query.get('latitude', [cfg['default_location']['latitude']])[0])
        lon = float(query.get('longitude', [cfg['default_location']['longitude']])[0])
        coordinates(lat, lon)
        return row(f"weather:{cfg['weather_provider']}:{round(lat, 2)}:{round(lon, 2)}", cfg['refresh']['weather'])
    if path == 'cities':
        name = query.get('q', [''])[0]
        if not 2 <= len(name) <= 80:
            raise ValueError('Bounded city name required')
        key = 'city:' + ('weatherapi:' if cfg['weather_provider'] == 'weatherapi' else '') + name.casefold()
        return row(key, 86400)
    if path in {'crypto', 'quakes'}:
        return row(path, cfg['refresh'][path])
    items, sources = {}, []
    for source in cfg['sources']:
        found = row('news:' + source, cfg['refresh']['news'])
        for item in (found['data'] or [])[:100]:
            items.setdefault(item['url'], item)
        sources.append({'id': source, 'name': SOURCES[source][1], 'terms': SOURCES[source][3],
                        'at': found['at'], 'status': found['status']})
    listing = sorted(items.values(), key=lambda r: r.get('published') or 0, reverse=True)[:120]
    counts = {}
    for item in listing:
        stamp = item.get('published')
        if type(stamp) in (int, float) and 0 <= now - stamp <= 86400:
            for country in item.get('countries', []):
                counts[country] = counts.get(country, 0) + 1
    return {'items': listing, 'sources': sources, 'hotspots': counts, 'status': 'cached' if any(
        s['status'] == 'cached' for s in sources) else 'offline', 'cache_only': True,
        'notice': 'Existing public cache only. No publisher contacted; text is untrusted display data.'}
