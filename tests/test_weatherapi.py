"""WeatherAPI keys stay server-side; provider payloads/feeds are explicit fixtures."""
import json
import logging

import httpx
import pytest

from nexus.gui_world_settings import save, view
from nexus.hud.world.core import World, parse_feed
from nexus.hud.world.sources import SOURCES
from nexus.hud.world.weatherapi import code
from nexus.server import ServerState

KEY = 'weather-fixture-only-' + 'x' * 32


def payload():
    return {'location': {'tz_id': 'Asia/Dhaka'}, 'current': {'temp_c': 30, 'feelslike_c': 33, 'humidity': 75,
        'wind_kph': 12, 'precip_mm': 1, 'condition': {'code': 1273}, 'is_day': 1}, 'forecast': {'forecastday': [
        {'date': '2026-10-30', 'day': {'maxtemp_c': 34, 'mintemp_c': 24, 'uv': 5, 'totalprecip_mm': 8, 'condition': {'code': 1003}},
         'astro': {'sunrise': '06:05 AM', 'sunset': '05:22 PM'}, 'hour': [{'time_epoch': 1793318400, 'temp_c': 29, 'chance_of_rain': 70, 'precip_mm': 1, 'condition': {'code': 1183}}]}]}}


def test_weatherapi_actual_client_normalizes_and_never_caches_or_logs_key(repo, tmp_path, caplog):
    calls = []
    world = World(repo, cache_dir=tmp_path / 'cache', weather_key=lambda: KEY,
                  transport=httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(200, json=payload())))
    with caplog.at_level(logging.INFO, logger='httpx'):
        result = world.weather(23.81, 90.41)
    assert calls[0].url.host == 'api.weatherapi.com'
    assert calls[0].url.params['key'] == KEY and calls[0].url.params['days'] == '3'
    assert result['data']['provider'] == 'WeatherAPI.com'
    assert result['data']['current']['temperature_2m'] == 30
    assert result['data']['current']['weather_code'] == 95
    assert result['data']['forecast_days'] == 1  # Never invent plan-restricted days.
    assert result['data']['daily']['sunrise'][0] < result['data']['daily']['sunset'][0]
    assert world.weather(23.81, 90.41)['status'] == 'cached' and len(calls) == 1
    assert KEY not in json.dumps(result) and KEY not in caplog.text
    assert KEY not in (tmp_path / 'cache/public-cache.json').read_text()


def test_missing_key_does_not_call_any_provider(repo, tmp_path):
    world = World(repo, cache_dir=tmp_path / 'cache', weather_key=lambda: '',
                  transport=httpx.MockTransport(lambda r: pytest.fail('No provider request without key')))
    assert world.weather()['status'] == 'configuration-required'
    assert world.cities('Dhaka')['status'] == 'configuration-required'


def test_weatherapi_city_lookup_and_provider_errors_are_secret_safe(repo, tmp_path):
    world = World(repo, cache_dir=tmp_path / 'cache', weather_key=lambda: KEY,
                  transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[{'name': 'Dhaka', 'country': 'Bangladesh', 'lat': 23.81, 'lon': 90.41}])))
    assert world.cities('Dhaka')['data'][0]['name'] == 'Dhaka, Bangladesh'
    world.transport = httpx.MockTransport(lambda r: httpx.Response(403, json={'error': {'message': KEY}}))
    result = world.weather()
    assert result['status'] == 'offline' and KEY not in json.dumps(result)


def test_dashboard_world_settings_persist_without_key_and_reject_stale_writes(repo, monkeypatch):
    monkeypatch.delenv('WEATHERAPI_KEY', raising=False)
    state = ServerState(repo, quiet=True)
    old = view(state)
    data = {'confirmation': 'SAVE NEWS AND WEATHER', 'revision': old['revision'], 'key': KEY,
            'settings': {'weather_provider': 'weatherapi', 'sources': ['prothom-alo', 'daily-star', 'dhaka-tribune'], 'language': 'bn'}}
    save(state, data)
    assert KEY not in (repo / 'widget.yaml').read_text()
    after = view(state)
    assert after['weather_key_present'] and after['weather_key_source'] == 'session'
    assert KEY not in json.dumps(after)
    assert state.gui.world().weather_key() == KEY
    with pytest.raises(ValueError, match='changed'):
        save(state, data)
    with pytest.raises(ValueError, match='Review'):
        save(state, {**data, 'confirmation': 'wrong'})
    before = (repo / 'widget.yaml').read_bytes()
    with pytest.raises(ValueError):
        save(state, {**data, 'revision': after['revision'], 'settings': {'sources': ['https://localhost/private']}})
    assert (repo / 'widget.yaml').read_bytes() == before


def test_bangladesh_catalog_and_bangla_rss_are_real_parser_inputs(repo, tmp_path):
    world = World(repo, cache_dir=tmp_path / 'cache')
    for source in ['prothom-alo', 'daily-star', 'bdnews24', 'banglanews24', 'dhaka-tribune', 'jugantor', 'jagonews24']:
        assert SOURCES[source][0] == 'Bangladesh' and SOURCES[source][2].startswith('https://')
    xml = '<rss version="2.0"><channel><item><title>ঢাকা ও চট্টগ্রামে বৃষ্টি</title><link>https://example.org/news</link><description><![CDATA[<script>bad()</script><p>খবরের সারাংশ</p>]]></description></item></channel></rss>'.encode()
    row = parse_feed(xml, 'prothom-alo', world.matcher)[0]
    assert row['category'] == 'Bangladesh' and 'BGD' in row['countries']
    assert row['summary'] == 'খবরের সারাংশ' and row['published'] is None
    assert code({'code': 999999}) is None


def test_native_vault_failure_never_falls_back_to_plaintext(repo, monkeypatch):
    import nexus.gui_world_settings as module
    state = ServerState(repo, quiet=True)
    before = view(state)
    def fail(*args):
        raise ValueError(KEY)
    monkeypatch.setattr(module, 'remember', fail)
    with pytest.raises(ValueError) as error:
        save(state, {'revision': before['revision'], 'confirmation': 'SAVE NEWS AND WEATHER',
                     'settings': {'weather_provider': 'weatherapi'}, 'key': KEY, 'remember_key': True})
    assert KEY not in str(error.value)
    assert not (repo / 'widget.yaml').exists()
    assert not getattr(state, 'weatherapi_key', '')


def test_world_settings_refuse_symlinks_before_parsing(repo, tmp_path):
    private = tmp_path / 'private.yaml'
    private.write_text('private: secret-not-to-read')
    (repo / 'widget.yaml').symlink_to(private)
    state = ServerState(repo, quiet=True)
    with pytest.raises(ValueError, match='Unsafe'):
        view(state)


def test_world_settings_do_not_echo_malformed_private_yaml(repo):
    (repo / 'widget.yaml').write_text('sources: [private-key-do-not-echo\n', encoding='utf-8')
    state = ServerState(repo, quiet=True)
    with pytest.raises(ValueError) as error:
        view(state)
    assert 'private-key-do-not-echo' not in str(error.value)
    assert 'inspect it locally' in str(error.value)
