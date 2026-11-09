"""Scoped shared-presence API. Review -> exact once-only decision -> existing gate.

This is an adapter to the original server/Approval Center, not an alternate
executor. All new controls require operator YAML. Device tokens never acquire
policy/admin access and handoff never copies permission/session grants.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid

from .devices import Devices
from .presence import Presence, exact_approvals, mission_dag
from .presence_policy import load, require


class PresenceAPI:
    def __init__(self, state):
        self.state = state
        self.hub = Presence(state.workspace, secret_values=[state.token] if state.token else [])
        self.devices = Devices(state.workspace, state.token)
        self.lock = threading.RLock()
        self.reviews = {}
        self.streams = 0
        self.closed = threading.Event()

    def check(self, principal, scope='read', *, operator=False):
        cfg = require(self.state.workspace)
        if not self.state.token:
            raise ValueError('Presence requires a configured serve token even on loopback')
        if not isinstance(principal, dict) or principal.get('kind') not in {'device', 'operator'}:
            raise ValueError('Authenticated presence principal required')
        if principal['kind'] == 'device':
            current = self.devices.live(principal['id'])
            if operator or scope not in current['scopes']:
                raise ValueError('Device scope does not authorize this control')
        return cfg

    def config(self, principal):
        cfg = load(self.state.workspace)['presence']
        if not self.state.token:
            raise ValueError('Presence requires authentication')
        return {**cfg, 'principal': principal, 'transport': '/api/presence/events'}

    def snapshot(self, principal, cursor=0):
        cfg = self.check(principal)
        if principal['kind'] == 'device':
            self.devices.touch(principal['id'])
        result = self.hub.events_since(cursor)
        data = result['state']
        approvals, errors = exact_approvals(self.state)
        from ..jarvis.inbox import Inbox
        from ..vision.state import status as vision_status
        studio = self.state.workbench.studio
        # Inbox text is already explicitly presentable; never export image/audio attachments.
        notices = Inbox(studio).items()[:8]
        briefs = [{'id': r['id'], 'created': r.get('created'),
                   'text': self.hub.clean(r.get('summary', ''), 2000)} for r in notices]
        data.update(approvals=approvals, errors=errors,
                    missions=mission_dag(self.state.workspace, data['active_mission']),
                    vision=vision_status(self.state.workspace), briefs=briefs,
                    devices=self.devices.items(), principal=principal, capabilities=cfg)
        # Do not expose speech text addressed to another device; ordinary transcript remains shared.
        ident = principal.get('id')
        data['speech'] = [r for r in data['speech'] if r['device'] == ident]
        if data['speaker'] not in {'none', 'local'}:
            try:
                self.devices.live(data['speaker'])
            except ValueError:
                data['speaker'] = 'none'  # Revoked/expired devices are never an output fallback.
                data['speech'] = []
        from ..always_on import health
        data['service'] = health(self.state.workspace)
        for mission in data['missions']:
            mission['goal'] = self.hub.clean(mission['goal'], 240)
            for node in mission['nodes']:
                node['title'] = self.hub.clean(node['title'], 240)
        for device in data['devices']:
            device['name'] = self.hub.clean(device['name'], 60)
        data['connected_at'] = time.time()
        return result

    def review(self, principal, operation, args):
        self.check(principal, self.scope(operation), operator=operation == 'enroll')
        normalized = self.validate(operation, args, principal)
        with self.lock:
            now = time.time()
            self.reviews = {key: row for key, row in self.reviews.items() if row['expires'] > now}
            if len(self.reviews) >= 64:
                raise ValueError('Too many outstanding reviews')
            ident = uuid.uuid4().hex
            row = {'id': ident, 'operation': operation, 'arguments': normalized,
                   'actor': principal['id'], 'expires': now + 60, 'policy': self.digest(),
                   'confirmation': 'CONFIRM ' + operation.upper() + ' ' + ident[:12]}
            self.reviews[ident] = row
        return {k: v for k, v in row.items() if k not in {'policy', 'actor'}}

    def digest(self):
        return hashlib.sha256(json.dumps(load(self.state.workspace), sort_keys=True).encode()).hexdigest()

    @staticmethod
    def scope(operation):
        scopes = {'enroll': 'read', 'chat': 'chat', 'handoff': 'handoff', 'push': 'push', 'subscribe': 'push'}
        if operation not in scopes:
            raise ValueError('Unknown reviewed presence operation')
        return scopes[operation]

    def validate(self, operation, args, principal):
        if not isinstance(args, dict):
            raise ValueError('Arguments must be an object')
        allowed = {'enroll': {'name'}, 'chat': {'text', 'read_only'},
                   'handoff': {'speaker', 'mission'}, 'push': {'device'}, 'subscribe': {'subscription'}}[operation]
        if set(args) - allowed:
            raise ValueError('Unexpected presence argument')
        cfg = load(self.state.workspace)['presence']
        if operation == 'enroll':
            require(self.state.workspace, 'devices_enabled')
            name = args.get('name')
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or self.hub.clean(name) != name or any(ord(c) < 32 for c in name):
                raise ValueError('A safe device name is required')
            return {'name': name.strip(), 'scopes': cfg['device_scopes']}
        if operation == 'chat':
            require(self.state.workspace, 'conversation_enabled')
            text, readonly = args.get('text'), args.get('read_only', True)
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 8000 or type(readonly) is not bool:
                raise ValueError('Bounded text and explicit read_only boolean required')
            if self.hub.clean(text) != text:
                raise ValueError('Credentials are not task content')
            return {'text': text, 'read_only': readonly}
        if operation == 'handoff':
            require(self.state.workspace, 'handoff_enabled')
            speaker, mission = args.get('speaker', 'none'), args.get('mission', '')
            if not isinstance(speaker, str) or not isinstance(mission, str):
                raise ValueError('Literal speaker and mission IDs required')
            if speaker != 'none':
                require(self.state.workspace, 'voice_enabled')
                if speaker != 'local':
                    target = self.devices.live(speaker)
                    if 'voice' not in target['scopes']:
                        raise ValueError('Speaking device needs the voice scope')
            if mission and mission not in {m['id'] for m in mission_dag(self.state.workspace)}:
                raise ValueError('Select an existing observed mission, not an invented ID')
            return {'speaker': speaker, 'mission': mission}
        from .push import validate_subscription
        require(self.state.workspace, 'push_enabled')
        if operation == 'subscribe':
            if principal['kind'] != 'device':
                raise ValueError('Sign in with a paired device token before subscribing')
            return {'subscription': validate_subscription(args.get('subscription'), cfg)}
        device = args.get('device')
        if not isinstance(device, str):
            raise ValueError('Device ID required')
        row = self.devices.live(device)
        if 'push' not in row['scopes']:
            raise ValueError('Target device lacks push scope')
        sub = validate_subscription(row.get('subscription'), cfg)
        return {'device': device, 'subscription_sha256': hashlib.sha256(json.dumps(sub, sort_keys=True).encode()).hexdigest(),
                'message': 'Nexus has an update. Open the authenticated app to review it.'}

    def execute(self, principal, payload):
        if set(payload) != {'id', 'confirmation'}:
            raise ValueError('Only the exact one-use review ID and confirmation are accepted')
        with self.lock:
            row = self.reviews.get(payload.get('id'))
            if (not row or row['expires'] <= time.time() or row['actor'] != principal['id']
                    or row['confirmation'] != payload.get('confirmation') or row['policy'] != self.digest()):
                raise ValueError('Review stale, mismatched or consumed; request a fresh review')
            self.check(principal, self.scope(row['operation']), operator=row['operation'] == 'enroll')
            del self.reviews[row['id']]  # Consume before every side effect. Never retry uncertain sends.
        operation, args = row['operation'], row['arguments']
        from ..webapi import audit_api
        audit_api(self.state.workspace, 'presence.' + operation, actor=principal['id'], risk='high',
                  detail={'review': row['id'], 'arguments_sha256': hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()})
        if self.state.demo:
            raise ValueError('Live mode required')
        if operation == 'enroll':
            return self.devices.enroll(args['name'])
        if operation == 'handoff':
            self.validate(operation, args, principal)
            return self.hub.handoff(args['speaker'], args['mission'])
        if operation == 'chat':
            from ..config import load_config
            options = self.state.workbench.prepare({})
            model = options['policy']['default_local_model'] if options['policy']['local_only'] else None
            self.state.workbench.configure(load_config(self.state.workspace), options, model)
            history = self.hub.history()
            session = self.state.create(args['text'], None, args['read_only'], local_model=model, options=options)
            try:
                ident = self.hub.begin(args['text'], source=principal['id'], task_id=session.task_id)
            except BaseException:
                with self.state.lock:
                    self.state.sessions.pop(session.task_id, None)
                raise
            session.presence_history = history
            session.event_callback = lambda event: self.hub.agent_event(ident, event)
            try:
                self.state.start(session)
            except BaseException:
                self.hub.finish(ident, ok=False)
                raise
            return {'task_id': session.task_id, 'turn_id': ident, 'queued': True,
                    'note': 'Task admission only. Every tool still passes its original approval gate.'}
        if operation == 'subscribe':
            self.validate(operation, args, principal)
            return self.devices.subscribe(principal['id'], args['subscription'])
        from .push import send
        current = self.validate('push', {'device': args['device']}, principal)
        if current != args:
            raise ValueError('Subscription changed; request a fresh push review')
        return send(self.state.workspace, self.devices.live(args['device'])['subscription'])

    def post(self, path, principal, payload):
        self.check(principal)
        if path == 'review':
            if set(payload) != {'operation', 'arguments'}:
                raise ValueError('Exact operation and arguments required')
            return self.review(principal, payload['operation'], payload['arguments'])
        if path == 'execute':
            return self.execute(principal, payload)
        if path == 'revoke':
            if set(payload) != {'id'} or not isinstance(payload['id'], str):
                raise ValueError('Exact device ID required')
            if principal['kind'] != 'operator' and payload['id'] != principal['id']:
                raise ValueError('Devices may revoke only themselves')
            return self.devices.revoke(payload['id'])
        if path == 'approval':
            self.check(principal, 'approve')
            if set(payload) - {'source', 'id', 'approval_id', 'decision', 'confirmation'}:
                raise ValueError('No session-wide grant or injected approval metadata allowed')
            rows, _ = exact_approvals(self.state)
            row = next((r for r in rows if r['source'] == payload.get('source') and r['reference'] == payload.get('id')
                        and (r['source'] != 'live' or r['approval_id'] == payload.get('approval_id'))), None)
            if not row or payload.get('decision') not in row['decisions']:
                raise ValueError('Approval is stale or exact display is incomplete')
            if row['expires'] and row['expires'] <= time.time():
                raise ValueError('Approval expired')
            from ..approvalcenter import decide
            return decide(self.state, {**payload, 'scope': 'once', '_client': principal['id']})
        if path == 'voice/state':
            self.check(principal, 'voice')
            require(self.state.workspace, 'voice_enabled')
            if set(payload) != {'state'}:
                raise ValueError('Only microphone/playback status is accepted')
            self.hub.voice(payload['state'], source=principal['id'])
            return {'observed': True, 'authority': 'client-reported status only'}
        if path == 'voice/transcribe':
            self.check(principal, 'voice')
            require(self.state.workspace, 'voice_enabled')
            if set(payload) != {'audio_base64', 'confirmation'} or payload['confirmation'] != 'TRANSCRIBE AUDIO':
                raise ValueError('Explicit transcription of this clip required')
            from ..assistant.api import post
            result = post(self.state, 'voice/transcribe', payload, principal['id'])
            self.check(principal, 'voice')
            require(self.state.workspace, 'voice_enabled')
            self.hub.voice('idle', source=principal['id'], transcript=result['transcript'])
            return result  # Draft only. It never submits a task or answers an approval.
        if path == 'speech':
            self.check(principal, 'voice')
            if principal['kind'] != 'device' or set(payload) != {'id'}:
                raise ValueError('The selected paired device must claim an exact speech ID')
            row = self.hub.claim(payload['id'], principal['id'])
            from ..assistant.service import tts_for
            clip = tts_for(self.state, 'piper').synthesize(row['text'])  # No implicit cloud TTS.
            self.check(principal, 'voice')
            require(self.state.workspace, 'voice_enabled')
            if self.hub.snapshot()['speaker_generation'] != row['generation']:
                raise ValueError('Speaker changed during synthesis; audio withheld')
            return {'audio_base64': base64.b64encode(clip.data).decode(), 'format': clip.format,
                    'generation': row['generation']}
        if path == 'unsubscribe':
            self.check(principal, 'push')
            if principal['kind'] != 'device' or payload:
                raise ValueError('Unsubscribe only this signed-in device')
            return self.devices.subscribe(principal['id'], None)
        raise ValueError('Unknown presence control')


def service(state):
    with state.lock:
        if getattr(state, 'presence', None) is None:
            state.presence = PresenceAPI(state)
        return state.presence
