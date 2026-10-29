"""Public information paths: real parser/cache/HTTP clients; upstream data scripted."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from nexus.hud.app import create_app
from nexus.hud.world.core import ASSETS, Cache, World, link, parse_feed, settings, text
from nexus.server import ServerState

FEED = b'''<rss version="2.0"><channel><title>Test</title><item><title>Bangladesh security news</title><description><![CDATA[<script>steal()</script><p>Dinajpur &amp; Dhaka</p>]]></description><link>https://example.org/report</link><pubDate>Thu, 29 Oct 2026 08:00:00 GMT</pubDate></item><item><title>bad</title><link>javascript:alert(1)</link></item></channel></rss>'''


@pytest.fixture
def world(repo, tmp_path):
    return World(repo, cache_dir=tmp_path / 'cache', transport=httpx.MockTransport(lambda r: httpx.Response(200, content=FEED)), sleep=lambda _: None)


def test_plain_text_sanitizer_never_retains_script_payload():
    assert text('<p>Hello &amp; <b>world</b></p><script>alert(1)</script><iframe>bad</iframe>') == 'Hello & world'
    assert len(text('x' * 50000)) <= 400
    assert text('&lt;img onerror=alert(1)&gt;') == '<img onerror=alert(1)>'  # Literal text, never inserted as HTML.


@pytest.mark.parametrize('value', ['javascript:alert(1)', 'data:text/html,x', '//evil.example', 'https://user:secret@a.org', 'https://a.org/\nfoo', 'file:///tmp/x'])
def test_only_absolute_http_links_without_credentials(value):
    assert link(value) is None


def test_country_matcher_word_boundaries_bangla_and_no_iso_false_matches(world):
    assert 'BGD' in world.matcher.match('দিনাজপুরে বৃষ্টি', '')
    assert 'RUS' in world.matcher.match('Russia reports', '')
    assert 'USA' in world.matcher.match('United States reports', '')
    assert 'IND' not in world.matcher.match('IN progress / business', '')
    assert 'OMN' not in world.matcher.match('Romania', '')


def test_real_feedparser_extracts_safe_excerpt_time_and_country(world):
    rows = parse_feed(FEED, 'bbc', world.matcher)
    assert len(rows) == 1 and rows[0]['summary'] == 'Dinajpur & Dhaka'
    assert rows[0]['countries'] == ['BGD'] and rows[0]['published'] > 0
    assert rows[0]['source'] == 'BBC News · World' and rows[0]['url'] == 'https://example.org/report'
    with pytest.raises(ValueError):
        parse_feed(b'<html>not rss', 'bbc', world.matcher)


def test_atom_and_absent_publisher_date_do_not_invent_time(world):
    rows = parse_feed(b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>France</title><link href="https://a.org"/><summary>Hello</summary></entry></feed>', 'dw', world.matcher)
    assert rows[0]['published'] is None and 'FRA' in rows[0]['countries']


def test_cache_ttl_negative_backoff_offline_and_restart(tmp_path):
    now = [1000]
    cache = Cache(tmp_path, clock=lambda: now[0])
    calls = []
    def load():
        calls.append(1)
        return ['actual cached item']
    assert cache.get('feed', 60, load)['status'] == 'live'
    assert cache.get('feed', 60, load)['status'] == 'cached' and len(calls) == 1
    now[0] += 61
    def fail():
        calls.append(1)
        raise httpx.ConnectError('offline')
    stale = cache.get('feed', 60, fail)
    assert stale['offline'] and stale['stale'] and stale['at'] == 1000 and stale['data'] == ['actual cached item']
    cache.get('feed', 60, fail)
    assert len(calls) == 2
    restart = Cache(tmp_path, clock=lambda: now[0])
    assert restart.get('feed', 60, fail)['data'] == ['actual cached item']
    empty = cache.get('empty', 60, fail)
    assert empty['data'] is None and empty['at'] is None


def test_same_key_cache_misses_are_coalesced(tmp_path):
    cache = Cache(tmp_path)
    calls = []
    def load():
        calls.append(1)
        threading.Event().wait(.03)
        return ['one']
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _: cache.get('same', 600, load), range(8)))
    assert len(calls) == 1 and all(row['data'] == ['one'] for row in rows)


def test_cache_disk_and_entry_budgets_are_bounded(tmp_path):
    cache = Cache(tmp_path)
    for i in range(70):
        cache.get(str(i), 60, lambda: 'x')
    assert len(cache.rows) == 64 and (tmp_path / 'public-cache.json').stat().st_size < 8_000_000
    bad = cache.get('bad', 60, lambda: float('nan'))
    assert bad['data'] is None and bad['offline']


def test_one_failed_news_source_does_not_drop_other_feed(repo, tmp_path):
    (repo / 'widget.yaml').write_text('sources: [bbc, dw]\n')
    def upstream(request):
        return httpx.Response(503) if request.url.host == 'rss.dw.com' else httpx.Response(200, content=FEED)
    w = World(repo, cache_dir=tmp_path / 'cache', transport=httpx.MockTransport(upstream), sleep=lambda _: None)
    row = w.news()
    assert row['status'] == 'partial' and len(row['items']) == 1
    assert {s['status'] for s in row['sources']} == {'live', 'offline'}


def test_requests_have_no_redirect_proxy_or_ambient_authorization(world):
    calls = []
    def upstream(request):
        calls.append(request)
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
    world.transport = httpx.MockTransport(upstream)
    with pytest.raises(httpx.HTTPStatusError):
        world.fetch('https://feeds.bbci.co.uk/news/world/rss.xml')
    assert len(calls) == 1


def test_429_is_not_retried(world):
    calls = []
    world.transport = httpx.MockTransport(lambda request: calls.append(1) or httpx.Response(429))
    with pytest.raises(httpx.HTTPStatusError):
        world.fetch('https://feeds.bbci.co.uk/news/world/rss.xml')
    assert len(calls) == 1


def test_timeout_retry_is_bounded_and_then_offline(world):
    calls = []
    def upstream(request):
        calls.append(1)
        raise httpx.ReadTimeout('scripted', request=request)
    world.transport = httpx.MockTransport(upstream)
    assert world.quakes()['offline'] and len(calls) == 2


def weather_payload():
    return {'current': {'temperature_2m': 32, 'weather_code': 0},
            'hourly': {'time': [1793260800], 'temperature_2m': [32]},
            'daily': {'precipitation_sum': [55], 'temperature_2m_max': [41], 'weather_code': [95]}, 'timezone': 'Asia/Dhaka'}


def test_weather_forecast_thresholds_and_no_official_warning_claim(world):
    calls = []
    world.transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json=weather_payload()))
    row = world.weather()
    assert len(row['data']['alerts']) == 3 and 'NOT an official' in row['data']['alert_notice']
    assert calls[0].url.params['latitude'] == '25.63' and calls[0].url.params['forecast_days'] == '7'
    assert world.weather()['status'] == 'cached' and len(calls) == 1
    for lat, lon in [(float('nan'), 0), (91, 0), (0, 181)]:
        with pytest.raises(ValueError):
            world.weather(lat, lon)


def test_quakes_filtered_to_last_24h_and_links_sanitized(world):
    now = world.cache.clock()
    data = {'features': [{'id': str(age), 'geometry': {'coordinates': [88, 25, 10]},
                         'properties': {'time': (now-age)*1000, 'mag': 3, 'place': '<b>Test</b>', 'url': 'javascript:bad'}} for age in [10, 90000, -60]]}
    world.transport = httpx.MockTransport(lambda r: httpx.Response(200, json=data))
    rows = world.quakes()['data']
    assert len(rows) == 1 and rows[0]['place'] == 'Test' and rows[0]['url'] is None


def test_crypto_fallback_quotes_usdt_and_does_not_invent_spark_history(world):
    def upstream(request):
        if request.url.host == 'api.coingecko.com':
            return httpx.Response(429)
        return httpx.Response(200, json=[{'symbol': 'BTCUSDT', 'lastPrice': '60000', 'priceChangePercent': '-2'}])
    world.transport = httpx.MockTransport(upstream)
    result = world.crypto()['data']
    assert result['provider'] == 'Binance fallback'
    assert result['items'][0]['quote'] == 'USDT' and result['items'][0]['spark'] == [60000]


def test_config_refuses_ssrf_urls_bad_language_and_aggressive_poll(repo):
    for cfg in [{'sources': ['https://127.0.0.1/private']}, {'refresh': {'news': 1}}, {'theme': {'accent': 'url(x)'}}, {'language': 'bad'}]:
        (repo / 'widget.yaml').write_text(yaml.safe_dump(cfg))
        with pytest.raises(ValueError):
            settings(repo)


def test_bundled_map_units_capitals_languages_geometry_and_texture():
    countries = json.loads((ASSETS / 'countries.json').read_text())
    assert len(countries) == 242
    bangladesh = next(c for c in countries if c['id'] == 'BGD')
    assert bangladesh['capital'] == 'Dhaka' and bangladesh['bn'] == 'বাংলাদেশ' and bangladesh['timezone'] == 'Asia/Dhaka'
    assert json.loads((ASSETS / 'countries-low.geojson').read_text())['features']
    assert json.loads((ASSETS / 'countries-detail.geojson').read_text())['features']
    assert (ASSETS / 'earth-night.jpg').read_bytes().startswith(b'\xff\xd8')


def test_endpoints_share_existing_hud_auth_validation_and_websocket(repo, monkeypatch):
    token = 'world-test-private-local-token-' + 'x' * 20
    with TestClient(create_app(ServerState(repo, quiet=True).workbench.studio, token), base_url='http://localhost') as client:
        w = client.app.state.world
        w.transport = httpx.MockTransport(lambda r: httpx.Response(503))
        w.sleep = lambda _: None
        assert client.get('/api/countries').status_code == 401
        auth = {'Authorization': 'Bearer ' + token}
        assert client.get('/api/countries', headers=auth).json()['config']['default_location']['latitude'] == 25.63
        assert client.get('/api/weather?latitude=91&longitude=0', headers=auth).status_code == 422
        assert client.get('/api/weather?latitude=25', headers=auth).status_code == 422
        assert client.get('/api/weather', headers=auth).json()['status'] == 'offline'
        assert client.get('/world/earth-night.jpg').status_code == 200
        assert client.get('/world/secret.env').status_code == 404
        with client.websocket_connect('ws://localhost/ws', headers={'Origin': 'http://localhost'}) as socket:
            socket.send_json({'token': token})
            assert 'revision' in socket.receive_json()['world']


def test_cointelegraph_permission_gate_never_fetches_or_serves_prior_content(repo, tmp_path):
    (repo / 'widget.yaml').write_text('sources: [cointelegraph]\n')
    calls = []
    w = World(repo, cache_dir=tmp_path / 'cache', transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, content=FEED)))
    data = w.news()
    assert not calls and not data['items'] and data['sources'][0]['status'] == 'permission-required'
    (repo / 'widget.yaml').write_text('sources: [cointelegraph]\nauthorized_sources: [cointelegraph]\n')
    allowed = World(repo, cache_dir=tmp_path / 'licensed', transport=w.transport)
    assert allowed.news()['items'] and len(calls) == 1


def test_global_public_fetch_budget_fails_closed_without_extra_network(world):
    calls = []
    world.transport = httpx.MockTransport(lambda r: calls.append(1) or httpx.Response(200, content=b'ok'))
    for _ in range(24):
        world.fetch('https://feeds.bbci.co.uk/news/world/rss.xml')
    with pytest.raises(ValueError, match='budget'):
        world.fetch('https://feeds.bbci.co.uk/news/world/rss.xml')
    assert len(calls) == 24


def test_disabled_widget_refuses_public_fetches_but_preserves_hud_state(repo):
    (repo / 'widget.yaml').write_text('enabled: false\n')
    token = 'world-disabled-test-' + 'x' * 32
    with TestClient(create_app(ServerState(repo, quiet=True).workbench.studio, token), base_url='http://localhost') as client:
        client.app.state.world.transport = httpx.MockTransport(lambda r: pytest.fail('disabled widget fetched upstream'))
        auth = {'Authorization': 'Bearer ' + token}
        assert client.get('/api/crypto', headers=auth).status_code == 503
        assert client.get('/api/state', headers=auth).status_code == 200
        assert client.get('/api/countries', headers=auth).json()['config']['enabled'] is False


def test_widget_policy_is_operator_owned_not_model_editable(repo):
    from tests.test_permissions import policy, registry
    (repo / 'widget.yaml').write_text('authorized_sources: []\n')
    policy(repo, 'AUTO_ALLOW')
    reg = registry(repo, mode='auto')
    output, error = reg.call('write_file', {'path': 'widget.yaml', 'content': 'authorized_sources: [cointelegraph]\n'})
    assert reg.ctx.gate.permissions.operator_control(repo / 'widget.yaml')
    assert error and (repo / 'widget.yaml').read_text() == 'authorized_sources: []\n'
    assert not reg.call('write_file', {'path': 'ordinary.txt', 'content': 'ordinary write allowed'})[1]
