"""Exact device/service/topic policy and fake HA/MQTT transports; never a real send."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from nexus.assistant.automation import HomeAssistant, MqttHome
from nexus.assistant.personal_data import ExternalActions
from nexus.smart_home import TLSMQTT, HomeAssistantREST
from nexus.tools import ApprovalDecision
from nexus.types import Risk

from .computer_helpers import home, policy, registry


@pytest.fixture()
def configured(repo, monkeypatch):
    effects = []
    class Backend:
        def execute(self, device, action, message, guard):
            guard()
            effects.append((device, action, message))
            return {'requested': True, 'state': 'on'}
    monkeypatch.setattr('nexus.smart_home.backend', lambda *_: Backend())
    reg, calls = registry(repo, smart_home=home())
    return reg, calls, effects


def test_device_mutations_always_need_fresh_dangerous_confirmation(configured):
    reg, calls, effects = configured
    for device, action in [('desk', 'turn_off'), ('front', 'unlock'), ('alarm', 'alarm_disarm'), ('garage', 'open_cover')]:
        args = {'adapter': 'home_assistant', 'device': device, 'action': action}
        assert not reg.call('home_action', args)[1]
        assert not reg.call('home_action', args)[1]
    assert len(calls) == len(effects) == 8
    assert all(c[2] is Risk.DANGEROUS for c in calls)
    assert all(c[1]['_review']['target']['security_device'] for c in calls[2:])
    reg.ctx.gate.callback = None
    assert reg.call('home_action', {'adapter': 'home_assistant', 'device': 'front', 'action': 'unlock'})[1]
    assert len(effects) == 8


@pytest.mark.parametrize('args', [
    {'adapter': 'home_assistant', 'device': 'light.not-listed', 'action': 'turn_on'},
    {'adapter': 'home_assistant', 'device': 'desk', 'action': 'toggle'},
    {'adapter': 'mqtt', 'device': 'desk', 'action': 'off', 'topic': 'any/topic'},
    {'adapter': 'mqtt', 'device': 'desk', 'action': 'off', 'message': 'arbitrary payload'},
    {'adapter': 'mqtt', 'device': 'desk', 'action': 'DELETE'},
    {'adapter': 'home_assistant', 'device': 'desk', 'action': 'turn_off', 'entity_id': 'all'},
])
def test_no_raw_entities_service_paths_topics_or_dynamic_payloads(configured, args):
    reg, calls, effects = configured
    assert reg.call('home_action', args)[1]
    assert not calls and not effects


def test_companion_phone_notifications_are_normal_messages_not_device_hooks(configured):
    reg, calls, effects = configured
    args = {'adapter': 'home_assistant', 'device': 'phone', 'action': 'notify', 'message': 'Your reviewed job finished.'}
    assert not reg.call('home_action', args)[1]
    assert len(calls) == len(effects) == 1
    for message in ('command_webview', 'command_launch_app', 'command_broadcast_intent', 'clear_notification'):
        assert reg.call('home_action', {**args, 'message': message})[1]
    assert len(effects) == 1


def test_disabled_never_resolves_credentials_or_touches_network(repo, monkeypatch):
    monkeypatch.setattr('nexus.smart_home.secret', lambda *_: pytest.fail('no credential lookup while disabled'))
    monkeypatch.setattr('httpx.Client', lambda *_a, **_kw: pytest.fail('no network'))
    reg, calls = registry(repo)
    assert reg.call('home_action', {'adapter': 'home_assistant', 'device': 'desk', 'action': 'turn_off'})[1]
    assert not calls


def test_disable_during_approval_blocks_physical_effect(configured, repo):
    reg, _calls, effects = configured
    def approve(*_):
        policy(repo, smart_home={**home(), 'enabled': False})
        return ApprovalDecision(True)
    reg.ctx.gate.callback = approve
    assert reg.call('home_action', {'adapter': 'mqtt', 'device': 'garage', 'action': 'close'})[1]
    assert not effects


@pytest.mark.parametrize('reflected', [False, True])
def test_home_assistant_uses_documented_single_entity_rest_and_no_redirects(monkeypatch, reflected):
    seen = []
    sentinel = 'opaque-home-api-test-sentinel'
    monkeypatch.setattr('nexus.smart_home.secret', lambda name: sentinel)
    class Response:
        status_code, headers = 200, {}
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def iter_bytes(self, size):
            yield json.dumps({'entity_id': 'light.desk', 'state': sentinel if reflected else 'on', 'attributes': {'secret': sentinel}}).encode()
    class Client:
        def __init__(self, **kw):
            seen.append(kw)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def stream(self, *args, **kw):
            seen.append((args, kw))
            return Response()
    monkeypatch.setattr('httpx.Client', Client)
    backend = HomeAssistantREST(home()['home_assistant'])
    result = backend.execute({'entity': 'light.desk'}, 'state', None, lambda: None)
    assert result == {'entity': 'light.desk', 'state': '[REDACTED_SECRET]' if reflected else 'on', 'untrusted': True}
    assert seen[1][0] == ('GET', 'https://home.example:443/api/states/light.desk')
    assert not seen[0]['trust_env'] and not seen[0]['follow_redirects']
    backend.execute({'entity': 'lock.front'}, 'unlock', None, lambda: None)
    assert seen[3][0][1].endswith('/api/services/lock/unlock') and seen[3][1]['json'] == {'entity_id': 'lock.front'}
    backend.execute({'entity': 'notify.mobile_app_owner'}, 'notify', 'hello', lambda: None)
    assert seen[5][0][1].endswith('/api/services/notify/mobile_app_owner') and seen[5][1]['json'] == {'message': 'hello'}
    assert sentinel not in json.dumps(result)


def test_native_http_errors_do_not_leak_tokens(monkeypatch):
    import httpx
    sentinel = 'opaque-secret-in-native-error'
    monkeypatch.setattr('nexus.smart_home.secret', lambda _: sentinel)
    monkeypatch.setattr('httpx.Client', lambda **_: (_ for _ in ()).throw(httpx.ConnectError(sentinel)))
    with pytest.raises(ValueError) as failure:
        HomeAssistantREST(home()['home_assistant']).execute({'entity': 'light.desk'}, 'turn_off', None, lambda: None)
    assert sentinel not in str(failure.value)


def mqtt_fake(monkeypatch, *, acknowledge=True):
    calls = []
    class Client:
        def __init__(self, *_args, **kwargs):
            calls.append(('client', kwargs))
        def username_pw_set(self, *args):
            calls.append(('auth', len(args)))
        def tls_set(self):
            calls.append(('tls', True))
        def connect(self, *args, **kwargs):
            calls.append(('connect', args, kwargs))
        def loop(self, **_):
            self.on_connect(self, None, None, 0, None)
            return 0
        def publish(self, *args, **kwargs):
            calls.append(('publish', args, kwargs))
            return SimpleNamespace(rc=0, is_published=lambda: acknowledge)
        def disconnect(self):
            calls.append(('disconnect',))
    module = SimpleNamespace(Client=Client, CallbackAPIVersion=SimpleNamespace(VERSION2=2), MQTT_ERR_SUCCESS=0)
    package = SimpleNamespace(client=module)
    monkeypatch.setitem(sys.modules, 'paho', SimpleNamespace(mqtt=package))
    monkeypatch.setitem(sys.modules, 'paho.mqtt', package)
    monkeypatch.setitem(sys.modules, 'paho.mqtt.client', module)
    monkeypatch.setattr('nexus.smart_home.secret', lambda _: 'opaque-test-password')
    monkeypatch.setenv('MQTT_USERNAME', 'nexus-test')
    return calls


def test_tls_mqtt_exact_payload_no_retention_or_auto_reconnect(monkeypatch):
    calls = mqtt_fake(monkeypatch)
    cfg = home()['mqtt']
    result = TLSMQTT(cfg).execute(cfg['devices']['garage'], 'close', None, lambda: None)
    assert result['acknowledged'] and result['retained'] is False
    assert ('tls', True) in calls
    assert ('client', {'reconnect_on_failure': False}) in calls
    assert ('publish', ('house/garage/set', 'CLOSE'), {'qos': 1, 'retain': False}) in calls
    assert calls[-1] == ('disconnect',)


def test_mqtt_cancellation_prevents_publish_and_disconnects(monkeypatch):
    calls = mqtt_fake(monkeypatch)
    cfg = home()['mqtt']
    checked = []
    def guard():
        checked.append(True)
        if len(checked) >= 3:
            raise ValueError('cancelled')
    with pytest.raises(ValueError, match='cancelled'):
        TLSMQTT(cfg).execute(cfg['devices']['desk'], 'off', None, guard)
    assert not any(c[0] == 'publish' for c in calls) and calls[-1][0] == 'disconnect'


def test_legacy_home_apis_cannot_expand_yaml_scopes(configured, repo):
    reg, calls, effects = configured
    with pytest.raises(ValueError):
        HomeAssistant(None, base_url='https://evil:443', workspace=repo, gate=reg.ctx.gate)
    with pytest.raises(ValueError):
        MqttHome(host='broker.example', topic_prefixes=['house/'], workspace=repo, gate=reg.ctx.gate)
    home_api = HomeAssistant(None, base_url='https://home.example:443', entities=['light.desk'], workspace=repo, gate=reg.ctx.gate)
    actions = ExternalActions()
    payload = {'domain': 'light', 'service': 'turn_off', 'entity_id': 'light.desk'}
    approval = actions.preview('HOME ASSISTANT', payload)
    assert home_api.call(actions, approval, approval['confirmation'], payload)['requested']
    assert len(effects) == len(calls) == 1
    with pytest.raises(ValueError):
        home_api.call(actions, approval, approval['confirmation'], payload)
