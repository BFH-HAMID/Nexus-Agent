"""YAML, native input, approval, capture and compatibility boundaries (all fake IO)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
import yaml

from nexus.computer import settings
from nexus.computer_actions import registry_for
from nexus.computer_control import EmergencyStop
from nexus.desktop_control import WindowsDesktop
from nexus.tools import ApprovalDecision, ApprovalGate
from nexus.types import Risk

from .computer_helpers import FakeDesktop, desktop, policy, registry


def test_environment_shipped_switches_are_all_off():
    cfg = settings(yaml.safe_load((Path(__file__).parents[1] / 'permissions.yaml').read_text())['computer'])
    assert all(not cfg[key]['enabled'] for key in ('desktop', 'files', 'workflows', 'browser_controls', 'smart_home'))
    assert not cfg['workflows']['voice'] and not cfg['smart_home']['voice']
    assert not cfg['browser_controls']['forms'] and not cfg['browser_controls']['downloads']
    assert not cfg['smart_home']['home_assistant']['enabled'] and not cfg['smart_home']['mqtt']['enabled']


@pytest.mark.parametrize('value', [
    {'desktop': True}, {'files': {'enabled': 'yes'}}, {'desktop': {'backend': 'ydotool'}},
    {'files': {'roots': ['../data']}}, {'files': {'roots': ['.nexus']}}, {'files': {'max_bytes': True}},
    {'workflows': {'directory': '.'}}, {'workflows': {'approved': {'run': 'not-a-digest'}}},
    {'workflows': {'allowed_tools': ['workflow_run']}}, {'browser_controls': {'downloads': 'on'}},
    {'browser_controls': {'download_dir': '/tmp'}}, {'browser_controls': {'search_url': 'https://x:443/{other}'}},
    {'browser_origins': ['https://EXAMPLE.com:443']}, {'browser_origins': ['https://example.com:0']},
    {'smart_home': {'home_assistant': {'enabled': True, 'url': 'http://localhost.evil:8123'}}},
    {'smart_home': {'mqtt': {'devices': {'light': {'topic': 'home/#', 'commands': {'off': 'off'}}}}}},
    {'smart_home': {'home_assistant': {'token': 'not-allowed'}}}, {'smart_home': {'mqtt': {'tls': False}}},
])
def test_malformed_scopes_fail_closed(value):
    with pytest.raises(ValueError):
        settings(value)


@pytest.fixture()
def controlled(repo, monkeypatch):
    cfg = desktop(repo)
    fake = FakeDesktop(repo)
    monkeypatch.setattr('nexus.desktop_control.backend', lambda *_: fake)
    reg, calls = registry(repo, desktop=cfg)
    return reg, calls, fake, cfg


def test_desktop_default_off_cannot_be_enabled_by_environment_or_legacy_review(repo, monkeypatch):
    from nexus.assistant.automation import DesktopController
    monkeypatch.setenv('NEXUS_DESKTOP_ENABLED', '1')
    reg = registry_for(repo, ApprovalGate(mode='auto', callback=lambda *_: ApprovalDecision(True)))
    assert not reg.has('desktop_action')
    with pytest.raises(ValueError):
        DesktopController(workspace=repo, review=lambda *_: True).execute('click', {'app': 'any', 'x': 1, 'y': 1})


@pytest.mark.parametrize('args', [
    {'action': 'focus', 'app': 'unknown'}, {'action': 'type', 'app': 'editor', 'text': 'hi\nenter'},
    {'action': 'click', 'app': 'editor', 'x': 640, 'y': 0}, {'action': 'click', 'app': 'editor', 'x': -1, 'y': 0},
    {'action': 'hotkey', 'app': 'editor', 'keys': ['ctrl', 'exec shell']},
    {'action': 'focus', 'app': 'editor', 'window': '999'},
    {'action': 'click', 'app': 'editor', 'label': 'Yes', 'x': 1, 'y': 1},
    {'action': 'focus', 'app': 'editor', '_review': {}},
])
def test_desktop_scopes_and_inputs_rejected_before_approval(controlled, args):
    reg, calls, fake, _ = controlled
    assert reg.call('desktop_action', args)[1]
    assert not calls and not fake.effects


@pytest.mark.parametrize('action,extra', [
    ('focus', {}), ('close', {}), ('type', {'text': 'literal +^{ENTER}'}),
    ('hotkey', {'keys': ['ctrl', 's']}), ('click', {'x': 12, 'y': 25}),
    ('click', {'label': 'Save'}), ('move', {'x': 1, 'y': 1}), ('scroll', {'clicks': -2}),
])
def test_native_requests_use_fresh_gate_every_time(controlled, action, extra):
    reg, calls, fake, _ = controlled
    reg.ctx.gate.always_allow.add('desktop_action')
    for _ in range(2):
        result, error = reg.call('desktop_action', {'action': action, 'app': 'editor', **extra})
        assert not error, result
    assert len(calls) == len(fake.effects) == 2
    assert all(c[2] is Risk.DANGEROUS for c in calls)
    assert 'window' in calls[0][1]['_review']['target']
    assert 'desktop_action' not in reg.ctx.gate.session_allowed


def test_open_fixed_argv_without_shell_or_ambient_credentials(controlled, monkeypatch, repo):
    reg, calls, _fake, _cfg = controlled
    captured = []
    monkeypatch.setenv('CLOUD_SECRET', 'opaque-not-for-app')
    monkeypatch.setattr('nexus.desktop_control.subprocess.Popen', lambda argv, **kw:
        captured.append((argv, kw)) or type('Child', (), {'pid': 123})())
    output, error = reg.call('desktop_action', {'app': 'editor', 'action': 'open'})
    assert not error, output
    assert captured[0][0] == [str(repo / 'trusted-app.bin'), '--safe']
    assert not captured[0][1].get('shell') and 'CLOUD_SECRET' not in captured[0][1]['env']
    assert len(calls) == 1


@pytest.mark.parametrize('kind', ['deny', 'plan', 'readonly', 'no-channel', 'cancel', 'emergency'])
def test_desktop_all_approval_denial_paths_have_no_effect(controlled, repo, kind):
    reg, calls, fake, _ = controlled
    if kind == 'deny':
        reg.ctx.gate.callback = lambda *_: ApprovalDecision(False)
    elif kind == 'plan':
        reg.ctx.gate.mode = 'plan'
    elif kind == 'readonly':
        value = yaml.safe_load((repo / 'permissions.yaml').read_text())
        value['default'] = 'READ_ONLY'
        (repo / 'permissions.yaml').write_text(yaml.safe_dump(value))
    elif kind == 'no-channel':
        reg.ctx.gate.callback = None
        reg.ctx.gate.session_allowed.add('desktop_action')
    elif kind == 'cancel':
        reg.ctx.cancel_event = threading.Event()
        reg.ctx.cancel_event.set()
    else:
        EmergencyStop().set(True)
    try:
        assert reg.call('desktop_action', {'app': 'editor', 'action': 'focus'})[1]
        assert not fake.effects
    finally:
        EmergencyStop().set(False)


@pytest.mark.parametrize('change', ['window', 'policy', 'arguments', 'executable', 'stop'])
def test_review_is_bound_to_live_identity_policy_and_arguments(controlled, repo, change):
    reg, _calls, fake, cfg = controlled
    def approve(_spec, args, *_):
        if change == 'window':
            fake.window['started'] += 1
        elif change == 'policy':
            policy(repo, desktop={**cfg, 'enabled': False})
        elif change == 'arguments':
            args['text'] = 'unreviewed replacement'
        elif change == 'executable':
            (repo / 'trusted-app.bin').write_text('different app')
        else:
            EmergencyStop().set(True)
        return ApprovalDecision(True)
    reg.ctx.gate.callback = approve
    try:
        assert reg.call('desktop_action', {'app': 'editor', 'action': 'type', 'text': 'reviewed value'})[1]
        assert not fake.effects
    finally:
        EmergencyStop().set(False)


def test_no_background_input_and_windows_text_is_literal(controlled):
    reg, _calls, fake, _cfg = controlled
    fake.window['foreground'] = False
    assert reg.call('desktop_action', {'app': 'editor', 'action': 'type', 'text': 'x'})[1]
    assert not fake.effects
    assert WindowsDesktop.literal('a+^{ENTER}%') == 'a{+}{^}{{}ENTER{}}{%}'


def test_desktop_audit_is_metadata_not_typed_text(controlled, repo):
    from nexus.audit import tail
    reg, _calls, _fake, _cfg = controlled
    raw = 'private ordinary prose not for the audit log'
    assert not reg.call('desktop_action', {'app': 'editor', 'action': 'type', 'text': raw})[1]
    rows = tail(repo, limit=10)
    assert {'started', 'completed'} <= {r['result'] for r in rows if r['what'] == 'computer.action'}
    assert raw not in json.dumps(rows)


def test_known_secret_refused_before_native_probe_or_approval(controlled, monkeypatch):
    reg, calls, fake, _cfg = controlled
    monkeypatch.setenv('OPENAI_API_KEY', 'opaque-secret-sentinel-812739')
    assert reg.call('desktop_action', {'app': 'editor', 'action': 'type', 'text': 'opaque-secret-sentinel-812739'})[1]
    assert not fake.probes and not fake.effects and not calls


def test_screenshot_reuses_context_privacy_and_local_hardoff(controlled, repo, monkeypatch):
    from nexus.vision.state import hard_off
    reg, calls, fake, _cfg = controlled
    (repo / 'context.yaml').write_text('enabled: true\nscreenshots: true\n')
    foreground = {**fake.window, 'title': 'Safe editor', 'app': 'editor'}
    monkeypatch.setattr('nexus.context.core.Context.desktop', lambda self: foreground)
    captured = []
    data = bytearray(b'fake bounded screenshot bytes, never upload')
    def capture(context, gate):
        from nexus.context.vision import consent
        consent(gate, 'context_capture', {'window': 'editor', 'destination': 'memory only'})
        captured.append(context.root)
        return data
    monkeypatch.setattr('nexus.context.vision.capture', capture)
    output, error = reg.call('desktop_action', {'app': 'editor', 'action': 'screenshot'})
    assert not error, output
    result = json.loads(output)
    assert (repo / result['path']).read_bytes().startswith(b'fake bounded')
    assert [c[0] for c in calls] == ['desktop_action', 'context_capture']
    assert set(data) == {0} and captured == [repo]
    hard_off(repo)
    assert reg.call('desktop_action', {'app': 'editor', 'action': 'screenshot'})[1]
    assert captured == [repo]


def test_capture_not_enabled_merely_by_desktop_opt_in(controlled, monkeypatch):
    reg, calls, _fake, _ = controlled
    monkeypatch.setattr('nexus.context.vision.capture', lambda *_: pytest.fail('must not capture'))
    assert reg.call('desktop_action', {'app': 'editor', 'action': 'screenshot'})[1]
    assert not calls


def test_x11_matches_both_executable_and_window_class_without_titles_as_authority(repo, monkeypatch):
    from nexus.desktop_control import X11Desktop
    cfg = desktop(repo)
    native = X11Desktop.__new__(X11Desktop)
    calls = []
    def command(name, *args):
        calls.append((name, args))
        return '42' if name == 'xdotool' else '0x0000002a 0 501 10 20 640 480 nexus.Editor host Untrusted window title'
    native.command = command
    monkeypatch.setattr('nexus.desktop_control.process', lambda pid: {'pid': pid, 'started': 10,
                                                                    'executable': str(repo / 'trusted-app.bin')})
    result = native.locate(cfg['apps']['editor'])
    assert result['foreground'] and result['handle'] == 42 and result['bounds']['left'] == 10
    assert 'title' not in result
    monkeypatch.setattr('nexus.desktop_control.process', lambda pid: {'pid': pid, 'started': 10, 'executable': '/different/app'})
    with pytest.raises(ValueError, match='Exactly one'):
        native.locate(cfg['apps']['editor'])


def test_x11_refuses_global_wayland_fallback_and_clear_optional_dependency_error(repo, monkeypatch):
    from nexus.desktop_control import WindowsDesktop, X11Desktop
    monkeypatch.setenv('DISPLAY', ':77')
    monkeypatch.setenv('WAYLAND_DISPLAY', 'wayland-0')
    with pytest.raises(ValueError, match='fallback is forbidden'):
        X11Desktop(registry_for(repo).ctx)
    import builtins
    original = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == 'pywinauto':
            raise ImportError('not installed')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', missing)
    with pytest.raises(ValueError, match='desktop'):
        WindowsDesktop(registry_for(repo).ctx)


def test_app_fixed_launch_inputs_cannot_be_edited_by_model_tools(repo):
    cfg = desktop(repo)
    (repo / 'launch-config.json').write_text('{}')
    cfg['apps']['editor']['args'] = ['--config=launch-config.json']
    reg, _calls = registry(repo, desktop=cfg)
    assert reg.call('write_file', {'path': 'launch-config.json', 'content': '{"execute":"untrusted"}'})[1]
    assert (repo / 'launch-config.json').read_text() == '{}'


def test_known_credentials_in_native_results_are_masked_before_live_events(controlled, monkeypatch):
    reg, _calls, _fake, _cfg = controlled
    sentinel = 'opaque-private-native-response-384926'
    monkeypatch.setenv('CUSTOM_API_KEY', sentinel)
    monkeypatch.setattr('nexus.desktop_control.perform', lambda *_: {'result': sentinel})
    events = []
    reg.ctx.emit = events.append
    output, error = reg.call('desktop_action', {'app': 'editor', 'action': 'focus'})
    assert not error and sentinel not in output and 'REDACTED' in output
    assert sentinel not in json.dumps([event.to_json() for event in events])
