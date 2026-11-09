"""Documented Home Assistant REST and TLS MQTT only; no device discovery/hooks."""
from __future__ import annotations

import json
import os
import time
from urllib.parse import quote

from .computer_policy import mapping, text


def secret(name):
    from .assistant.personal_data import NativeVault
    return NativeVault().get(name)


class HomeAssistantREST:
    def __init__(self, cfg):
        self.cfg = cfg

    def request(self, method, path, body, guard):
        import httpx
        token = secret('home_assistant')
        if not token:
            raise ValueError('Set HOME_ASSISTANT_TOKEN in the environment/.env or native keyring, never YAML')
        guard()
        deadline = time.monotonic() + 15
        try:
            with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
                with client.stream(method, self.cfg['url'] + path, json=body,
                                   headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
                                            'Accept-Encoding': 'identity'}) as response:
                    if not 200 <= response.status_code < 300 or response.headers.get('content-encoding', 'identity') != 'identity':
                        raise ValueError('Home Assistant refused the request; no redirect or retry')
                    data = bytearray()
                    for chunk in response.iter_bytes(8192):
                        guard()
                        data.extend(chunk)
                        if time.monotonic() > deadline or len(data) > 200000:
                            raise ValueError('Home Assistant response budget exceeded; outcome may be uncertain')
                    result = json.loads(data) if data else {}
                    # A server must not reflect the credential into visible state,
                    # including native-keyring values not present in os.environ.
                    if isinstance(result, dict) and 'state' in result:
                        from .redaction import mask
                        if not isinstance(result['state'], str):
                            raise ValueError('Home Assistant state must be bounded plain text')
                        result['state'] = mask(result['state'], [token])
                    return result
        except (httpx.HTTPError, UnicodeError, json.JSONDecodeError):
            raise ValueError('Home Assistant connection/response failed; inspect device state before retrying') from None

    def execute(self, device, action, message, guard):
        entity = device['entity']
        domain, name = entity.split('.', 1)
        if action == 'state':
            row = self.request('GET', '/api/states/' + quote(entity, safe='.'), None, guard)
            if not isinstance(row, dict) or row.get('entity_id') != entity:
                raise ValueError('Home Assistant returned a different/invalid entity')
            # Attributes may contain tokens, camera URLs or other devices. Do not
            # expose them, and never fetch /api/states globally then filter.
            return {'entity': entity, 'state': str(row.get('state', ''))[:100], 'untrusted': True}
        path = f'/api/services/{domain}/{name if domain == "notify" else action}'
        body = {'message': message} if domain == 'notify' else {'entity_id': entity}
        self.request('POST', path, body, guard)
        return {'entity': entity, 'action': action, 'requested': True,
                'note': 'Service accepted, not proof of physical state/delivery. No automatic retry.'}


class TLSMQTT:
    def __init__(self, cfg):
        self.cfg = cfg

    def execute(self, device, action, message, guard):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise ValueError('Install MQTT support: pip install -e ".[smart-home]"') from None
        password = secret('mqtt')
        username = os.environ.get('MQTT_USERNAME', '')
        if not username or not password:
            raise ValueError('MQTT_USERNAME and MQTT_PASSWORD/native keyring are required')
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
        client.username_pw_set(username, password)
        client.tls_set()  # native trust store + hostname verification; no insecure toggle
        client.connect_timeout = 5
        accepted = []
        client.on_connect = lambda _c, _u, _f, reason, _p: accepted.append(reason == 0)
        deadline = time.monotonic() + 15
        try:
            guard()
            client.connect(self.cfg['host'], self.cfg['port'], keepalive=10)
            while not accepted:
                guard()
                if time.monotonic() >= deadline or client.loop(timeout=.1) != mqtt.MQTT_ERR_SUCCESS:
                    raise ValueError('MQTT connection failed; no retries')
            if not accepted[0]:
                raise ValueError('MQTT connection refused')
            guard()
            info = client.publish(device['topic'], device['commands'][action], qos=1, retain=False)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise ValueError('MQTT publish refused')
            while not info.is_published():
                guard()
                if time.monotonic() >= deadline or client.loop(timeout=.1) != mqtt.MQTT_ERR_SUCCESS:
                    raise ValueError('MQTT acknowledgement unavailable; inspect device before retry')
            return {'topic': device['topic'], 'action': action, 'acknowledged': True, 'retained': False,
                    'note': 'Broker acknowledgement only. QoS 1 can deliver duplicates; no physical-state guarantee.'}
        except (OSError, RuntimeError):
            raise ValueError('MQTT connection/publish failed; outcome may be uncertain, no automatic retry') from None
        finally:
            try:
                client.disconnect()
            except Exception:
                pass


def backend(adapter, cfg):
    return HomeAssistantREST(cfg) if adapter == 'home_assistant' else TLSMQTT(cfg)


def plan(ctx, args, computer):
    mapping(args, {'adapter', 'device', 'action', 'message'}, 'smart-home action')
    cfg = computer['smart_home']
    adapter = args.get('adapter')
    if not cfg['enabled'] or adapter not in ('home_assistant', 'mqtt') or not cfg[adapter]['enabled']:
        raise ValueError('Smart-home adapter disabled in permissions.yaml')
    device = cfg[adapter]['devices'].get(args.get('device'))
    if not device:
        raise ValueError('Device is outside the exact operator allowlist')
    action = args.get('action')
    allowed = device.get('services', device.get('commands', {}))
    if action not in allowed and not (adapter == 'home_assistant' and action == 'state' and not device['entity'].startswith('notify.')):
        raise ValueError('Device action is outside the exact operator allowlist')
    phone = adapter == 'home_assistant' and device['entity'].startswith('notify.')
    if phone:
        message = text(args.get('message'), 500)
        if 'command_' in message.casefold() or message.casefold().strip() in {'clear_notification', 'remove_channel'}:
            raise ValueError('Only ordinary companion-app notifications are allowed; no remote phone commands')
    elif 'message' in args:
        raise ValueError('Only companion notifications accept message data; device payloads are operator-fixed')
    hazardous = adapter == 'home_assistant' and device['entity'].split('.')[0] in {'lock', 'alarm_control_panel', 'cover'}
    hazardous = hazardous or adapter == 'mqtt' and device['kind'] in {'lock', 'alarm', 'garage'}
    return {'adapter': adapter, 'device': device, 'endpoint': cfg[adapter].get('url', cfg[adapter].get('host')),
            'risk': 'DANGEROUS', 'security_device': bool(hazardous), 'confirmation': 'fresh for EVERY call, including locks/alarms/garage'}


def perform(ctx, args, review, guard, registry):
    cfg = ctx.gate.permissions.load()['computer']['smart_home'][args['adapter']]
    guard()
    result = backend(args['adapter'], cfg).execute(review['device'], args['action'], args.get('message'), guard)
    if args['action'] == 'state':
        from .untrusted import wrap
        ctx.mark_tainted('home-assistant-state')
        return wrap(json.dumps(result), source='home-assistant:' + args['device'], kind='device')[0]
    return result
