"""Offline fixtures shared by perception tests; never load native detectors/models."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import httpx
import numpy as np
import yaml
from PIL import Image, ImageDraw

from nexus.tools import ApprovalDecision, ApprovalGate
from nexus.vision.policy import load_policy
from nexus.vision.vlm import VLMAdapter

HTTP_CLIENT = httpx.Client


def enable(root, **sections):
    cfg = load_policy(root)
    cfg['enabled'] = True
    for name, values in sections.items():
        if isinstance(values, dict):
            cfg[name].update(values)
        else:
            cfg[name] = values
    (root / 'vision.yaml').write_text(yaml.safe_dump(cfg))
    return cfg


def gate(reviews=None, callback=None, **kwargs):
    def approved(spec, args, risk, preview):
        if reviews is not None:
            reviews.append((spec.name, args))
        return callback(spec, args) if callback else ApprovalDecision(True, scope='session')
    return ApprovalGate(callback=approved, **kwargs)


def image_bytes():
    image = Image.new('RGB', (200, 100), 'white')
    ImageDraw.Draw(image).rectangle((40, 20, 170, 80), fill='blue')
    stream = io.BytesIO()
    image.save(stream, 'PNG')
    return stream.getvalue()


def fake_vlm(monkeypatch, *, answer='The blue bar is visible.', refs=None, hook=None, status=200):
    calls = []
    factories = []
    def respond(request):
        calls.append(request)
        if hook:
            value = hook(request)
            if value is not None:
                return value
        return httpx.Response(status, json={'choices': [{'message': {'role': 'assistant', 'content': json.dumps({
            'answer': answer, 'references': refs if refs is not None else [{'page': 1, 'region': [.2, .2, .85, .8]}],
            'insufficient_evidence': False})}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 20}})
    def client(**kw):
        factories.append(kw)
        return HTTP_CLIENT(transport=httpx.MockTransport(respond), **kw)
    monkeypatch.setattr('nexus.vision.service.VLMAdapter', lambda workspace, cfg: VLMAdapter(workspace, cfg, client_factory=client))
    return calls, factories


def fake_faces(monkeypatch):
    def load(stream):
        with Image.open(stream) as image:
            return np.array(image.convert('RGB'))
    module = SimpleNamespace(load_image_file=load, face_locations=lambda *args, **kw: [],
                             face_encodings=lambda image: [[.1 if image[0, 0, 0] == 0 else .8] * 128])
    monkeypatch.setattr('nexus.vision.media.face_module', lambda: module)
    monkeypatch.setattr('nexus.vision.faces.face_module', lambda: module)
    monkeypatch.setattr('nexus.vision.faces.os_key', lambda *args, **kw: b'k' * 32)
    return module
