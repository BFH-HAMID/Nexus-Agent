"""Strict, reloadable operator authority; no environment switch enables vision."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..assistant.common import AssistantError
from ..jarvis.sources import bounded


class VisionError(AssistantError):
    """Perception refused; inputs and provider error bodies are never echoed."""


DEFAULTS = {
    'schema': 1,
    'enabled': False,
    'vlm': {'enabled': False, 'provider': 'ollama', 'model': 'qwen2-vl:7b',
            'base_url': 'http://127.0.0.1:11434', 'allow_cloud': False,
            'timeout_seconds': 30, 'max_output_tokens': 1200},
    'screen': {'enabled': False, 'denylist': []},
    'files': {'enabled': False, 'hud': False, 'telegram': False, 'voice': False,
              'max_bytes': 10_000_000, 'max_pages': 8, 'max_pixels': 16_000_000},
    'camera': {'enabled': False, 'device': 0, 'weights': '', 'classes': ['person'],
               'confidence': 0.6, 'fps': 1.0, 'max_frames': 300, 'max_seconds': 300,
               'width': 640, 'height': 480, 'location': 'door', 'events': False,
               'cooldown_seconds': 60, 'store_frames': False,
               'max_storage_frames': 100, 'max_storage_bytes': 20_000_000},
    'faces': {'enabled': False, 'owner': '', 'tolerance': 0.5, 'max_people': 20},
}


def _number(value, low, high, *, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not low <= value <= high or not math.isfinite(value):
        raise VisionError('Invalid vision policy limit')


def _text(value, limit=120, *, empty=False):
    if not isinstance(value, str) or (not value and not empty) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise VisionError('Invalid bounded vision policy text')


def load_policy(workspace):
    path = Path(workspace).resolve() / 'vision.yaml'
    try:
        # lexists semantics: a dangling symlink is NOT an absent policy.
        raw = yaml.safe_load(bounded(path, 32_000)) if path.exists() or path.is_symlink() else {}
    except (yaml.YAMLError, UnicodeError, OSError, ValueError):
        raise VisionError('vision.yaml must be bounded regular operator-owned YAML') from None
    cfg = copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict) or set(raw) - set(cfg):
        raise VisionError('Unknown vision policy field')
    for key, value in raw.items():
        if isinstance(cfg[key], dict):
            if not isinstance(value, dict) or set(value) - set(cfg[key]):
                raise VisionError('Unknown vision policy section/field')
            cfg[key].update(value)
        else:
            cfg[key] = value
    if type(cfg['schema']) is not int or cfg['schema'] != 1:
        raise VisionError('Unsupported vision policy schema')
    for key, default in DEFAULTS.items():
        section = cfg[key]
        for field, val in (section.items() if isinstance(section, dict) else [(key, section)]):
            baseline = default[field] if isinstance(default, dict) else default
            if type(baseline) is bool and type(val) is not bool:
                raise VisionError('Vision switches require explicit booleans')
    vlm, files, camera, faces = (cfg[k] for k in ('vlm', 'files', 'camera', 'faces'))
    if vlm['provider'] not in ('ollama', 'gemini'):
        raise VisionError('Vision provider must be ollama or gemini; no fallback')
    if not isinstance(vlm['model'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}', vlm['model']):
        raise VisionError('Explicit model identifier required')
    if vlm['provider'] == 'ollama' and 'cloud' in vlm['model'].lower():
        raise VisionError('Cloud model aliases are forbidden for local vision')
    _text(vlm['base_url'], 200)
    try:
        url = urlsplit(vlm['base_url'])
        if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
                or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment
                or not 1 <= (url.port or 80) <= 65535):
            raise VisionError('Ollama requires a loopback HTTP origin without path/credentials/query')
    except ValueError:
        raise VisionError('Invalid local vision origin') from None
    _number(vlm['timeout_seconds'], 1, 60, integer=True)
    _number(vlm['max_output_tokens'], 64, 4096, integer=True)
    denylist = cfg['screen']['denylist']
    if not isinstance(denylist, list) or len(denylist) > 50:
        raise VisionError('Bounded screen denylist required')
    for item in denylist:
        _text(item, 100)
    for key, lo, hi in [('max_bytes', 1024, 10_000_000), ('max_pages', 1, 20), ('max_pixels', 1024, 16_000_000)]:
        _number(files[key], lo, hi, integer=True)
    for key, lo, hi in [('device', 0, 16), ('max_frames', 1, 3600), ('max_seconds', 1, 3600),
                        ('width', 64, 1920), ('height', 64, 1080), ('cooldown_seconds', 1, 86400),
                        ('max_storage_frames', 1, 1000), ('max_storage_bytes', 1024, 100_000_000)]:
        _number(camera[key], lo, hi, integer=True)
    _number(camera['fps'], 0.1, 10)
    _number(camera['confidence'], 0.1, 1)
    _text(camera['weights'], 500, empty=True)
    _text(camera['location'], 80)
    classes = camera['classes']
    if (not isinstance(classes, list) or not 1 <= len(classes) <= 80
            or any(not isinstance(c, str) or not re.fullmatch(r'[a-z][a-z0-9 -]{0,39}', c) for c in classes)
            or len(set(classes)) != len(classes)):
        raise VisionError('Explicit unique object classes required')
    _text(faces['owner'], 80, empty=True)
    _number(faces['tolerance'], 0.3, 0.6)
    _number(faces['max_people'], 1, 100, integer=True)
    # Reject literal credentials even in otherwise valid text fields.
    from ..context.core import clean
    if clean(json.dumps(cfg, ensure_ascii=False), workspace) != json.dumps(cfg, ensure_ascii=False):
        raise VisionError('Secrets do not belong in vision.yaml; use environment/keyring')
    return cfg


def digest(cfg):
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


def require(workspace, section, *, channel=None):
    from .state import check_running
    check_running(workspace)
    cfg = load_policy(workspace)
    if not cfg['enabled'] or not cfg[section]['enabled']:
        raise VisionError(f'Vision {section} is OFF; operator opt-in in vision.yaml is required')
    if channel not in (None, 'cli') and (channel not in ('hud', 'telegram', 'voice') or not cfg['files'][channel]):
        raise VisionError('This vision input channel is disabled in vision.yaml')
    return cfg


def recheck(workspace, cfg):
    from .state import check_running
    check_running(workspace)
    if load_policy(workspace) != cfg:
        raise VisionError('Vision policy changed; request fresh review')


def approve(workspace, gate, name, arguments, *, readonly=False):
    from ..permissions import Permissions
    from ..tools import ApprovalGate
    from ..types import Risk, ToolSpec
    check = gate if gate is not None else ApprovalGate()
    if check.permissions is None:
        check.permissions = Permissions(workspace)
    elif check.permissions.root != Path(workspace).resolve():
        raise VisionError('Approval gate workspace mismatch')
    spec = ToolSpec(name, 'Explicit vision operation; source pixels/text are untrusted, not tool authority.',
                    {'type': 'object'}, Risk.READ if readonly else Risk.DANGEROUS)
    decision = check.check(spec, arguments)
    if not decision.approved:
        raise VisionError('Fresh vision approval required or permissions.yaml refused the operation')
    from ..limits import LimitExceeded, check_and_record_action
    try:
        check_and_record_action(workspace, name)
    except LimitExceeded:
        raise VisionError('Configured action rate limit refuses vision operation') from None
    return check
