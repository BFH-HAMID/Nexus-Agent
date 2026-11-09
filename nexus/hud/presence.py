"""One opt-in handoff state for the existing HTTP GUI and local voice pipeline.

Only bounded conversation text/status is shared. Tool calls, credentials, audio,
images and approval grants are deliberately absent. Reconnect sends a complete
snapshot, never replays a command. Cross-process writers use a private file lock.
"""
from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from pathlib import Path

from .presence_policy import load, require
from .presence_store import Store


def initial():
    return {'schema': 1, 'revision': 0, 'conversation_id': '', 'turns': [], 'active_mission': '',
            'speaker': None, 'speaker_generation': 0, 'voice': {}, 'speech': [], 'events': []}


class Presence:
    def __init__(self, workspace, *, clock=time.time, secret_values=()):
        self.workspace = Path(workspace).resolve()
        self.clock = clock
        self.secret_values = tuple(secret_values)
        self._stream_masks = {}  # In-memory credential prefixes, never written to shared state.
        self._stream_lock = threading.RLock()
        self.store = Store(self.workspace, 'conversation', initial)

    def enabled(self, capability='enabled'):
        cfg = load(self.workspace)['presence']
        if not cfg['enabled'] or not cfg['conversation_enabled']:
            self._forget_streams()
        return bool(cfg['enabled'] and cfg[capability])

    def clean(self, text, limit=8000):
        from ..redaction import mask, secrets
        # Never cache the secret list: rotations must take effect immediately.
        return mask(str(text or ''), secrets(self.workspace) + list(self.secret_values))[:limit]

    def _forget_streams(self, active=()):
        with self._stream_lock:
            for ident in list(self._stream_masks):
                if ident not in active:
                    self._stream_masks.pop(ident, None)

    def _prune(self, data, cfg):
        now = self.clock()
        data['turns'] = [r for r in data['turns'] if r['at'] > now - cfg['retention_hours'] * 3600][-cfg['history_turns']:]
        for turn in data['turns']:
            turn['user'], turn['assistant'] = self.clean(turn['user']), self.clean(turn['assistant'])
            if turn['status'] in {'queued', 'thinking', 'running'} and turn['updated'] < now - 900:
                turn['status'] = 'interrupted'  # No recovery/replay is implied.
        data['speech'] = [r for r in data['speech'] if r['expires'] > now][-16:]
        if not cfg['conversation_enabled']:
            data['turns'] = []
        if data['voice'].get('at', 0) < now - cfg['retention_hours'] * 3600:
            data['voice'] = {}
        if not cfg['voice_enabled']:
            data['voice'], data['speech'] = {}, []
        if data['voice']:
            data['voice']['transcript'] = self.clean(data['voice'].get('transcript'), 4000)
        for row in data['speech']:
            row['text'] = self.clean(row['text'], 2000)
        self._forget_streams({r['id'] for r in data['turns'] if r['status'] in {'queued', 'thinking', 'running'}})
        if not data['conversation_id']:
            data['conversation_id'] = uuid.uuid4().hex

    def _change(self, kind, change, capability='enabled'):
        cfg = require(self.workspace, capability)
        def mutate(data):
            self._prune(data, cfg)
            result = change(data)
            data['events'] = (data['events'] + [{'seq': data['revision'] + 1, 'kind': kind,
                                               'at': self.clock()}])[-128:]
            return result
        return self.store.update(mutate)

    def snapshot(self):
        try:
            cfg = require(self.workspace)
        except ValueError:
            self._forget_streams()
            raise
        data = self.store.read()
        before = copy.deepcopy(data)
        self._prune(data, cfg)
        if data != before:
            # Persist the initial identity and retention pruning; never replay a turn.
            data = self.store.update(lambda current: self._prune(current, require(self.workspace)))
        for row in data['turns']:
            row['user'], row['assistant'] = self.clean(row['user']), self.clean(row['assistant'])
        if data['voice']:
            data['voice']['transcript'] = self.clean(data['voice'].get('transcript'), 4000)
        for row in data['speech']:
            row['text'] = self.clean(row['text'], 2000)
        data['speaker'] = data['speaker'] if data['speaker'] is not None else cfg['default_speaker']
        # Stale microphone flags are never shown as current hardware observations.
        voice = data['voice']
        if voice and self.clock() - voice.get('at', 0) > 30:
            data['voice'] = {**voice, 'state': 'unknown', 'stale': True}
        return data

    def history(self):
        from ..types import Message
        data = self.snapshot()
        messages = []
        for row in data['turns']:
            if row['status'] not in {'done', 'error', 'interrupted'}:
                continue
            messages.append(Message(role='user', content=row['user']))
            if row['assistant']:
                messages.append(Message(role='assistant', content=row['assistant']))
        return messages

    def begin(self, text, *, source='gui', task_id=''):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 8000:
            raise ValueError('A bounded conversation turn is required')
        if self.clean(text) != text:
            raise ValueError('Credentials cannot be conversation content')
        ident = uuid.uuid4().hex
        def start(data):
            if any(r['status'] in {'queued', 'thinking', 'running'} for r in data['turns']):
                raise ValueError('Conversation is busy; wait for the current turn (no duplicate submission)')
            now = self.clock()
            data['turns'].append({'id': ident, 'task_id': task_id, 'user': text, 'assistant': '',
                                  'source': source, 'at': now, 'updated': now, 'status': 'queued',
                                  'usage': {'input_tokens': 0, 'output_tokens': 0, 'cost_usd': None}})
            data['turns'] = data['turns'][-load(self.workspace)['presence']['history_turns']:]
            return ident
        return self._change('conversation.started', start, 'conversation_enabled')

    def agent_event(self, ident, event):
        if not self.enabled('conversation_enabled'):
            return
        kind = event.get('type')
        if kind not in {'think', 'text_delta', 'text', 'usage', 'done', 'error', 'approval_request', 'approval_response'}:
            return
        def update(data):
            row = next((r for r in data['turns'] if r['id'] == ident), None)
            if row is None or row['status'] in {'done', 'error', 'interrupted'}:
                self._stream_masks.pop(ident, None)
                return {'completed_text': ''}
            row['updated'] = self.clock()
            if kind == 'text_delta':
                from ..redaction import StreamMask, secrets
                with self._stream_lock:
                    stream = self._stream_masks.setdefault(ident, StreamMask(list(self.secret_values), hold_words=True))
                    stream.values = sorted(set(stream.values) | set(secrets(self.workspace)), key=len, reverse=True)
                    piece = stream.feed(str(event.get('text') or ''))
                row['assistant'] = self.clean(row['assistant'] + piece)
                row['status'] = 'thinking'
            elif kind == 'text':
                self._stream_masks.pop(ident, None)
                row['assistant'] = self.clean(event.get('text'))
            elif kind == 'usage':
                for key in ('input_tokens', 'output_tokens'):
                    value = event.get(key)
                    if type(value) is int and 0 <= value < 10**10:
                        row['usage'][key] = value
                # No invented free/zero cost when no verified model price is available.
            elif kind == 'done':
                stream = self._stream_masks.pop(ident, None)
                if stream:
                    row['assistant'] = self.clean(row['assistant'] + stream.feed('', final=True))
                result = event.get('result') or {}
                row['status'] = 'done' if event.get('status') == 'done' else 'error'
                row['assistant'] = self.clean(result.get('text') or result.get('summary') or row['assistant'])
            elif kind == 'error':
                self._stream_masks.pop(ident, None)
                row['status'] = 'error'
            else:
                row['status'] = 'thinking'
            return {'completed_text': row['assistant'] if kind == 'done' and row['status'] == 'done' and row['source'] != 'voice' else ''}
        result = self._change('conversation.' + kind, update, 'conversation_enabled')
        if result['completed_text'] and self.enabled('voice_enabled'):
            self.say(result['completed_text'])

    def voice(self, state, *, source='local', transcript=''):
        if not self.enabled('voice_enabled'):
            return
        if state not in {'idle', 'listening', 'thinking', 'speaking', 'unknown'}:
            raise ValueError('Unknown voice state')
        def update(data):
            data['voice'] = {'state': state, 'source': source, 'at': self.clock(),
                             'transcript': self.clean(transcript, 4000), 'authority': 'display only; never approval'}
        self._change('voice.state', update, 'voice_enabled')

    def handoff(self, speaker, mission):
        def change(data):
            data.update(speaker=speaker, active_mission=mission,
                        speaker_generation=data['speaker_generation'] + 1, speech=[])
            return {'speaker': speaker, 'active_mission': mission, 'conversation_id': data['conversation_id'],
                    'speaker_generation': data['speaker_generation'], 'approval_grants_transferred': False}
        return self._change('handoff', change, 'handoff_enabled')

    def speaker(self):
        if not self.enabled('voice_enabled'):
            return 'local'  # Legacy voice behavior is unchanged while sharing is OFF.
        return self.snapshot()['speaker']

    def say(self, text, *, language='en'):
        """Route text, not audio. A device must claim once before local synthesis."""
        safe = self.clean(text, 2000)
        def add(data):
            target = data['speaker'] or load(self.workspace)['presence']['default_speaker']
            if target in {'none', 'local'} or not safe:
                return {'queued': False}
            row = {'id': uuid.uuid4().hex, 'text': safe, 'language': language[:12],
                   'device': target, 'generation': data['speaker_generation'], 'expires': self.clock() + 30}
            data['speech'] = (data['speech'] + [row])[-16:]
            return {'queued': True, 'id': row['id'], 'played': False}
        return self._change('speech.ready', add, 'voice_enabled')

    def claim(self, ident, device):
        def take(data):
            row = next((r for r in data['speech'] if r['id'] == ident), None)
            if (not row or row['device'] != device or data['speaker'] != device
                    or row['generation'] != data['speaker_generation']):
                raise ValueError('Speech is stale, consumed or assigned to another device')
            data['speech'].remove(row)  # Consume before synthesis; uncertain results are never retried.
            return row
        return self._change('speech.claimed', take, 'voice_enabled')

    def finish(self, ident, text='', *, ok=True):
        self.agent_event(ident, {'type': 'done', 'status': 'done' if ok else 'error', 'result': {'text': text}})

    def events_since(self, cursor):
        data = self.snapshot()
        events = data.pop('events')
        return {'reset': not events or cursor < events[0]['seq'] - 1 or cursor > data['revision'],
                'events': [r for r in events if r['seq'] > cursor], 'state': data}


def publish_voice(workspace, state, **kwargs):
    """Optional observational hook; a broken dashboard must never grant authority."""
    try:
        Presence(workspace).voice(state, **kwargs)
    except (ValueError, OSError, TimeoutError):
        pass


def mission_dag(workspace, selected=''):
    from ..chief import list_missions
    missions = list_missions(Path(workspace), limit=10)
    clean = Presence(workspace).clean
    return [{'id': m.id, 'goal': clean(m.goal, 240), 'status': m.status,
             'selected': m.id == selected,
             'cost_usd': m.total_cost_usd, 'cost_cap_usd': m.cost_cap_usd,
             'cost_basis': 'Chief recorded estimate, not a provider invoice',
             'nodes': [{'id': s.id, 'title': clean(s.title, 240), 'status': s.status,
                        'depends_on': list(s.depends_on)} for s in m.steps[:80]]}
            for m in missions]


def exact_approvals(state):
    """No truncated or redacted request may be granted by the compact HUD."""
    from .. import approvalcenter
    raw, errors = approvalcenter._sources(state)
    clean = approvalcenter._clean(state, raw)
    result = []
    budget = 500_000
    for original, row in zip(raw[:20], clean[:20], strict=True):
        action = {'tool': row['tool'], 'arguments': row['args']}
        if row['source'] == 'queue':
            job = state.workbench.studio.find('jarvis-job', row['reference'])
            action['task'] = job['task']
            action = approvalcenter._clean(state, action)
        text = json.dumps(action, ensure_ascii=False, sort_keys=True, indent=2)
        size = len(text.encode())
        complete = original == row and not row['args'].get('truncated') and size <= 200_000 and size <= budget
        if row['source'] == 'queue' and action.get('task') != job['task']:
            complete = False
        budget -= min(size, budget)
        result.append({**row, 'action_text': text if complete else 'Full exact review unavailable. Use the original local review; deny remains available.',
                       'complete': complete, 'scopes': ['once'],
                       'decisions': row['decisions'] if complete else [d for d in row['decisions'] if d in {'deny', 'reject', 'cancel'}]})
    return result, errors
