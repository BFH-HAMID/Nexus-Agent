"""WeatherAPI adapter: HTTPS/server-only credentials, normalized existing widget data."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo


class RedactWeatherKey(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if 'weatherapi.com' in message or '?key=' in message or '&key=' in message:
            record.msg = re.sub(r'([?&]key=)[^&\s\"\']+', r'\1[REDACTED]', message, flags=re.I)
            record.args = ()
        return True


for name in ('httpx', 'httpcore.http11', 'httpcore.http2', 'httpcore.connection_pool'):
    logger = logging.getLogger(name)
    if not any(isinstance(f, RedactWeatherKey) for f in logger.filters):
        logger.addFilter(RedactWeatherKey())


FORECAST = 'https://api.weatherapi.com/v1/forecast.json'
SEARCH = 'https://api.weatherapi.com/v1/search.json'
SERVICE = 'nexus-weatherapi'


def credential(root):
    value = os.environ.get('WEATHERAPI_KEY', '').strip()
    if value:
        return value
    try:
        from ...hud.auth import launch_token
        return launch_token(root, token='', service=SERVICE)
    except (ImportError, ValueError):
        return ''


def remember(root, value):
    from ...hud.auth import launch_token
    return launch_token(root, True, token=value, service=SERVICE)


def code(condition):
    # WeatherAPI condition IDs -> WMO-like display categories used by the widget.
    n = condition.get('code')
    if n == 1000:
        return 0
    if n in {1003, 1006, 1009}:
        return 3
    if n in {1030, 1135, 1147}:
        return 45
    if n in {1087, 1273, 1276, 1279, 1282}:
        return 95
    if n in {1066, 1069, 1072, 1114, 1117, 1168, 1171, 1204, 1207, 1210, 1213, 1216, 1219, 1222, 1225, 1237, 1249, 1252, 1255, 1258, 1261, 1264}:
        return 71
    return 61 if n in {1063, 1150, 1153, 1180, 1183, 1186, 1189, 1192, 1195, 1198, 1201, 1240, 1243, 1246} else None


def normalize(data):
    current, location = data['current'], data['location']
    days = data['forecast']['forecastday'][:7]
    if not days or not isinstance(current, dict):
        raise ValueError('WeatherAPI forecast missing')
    zone = ZoneInfo(location['tz_id'])
    hourly = {k: [] for k in ('time', 'temperature_2m', 'precipitation_probability', 'precipitation', 'weather_code')}
    daily = {k: [] for k in ('time', 'temperature_2m_max', 'temperature_2m_min', 'weather_code', 'sunrise', 'sunset', 'uv_index_max', 'precipitation_sum')}
    for row in days:
        day = row['day']
        daily['time'].append(datetime.strptime(row['date'], '%Y-%m-%d').replace(tzinfo=zone).timestamp())
        for out, key in [('temperature_2m_max', 'maxtemp_c'), ('temperature_2m_min', 'mintemp_c'), ('uv_index_max', 'uv'), ('precipitation_sum', 'totalprecip_mm')]:
            daily[out].append(day.get(key))
        daily['weather_code'].append(code(day.get('condition', {})))
        for field in ('sunrise', 'sunset'):
            try:
                stamp = datetime.strptime(row['date'] + ' ' + row['astro'][field], '%Y-%m-%d %I:%M %p').replace(tzinfo=zone).timestamp()
            except (KeyError, ValueError):
                stamp = None
            daily[field].append(stamp)
        for hour in row.get('hour', [])[:24]:
            for out, key in [('time', 'time_epoch'), ('temperature_2m', 'temp_c'), ('precipitation_probability', 'chance_of_rain'), ('precipitation', 'precip_mm')]:
                hourly[out].append(hour.get(key))
            hourly['weather_code'].append(code(hour.get('condition', {})))
    return {'provider': 'WeatherAPI.com', 'provider_url': 'https://www.weatherapi.com/', 'timezone': location['tz_id'],
            'forecast_days': len(days), 'hourly': hourly, 'daily': daily,
            'current': {'temperature_2m': current.get('temp_c'), 'apparent_temperature': current.get('feelslike_c'),
                        'relative_humidity_2m': current.get('humidity'), 'wind_speed_10m': current.get('wind_kph'),
                        'precipitation': current.get('precip_mm'), 'weather_code': code(current.get('condition', {})),
                        'is_day': current.get('is_day')}}
