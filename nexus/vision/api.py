"""Shared authenticated HUD transport, one-use cloud review, memory-only uploads."""
from __future__ import annotations

import base64
import binascii
import hashlib
import threading

from ..assistant.common import FreshApproval
from ..tools import ApprovalDecision, ApprovalGate
from .policy import VisionError, digest, require
from .service import VisionService
from .state import control, hard_off, status
from .vlm import GEMINI_ENDPOINT

MAX_BODY = 14_000_000


class VisionAPI:
    def __init__(self, workspace, *, service=None):
        self.root = workspace
        self.service = service or VisionService(workspace)
        self.reviews = FreshApproval(ttl=60)
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)

    def _input(self, payload, client):
        cfg = require(self.root, 'files', channel='hud')
        require(self.root, 'vlm')
        if not isinstance(payload, dict) or set(payload) - {
            'name', 'file_base64', 'question', 'cloud', 'no_people', 'pages', 'region',
            'challenge', 'confirmation', 'second_factor', 'agent',
        }:
            raise VisionError('Unsupported vision request fields')
        # Old clients may name an agent; they cannot override operator model policy.
        if payload.get('agent') not in (None, '', cfg['vlm']['model']):
            raise VisionError('Select the model in operator-owned vision.yaml, not an API payload')
        encoded = payload.get('file_base64')
        if not isinstance(encoded, str) or len(encoded) > (cfg['files']['max_bytes'] + 2) // 3 * 4:
            raise VisionError('Bounded base64 upload required')
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise VisionError('Invalid image/PDF base64 upload') from None
        if not data or len(data) > cfg['files']['max_bytes']:
            raise VisionError('Upload is empty or exceeds policy limit')
        name, question = payload.get('name'), payload.get('question')
        if not isinstance(name, str) or not 1 <= len(name) <= 250 or not isinstance(question, str) or not 1 <= len(question) <= 4000:
            raise VisionError('Bounded filename and question required')
        cloud, no_people = payload.get('cloud', False), payload.get('no_people', False)
        if type(cloud) is not bool or type(no_people) is not bool:
            raise VisionError('Cloud/no-people choices must be booleans')
        if cloud != (cfg['vlm']['provider'] == 'gemini') or (cloud and not cfg['vlm']['allow_cloud']):
            raise VisionError('Requested provider does not match enabled vision.yaml policy')
        binding = {'name': name, 'sha256': hashlib.sha256(data).hexdigest(), 'question': question,
                   'cloud': cloud, 'no_people': no_people, 'pages': payload.get('pages'),
                   'region': payload.get('region'), 'policy': digest(cfg), 'epoch': control(self.root)['epoch'], 'client': client}
        return cfg, data, binding

    def post(self, action, payload, client=''):
        if action == 'off':
            return hard_off(self.root)
        if not self.slots.acquire(blocking=False):
            raise VisionError('Vision busy; no queued uploads or automatic retries')
        try:
            return self._post(action, payload, client)
        finally:
            self.slots.release()

    def _post(self, action, payload, client=''):
        if action == 'off':
            return hard_off(self.root)  # Never waits for device/model slots or a valid policy.
        if action not in ('ask', 'preview'):
            raise VisionError('Unknown vision action; enabling/resume is CLI/operator-only')
        cfg, data, binding = self._input(payload, client)
        if action == 'preview':
            if not binding['cloud'] or not binding['no_people']:
                raise VisionError('Cloud review requires an explicit no-people cloud request')
            # No model, capture, download or retained media during review.
            with self.lock:
                self.reviews._pending = {k: v for k, v in self.reviews._pending.items()
                                         if v['expires'] > self.reviews.clock()}
                if len(self.reviews._pending) >= 32:
                    raise VisionError('Too many pending vision reviews; wait for expiry')
                challenge = self.reviews.begin('ALLOW VISION CLOUD UPLOAD', binding)
            return {'challenge': challenge, 'review': {
                'sha256': binding['sha256'], 'bytes': len(data), 'model': cfg['vlm']['model'],
                'endpoint': GEMINI_ENDPOINT if binding['cloud'] else cfg['vlm']['base_url'],
                'cloud': binding['cloud'], 'no_people_attestation': binding['no_people'],
                'warning': 'No faces allowed; face/OCR detection is best effort. Cloud may incur charges.'}}
        def review(spec, args, risk, preview):
            challenge = payload.get('challenge')
            if spec.name != 'vision_cloud_upload' or not isinstance(challenge, dict):
                return ApprovalDecision(False)
            with self.lock:
                ok = self.reviews.consume(str(challenge.get('id', '')), str(payload.get('confirmation', '')), binding)
            return ApprovalDecision(ok, scope='once', second_factor=str(payload.get('second_factor') or ''))
        gate = ApprovalGate(callback=review)
        return self.service.ask_bytes(binding['name'], data, binding['question'], gate=gate, channel='hud',
                                      cloud=binding['cloud'], no_people=binding['no_people'],
                                      pages=binding['pages'], crop=binding['region'])


def service(state):
    with state.lock:
        api = getattr(state, 'vision_api', None)
        if api is None:
            api = VisionAPI(state.workspace)
            state.vision_api = api
        return api


def get(state):
    return status(state.workspace)


def post(state, action, payload, client=''):
    if state.demo and action != 'off':
        raise VisionError('Vision capture/analysis is unavailable in demo mode')
    return service(state).post(action, payload, client)
