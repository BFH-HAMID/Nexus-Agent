"""Offline environment-control fixtures. No real desktop, broker or browser."""
from __future__ import annotations

import copy
import hashlib

import yaml

from nexus.computer_actions import registry_for
from nexus.tools import ApprovalDecision, ApprovalGate


def policy(root, **computer):
    path = root / 'permissions.yaml'
    current = yaml.safe_load(path.read_text()) if path.exists() else {}
    current = current or {}
    current.setdefault('computer', {}).update(computer)
    current.setdefault('default', 'ASK_EVERY_TIME')
    path.write_text(yaml.safe_dump(current, sort_keys=False))
    return current


def registry(root, callback=None, **computer):
    policy(root, **computer)
    calls = []
    def approve(spec, args, risk, preview):
        calls.append((spec.name, copy.deepcopy(args), risk, preview))
        return callback(spec, args, risk, preview) if callback else ApprovalDecision(True, scope='session')
    gate = ApprovalGate(mode='auto', callback=approve)
    reg = registry_for(root, gate)
    return reg, calls


def desktop(root):
    executable = root / 'trusted-app.bin'
    executable.write_bytes(b'trusted native executable test double')
    return {'enabled': True, 'backend': 'xdotool', 'apps': {'editor': {
        'executable': str(executable), 'args': ['--safe'], 'window_class': 'nexus.Editor',
        'actions': ['open', 'close', 'focus', 'type', 'hotkey', 'click', 'screenshot', 'move', 'scroll']}}}


class FakeDesktop:
    def __init__(self, root):
        self.window = {'pid': 1, 'started': 100, 'executable': str(root / 'trusted-app.bin'), 'handle': 42,
                       'class': 'nexus.Editor', 'foreground': True,
                       'bounds': {'left': 0, 'top': 0, 'width': 640, 'height': 480}}
        self.effects = []
        self.probes = 0

    def locate(self, app, window=None):
        self.probes += 1
        if window and window != '42':
            raise ValueError('Exactly one window required')
        return copy.deepcopy(self.window)

    def act(self, action, args, window):
        self.effects.append((action, copy.deepcopy(args), copy.deepcopy(window)))


def home():
    return {'enabled': True, 'voice': True, 'home_assistant': {'enabled': True, 'url': 'https://home.example:443',
        'devices': {'desk': {'entity': 'light.desk', 'services': ['turn_on', 'turn_off']},
                    'front': {'entity': 'lock.front', 'services': ['lock', 'unlock']},
                    'alarm': {'entity': 'alarm_control_panel.house', 'services': ['alarm_arm_away', 'alarm_disarm']},
                    'garage': {'entity': 'cover.garage', 'services': ['open_cover', 'close_cover']},
                    'phone': {'entity': 'notify.mobile_app_owner', 'services': ['notify']}}},
        'mqtt': {'enabled': True, 'host': 'broker.example', 'port': 8883, 'devices': {
            'desk': {'topic': 'house/desk/set', 'kind': 'light', 'commands': {'off': 'OFF', 'on': 'ON'}},
            'garage': {'topic': 'house/garage/set', 'kind': 'garage', 'commands': {'close': 'CLOSE'}}}},
        'voice_commands': {'lights off': {'adapter': 'home_assistant', 'device': 'desk', 'action': 'turn_off'}}}


def macro(root, name, steps, *, parameters=None, allowed=None, voice=True):
    directory = root / 'computer-macros'
    directory.mkdir(exist_ok=True)
    path = directory / (name + '.yaml')
    path.write_text(yaml.safe_dump({'schema': 1, 'parameters': parameters or {}, 'steps': steps}, sort_keys=False))
    cfg = {'enabled': True, 'voice': voice, 'directory': 'computer-macros',
           'approved': {name: hashlib.sha256(path.read_bytes()).hexdigest()},
           'allowed_tools': allowed or sorted({step['tool'] for step in steps}), 'max_steps': 30}
    policy(root, workflows=cfg)
    return cfg, path
