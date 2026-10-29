"""Bounded public-feed consumers with disk TTL, stale fallback and no agent coupling."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml

from .sources import BINANCE, CRYPTO, GEOCODE, QUAKES, SOURCES, WEATHER

ASSETS = Path(__file__).parent / 'assets'
DEFAULT = {'schema': 1, 'enabled': True, 'language': 'en',
           'default_location': {'name': 'Dinajpur, Bangladesh', 'latitude': 25.63, 'longitude': 88.64},
           'weather_provider': 'weatherapi', 'refresh': {'news': 600, 'crypto': 120, 'weather': 900, 'quakes': 300},
           'sources': list(SOURCES), 'authorized_sources': [], 'layers': dict.fromkeys(('news', 'quakes', 'home', 'terminator', 'arcs'), True),
           'theme': {'accent': '#69d6ba', 'border': '#47bedd', 'atmosphere': '#278bdb'}}


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.suppressed = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'iframe', 'object'}:
            self.suppressed += 1
        elif tag in {'br', 'p', 'div'}:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'iframe', 'object'}:
            self.suppressed = max(0, self.suppressed - 1)
        self.parts.append(' ')

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def text(value, limit=400):
    parser = TextOnly()
    parser.feed(str(value or '')[:20000])
    return re.sub(r'\s+', ' ', ''.join(parser.parts)).strip()[:limit]


def link(value):
    try:
        p = urlsplit(str(value or '').strip())
        if (p.scheme not in {'https', 'http'} or not p.hostname or p.username or p.password
                or len(value) > 2048 or re.search(r'[\x00-\x20\x7f]', value)):
            return None
        return urlunsplit(p)
    except (ValueError, TypeError):
        return None


def settings(root):
    path = Path(root) / 'widget.yaml'
    data = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
    if data is None:
        data = {}
    if not isinstance(data, dict) or set(data) - set(DEFAULT):
        raise ValueError('Invalid widget.yaml fields')
    cfg = copy.deepcopy(DEFAULT)
    for key, value in data.items():
        if key in {'refresh', 'theme', 'layers', 'default_location'}:
            if not isinstance(value, dict) or set(value) - set(cfg[key]):
                raise ValueError('Invalid widget settings')
            cfg[key].update(value)
        else:
            cfg[key] = value
    if type(cfg['schema']) is not int or cfg['schema'] != 1 or type(cfg['enabled']) is not bool or not isinstance(cfg['language'], str) or cfg['language'] not in {'en', 'bn'}:
        raise ValueError('Invalid widget schema/language')
    if (not isinstance(cfg['sources'], list) or len(cfg['sources']) > len(SOURCES)
            or any(not isinstance(s, str) or s not in SOURCES for s in cfg['sources'])):
        raise ValueError('Select known feed IDs; arbitrary URLs are refused')
    if cfg['weather_provider'] not in {'weatherapi', 'open-meteo'}:
        raise ValueError('Choose WeatherAPI or explicit legacy Open-Meteo')
    cfg['sources'] = list(dict.fromkeys(cfg['sources']))
    if not isinstance(cfg['authorized_sources'], list) or any(not isinstance(s, str) or s not in SOURCES for s in cfg['authorized_sources']):
        raise ValueError('Invalid authorized source IDs')
    for k, minimum in DEFAULT['refresh'].items():
        v = cfg['refresh'][k]
        if type(v) is not int or not minimum <= v <= 86400:
            raise ValueError('Refresh below provider-friendly minimum')
    if any(type(v) is not bool for v in cfg['layers'].values()):
        raise ValueError('Layer switches must be boolean')
    if any(not isinstance(v, str) or not re.fullmatch('#[0-9A-Fa-f]{6}', v) for v in cfg['theme'].values()):
        raise ValueError('Theme colors must be hex')
    location = cfg['default_location']
    coordinates(location['latitude'], location['longitude'])
    if not isinstance(location['name'], str) or not 1 <= len(location['name']) <= 120:
        raise ValueError('Invalid default location name')
    return cfg


def coordinates(lat, lon):
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in (lat, lon)) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('Invalid coordinates')


class Cache:
    """Serialized misses, atomic bounded disk snapshots; failed fetch never erases data."""
    def __init__(self, directory, clock=time.time):
        self.directory, self.clock = Path(directory), clock
        self.lock = threading.RLock()
        self.rows, self.retry_after = {}, {}
        self.revision = 0
        path = self.directory / 'public-cache.json'
        try:
            if path.stat().st_size < 8_000_000:
                rows = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(rows, dict):
                    self.rows = {k: v for k, v in list(rows.items())[:64] if isinstance(v, dict) and isinstance(v.get('at'), (int, float)) and 'data' in v}
        except (OSError, ValueError, TypeError):
            pass
        self.key_locks = [threading.Lock() for _ in range(32)]

    def get(self, key, ttl, loader):
        guard = self.key_locks[int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 32]
        with guard:
            now = self.clock()
            old = self.rows.get(key)
            if old and 0 <= now - old['at'] < ttl:
                return {**copy.deepcopy(old), 'status': 'cached', 'offline': False, 'stale': False}
            if self.retry_after.get(key, 0) > now:
                return {**copy.deepcopy(old or {'at': None, 'data': None}), 'status': 'offline', 'offline': True, 'stale': bool(old)}
            try:
                data = loader()
                row = {'at': self.clock(), 'data': data}
                encoded = json.dumps(row, allow_nan=False)
                if len(encoded) > 3_000_000:
                    raise ValueError('Response exceeds public cache budget')
                with self.lock:
                    self.rows[key] = row
                    self.revision += 1
                    while len(self.rows) > 64:
                        del self.rows[min(self.rows, key=lambda k: self.rows[k]['at'])]
                    self.directory.mkdir(parents=True, exist_ok=True)
                    while len(json.dumps(self.rows)) > 7_500_000 and len(self.rows) > 1:
                        del self.rows[min((k for k in self.rows if k != key), key=lambda k: self.rows[k]['at'])]
                    output = self.directory / 'public-cache.json'
                    temporary = output.with_suffix('.tmp')
                    temporary.write_text(json.dumps(self.rows, allow_nan=False))
                    temporary.replace(output)
                return {**copy.deepcopy(row), 'status': 'live', 'offline': False, 'stale': False}
            except Exception:
                with self.lock:
                    self.retry_after[key] = now + min(ttl, 120)
                    if len(self.retry_after) > 128:
                        self.retry_after = dict(sorted(self.retry_after.items(), key=lambda pair: pair[1], reverse=True)[:128])
                return {**copy.deepcopy(old or {'at': None, 'data': None}), 'status': 'offline', 'offline': True, 'stale': bool(old)}


class Matcher:
    def __init__(self, countries):
        self.patterns = []
        for c in countries:
            names = [c['name'], c.get('bn')]
            names += {'USA': ['United States', 'USA', 'U.S.'], 'GBR': ['United Kingdom', 'Britain', 'UK'],
                      'KOR': ['South Korea'], 'PRK': ['North Korea'], 'BGD': ['Bangladesh', 'বাংলাদেশ', 'Dinajpur', 'দিনাজপুর', 'Dhaka', 'ঢাকা', 'চট্টগ্রাম', 'রাজশাহী', 'সিলেট', 'খুলনা', 'বরিশাল', 'রংপুর', 'ময়মনসিংহ'],
                      'RUS': ['Russia'], 'CHN': ['China']}.get(c['id'], [])
            self.patterns.append((c['id'], re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(n) for n in names if n) + r')(?!\w)', re.I)))

    def match(self, title, summary):
        value = title + ' ' + summary
        return [code for code, pattern in self.patterns if pattern.search(value)]


def parse_feed(payload, source, matcher):
    import feedparser
    category, name, _, _ = SOURCES[source]
    parsed = feedparser.parse(payload)
    if not parsed.entries and parsed.bozo:
        raise ValueError('Invalid feed; preserve previous data')
    rows = []
    for entry in parsed.entries[:30]:
        title, summary = text(entry.get('title'), 240), text(entry.get('summary') or entry.get('description'), 360)
        url = link(entry.get('link'))
        if not title or not url:
            continue
        date = entry.get('published_parsed') or entry.get('updated_parsed')
        published = None
        if date:
            try:
                published = datetime(*date[:6], tzinfo=timezone.utc).timestamp()
            except (ValueError, OverflowError):
                pass
        rows.append({'id': hashlib.sha256(url.encode()).hexdigest()[:20], 'title': title, 'summary': summary,
                     'url': url, 'source': name, 'source_id': source, 'category': category,
                     'published': published, 'countries': matcher.match(title, summary)})
    return rows


class World:
    def __init__(self, root, *, cache_dir=None, transport=None, clock=time.time, sleep=time.sleep, weather_key=None):
        from .weatherapi import credential
        self.weather_key = weather_key or (lambda: credential(Path(root)))
        self.config = settings(root)
        self.countries = json.loads((ASSETS / 'countries.json').read_text(encoding='utf-8'))
        self.matcher = Matcher(self.countries)
        if cache_dir is None:
            from ...config import nexus_home
            cache_dir = nexus_home() / 'world' / hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
        self.cache = Cache(cache_dir, clock)
        self.transport, self.sleep = transport, sleep
        self.fetch_lock = threading.Lock()
        self.fetch_times = deque(maxlen=5000)

    def fetch(self, url, params=None, *, json_data=False):
        # Only callers below choose URL. No redirects, ambient credentials, proxies or cookies.
        for attempt in range(2):
            try:
                with self.fetch_lock:
                    now = self.cache.clock()
                    while self.fetch_times and now - self.fetch_times[0] >= 86400:
                        self.fetch_times.popleft()
                    if len(self.fetch_times) >= 5000 or sum(t > now - 60 for t in self.fetch_times) >= 24:
                        raise ValueError('Public source request budget reached')
                    self.fetch_times.append(now)
                with httpx.Client(timeout=httpx.Timeout(5, connect=3), follow_redirects=False, trust_env=False,
                                  transport=self.transport, headers={'User-Agent': 'Nexus-Local-HUD/1.0 (personal RSS reader)'}) as client:
                    with client.stream('GET', url, params=params) as response:
                        response.raise_for_status()
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            body.extend(chunk)
                            if len(body) > 3_000_000:
                                raise ValueError('Feed too large')
                        return json.loads(body) if json_data else bytes(body)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt:
                    raise
                self.sleep(.25)
            except httpx.HTTPStatusError as exc:
                # Do not retry 429/4xx or follow redirects (SSRF boundary).
                if attempt or exc.response.status_code < 500:
                    raise
                self.sleep(.25)
        raise ValueError('No response')

    def revisions(self):
        return {'revision': self.cache.revision, 'enabled': self.config['enabled']}

    def news(self):
        if not self.config['enabled']:
            return {'items': [], 'sources': [], 'status': 'disabled'}
        def one(source):
            if source == 'cointelegraph' and source not in self.config['authorized_sources']:
                return source, {'at': None, 'data': [], 'status': 'permission-required'}
            row = self.cache.get('news:' + source, self.config['refresh']['news'],
                                 lambda: parse_feed(self.fetch(SOURCES[source][2]), source, self.matcher))
            return source, row
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(one, self.config['sources']))
        items, sources = {}, []
        for source, row in results:
            for item in row['data'] or []:
                items.setdefault(item['url'], item)
            sources.append({'id': source, 'name': SOURCES[source][1], 'at': row['at'], 'status': row['status'],
                            'terms': SOURCES[source][3]})
        counts = {}
        now = self.cache.clock()
        for item in items.values():
            if item['published'] is not None and 0 <= now - item['published'] <= 86400:
                for country in item['countries']:
                    counts[country] = counts.get(country, 0) + 1
        offline = sum(s['status'] == 'offline' for s in sources)
        return {'items': sorted(items.values(), key=lambda r: r['published'] or 0, reverse=True)[:390],
                'hotspots': counts, 'sources': sources, 'status': 'offline' if sources and offline == sum(s['status'] != 'permission-required' for s in sources) else 'partial' if offline else 'ready',
                'notice': 'Untrusted RSS excerpts; heuristic country matching, not geolocation. Dinajpur coverage depends on publishers.'}

    def weather(self, lat=None, lon=None):
        lat = self.config['default_location']['latitude'] if lat is None else lat
        lon = self.config['default_location']['longitude'] if lon is None else lon
        coordinates(lat, lon)
        lat, lon = round(lat, 2), round(lon, 2)
        provider = self.config['weather_provider']
        if provider == 'weatherapi' and not self.weather_key():
            return {'provider': 'WeatherAPI.com', 'status': 'configuration-required', 'data': None, 'at': None, 'offline': True, 'stale': False}
        def load():
            params = {'latitude': lat, 'longitude': lon, 'timezone': 'auto', 'forecast_days': 7, 'timeformat': 'unixtime',
                      'current': 'temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,precipitation,weather_code,is_day',
                      'hourly': 'temperature_2m,precipitation_probability,precipitation,weather_code',
                      'daily': 'temperature_2m_max,temperature_2m_min,weather_code,sunrise,sunset,uv_index_max,precipitation_sum,precipitation_probability_max,wind_speed_10m_max'}
            if provider == 'weatherapi':
                from .weatherapi import FORECAST, normalize
                data = normalize(self.fetch(FORECAST, {'key': self.weather_key(), 'q': f'{lat},{lon}', 'days': 3, 'aqi': 'no', 'alerts': 'no'}, json_data=True))
            else:
                data = self.fetch(WEATHER, params, json_data=True)
                data.update(provider='Open-Meteo', provider_url='https://open-meteo.com/')
            if not isinstance(data.get('current'), dict) or not isinstance(data.get('daily'), dict) or not isinstance(data.get('hourly'), dict):
                raise ValueError('Invalid weather')
            allowed = ('current', 'current_units', 'hourly', 'hourly_units', 'daily', 'daily_units', 'timezone', 'utc_offset_seconds', 'provider', 'provider_url', 'forecast_days')
            result = {k: data.get(k) for k in allowed}
            daily = data['daily']
            alerts = []
            if any(isinstance(v, (float, int)) and v >= 50 for v in daily.get('precipitation_sum', [])):
                alerts.append('Heavy rain forecast ≥50 mm/day')
            if any(isinstance(v, (float, int)) and v >= 40 for v in daily.get('temperature_2m_max', [])):
                alerts.append('Extreme heat forecast ≥40°C')
            if any(v in (95, 96, 99) for v in daily.get('weather_code', [])):
                alerts.append('Thunderstorm forecast')
            result.update(alerts=alerts, latitude=lat, longitude=lon, alert_notice='Local threshold advisory, NOT an official warning')
            return result
        return self.cache.get(f'weather:{provider}:{lat}:{lon}', self.config['refresh']['weather'], load)

    def cities(self, name):
        name = text(name, 80)
        if len(name) < 2:
            raise ValueError('Enter at least two characters')
        if self.config['weather_provider'] == 'weatherapi':
            from .weatherapi import SEARCH
            if not self.weather_key():
                return {'status': 'configuration-required', 'at': None, 'data': None, 'offline': True, 'stale': False}
            def search():
                rows = self.fetch(SEARCH, {'key': self.weather_key(), 'q': name}, json_data=True)
                result = []
                for row in rows[:5]:
                    coordinates(row['lat'], row['lon'])
                    result.append({'name': text(row['name'] + ', ' + row['country'], 100), 'latitude': row['lat'], 'longitude': row['lon']})
                return result
            return self.cache.get('city:weatherapi:' + name.casefold(), 86400, search)
        def load():
            data = self.fetch(GEOCODE, {'name': name, 'count': 5, 'language': self.config['language'], 'format': 'json'}, json_data=True)
            result = []
            for row in data.get('results', [])[:5]:
                coordinates(row['latitude'], row['longitude'])
                result.append({'name': text(row.get('name'), 100) + ', ' + text(row.get('country'), 100),
                               'latitude': row['latitude'], 'longitude': row['longitude']})
            return result
        return self.cache.get('city:' + name.casefold(), 86400, load)

    def quakes(self):
        def load():
            data = self.fetch(QUAKES, json_data=True)
            rows = []
            for feature in data.get('features', [])[:2000]:
                p, geometry = feature.get('properties', {}), feature.get('geometry', {})
                coords = geometry.get('coordinates', [])
                if len(coords) < 2 or not isinstance(p.get('time'), (int, float)):
                    continue
                lon, lat = coords[:2]
                coordinates(lat, lon)
                if not 0 <= self.cache.clock() - p['time'] / 1000 <= 86400:
                    continue
                rows.append({'id': text(feature.get('id'), 80), 'lat': lat, 'lon': lon, 'magnitude': p.get('mag'),
                             'place': text(p.get('place'), 180), 'time': p['time'] / 1000, 'url': link(p.get('url'))})
            return rows
        return self.cache.get('quakes', self.config['refresh']['quakes'], load)

    def crypto(self):
        def load():
            try:
                data = self.fetch(CRYPTO, {'vs_currency': 'usd', 'ids': 'bitcoin,ethereum,solana,binancecoin,ripple,dogecoin',
                                          'sparkline': 'true', 'price_change_percentage': '24h'}, json_data=True)
                if not isinstance(data, list) or not data:
                    raise ValueError('Empty crypto quotes')
                return {'provider': 'CoinGecko', 'items': [
                    {'symbol': text(c['symbol'], 12).upper(), 'price': float(c['current_price']),
                     'change': c.get('price_change_percentage_24h'), 'spark': c.get('sparkline_in_7d', {}).get('price', [])[-48:],
                     'quote': 'USD', 'spark_period': 'last ~48h (CoinGecko hourly)'} for c in data[:6]]}
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                tickers = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT']
                data = self.fetch(BINANCE, {'symbols': json.dumps(tickers)}, json_data=True)
                if not isinstance(data, list) or not data:
                    raise ValueError('Empty fallback quotes') from None
                old = (self.cache.rows.get('crypto', {}).get('data') or {}).get('items', [])
                prior = {c['symbol']: c for c in old if c.get('quote') == 'USDT'}
                items = []
                for c in data[:6]:
                    symbol, price = c['symbol'].removesuffix('USDT'), float(c['lastPrice'])
                    items.append({'symbol': symbol, 'price': price, 'change': float(c['priceChangePercent']), 'quote': 'USDT',
                                  'spark': (prior.get(symbol, {}).get('spark', []) + [price])[-48:],
                                  'spark_period': 'locally observed samples only'})
                return {'provider': 'Binance fallback', 'items': items}
        return self.cache.get('crypto', self.config['refresh']['crypto'], load)
