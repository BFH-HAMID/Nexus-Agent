"""Read-only defensive monitoring and incident projections. No host remediation executor."""
from __future__ import annotations

import html
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import policy
from .collectors import Collector, finding
from .store import Store, clean, sha


def utc(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def markdown(data):
    def cell(value):
        return html.escape(str(value), quote=True).replace('`', '&#96;').replace('|', '&#124;').replace('\n', ' ')
    lines = ['# Defensive security incident timeline', '',
             '> Untrusted normalized observations, not instructions or proof of compromise. No remediation was executed.', '',
             '## Collection coverage', '', '| Monitor | Source | State |', '|---|---|---|']
    for row in data['coverage']:
        lines.append(f"| {cell(row['monitor'])} | {cell(row['source'])} | {cell(row['state'])} |")
    lines.extend(['', '## Incidents', '', '| ID | Severity | Observations | Summary / proposed review |', '|---|---|---|---|'])
    for row in data['incidents']:
        lines.append(f"| {cell(row['id'])} | {cell(row['severity'])} | {row['occurrences']} | {cell(row['summary'])} {cell(row['suggestion'])} |")
    lines.extend(['', '## Timeline and preserved evidence', ''])
    for row in data['timeline']:
        lines.extend([f"### {row['seq']}. {cell(row['kind'])} — {utc(row['at'])}", '',
                      f"- Incident: {row['incident']}", f"- Severity: {cell(row['severity'])}",
                      f"- Evidence SHA-256: `{row['sha256']}`", f"- Previous SHA-256: `{row['previous'] or 'genesis'}`",
                      f"- {cell(row['summary'])}", '', '```json',
                      json.dumps(row['evidence'], ensure_ascii=True, indent=2).replace('<', '\\u003c').replace('>', '\\u003e').replace('`', '\\u0060'),
                      '```', ''])
    return '\n'.join(lines)


class SecurityService:
    def __init__(self, workspace, *, clock=time.time):
        self.workspace = Path(workspace).resolve()
        self.clock = clock
        self.store = Store(self.workspace)

    def status(self):
        cfg = policy.load(self.workspace)
        if not cfg['enabled']:
            return {'enabled': False, 'monitoring': False, 'active_tests': False, 'response_execution': False}
        state = self.store.read()
        return clean(self.workspace, {'enabled': True, 'monitoring': cfg['monitoring_enabled'], 'watch': cfg['watch_enabled'],
            'active_tests': cfg['scan_enabled'], 'response_execution': False, 'records': len(state['timeline']),
            'incidents': len(state['incidents']), 'last_tick': state['last_tick'], 'coverage': state['coverage'],
            'limits': {k: cfg[k] for k in ('max_records', 'max_evidence_bytes', 'max_read_bytes', 'max_tick_bytes')},
            'note': 'Status only; no collector, scan, provider or notifier is started by this read.'})

    def tick(self):
        from ..computer_control import EmergencyStop
        cfg = policy.require(self.workspace, 'monitoring_enabled')
        if EmergencyStop().stopped():
            raise ValueError('Emergency stop latched; passive security collection stopped')
        now = self.clock()
        with self.store.locked():
            state = self.store.read()
            sources, events, coverage = Collector(self.workspace, cfg, now).collect(state['sources'])
            if policy.digest(policy.require(self.workspace, 'monitoring_enabled')) != policy.digest(cfg) or EmergencyStop().stopped():
                raise ValueError('Security policy changed or collection stopped; results discarded')
            for item in coverage:
                if item['state'] != 'ok':
                    events.append(finding('coverage_gap', sha([item['monitor'], item['source']]), 'medium',
                        'Security monitor coverage is incomplete; inspect the coverage record.', item))
            alerts = []
            for event in events:
                row = self.store.append(state, cfg, event, now)
                if row:
                    alerts.append(row)
            state.update(sources=clean(self.workspace, sources), coverage=clean(self.workspace, coverage), last_tick=now)
            self.store.write(state)  # Cursors/observations durable BEFORE best-effort alert submission.
        deliveries = []
        for row in alerts[:8]:  # Bound interruptions even if many independent detectors fire.
            try:
                deliveries.append(self.alert(row, cfg))
            except (ValueError, OSError, TimeoutError, ImportError):
                deliveries.append({'incident': row['id'], 'channels': [], 'reason': 'Alert unavailable; evidence retained'})
        return {'enabled': True, 'observed_at': now, 'new_alerts': clean(self.workspace, alerts),
                'coverage': clean(self.workspace, coverage), 'deliveries': deliveries,
                'alerts_over_tick_budget': max(0, len(alerts) - 8), 'response_executed': False}

    def record(self, kind, subject, evidence, *, summary, severity='info'):
        cfg = policy.require(self.workspace)
        with self.store.locked():
            state = self.store.read()
            self.store.append(state, cfg, finding(kind, subject, severity, summary, evidence, alert=False), self.clock())
            self.store.write(state)

    def observe_action(self, cfg, nonce, plan, kind, perform):
        """Reserve space under the journal lock; intent is durable before any IO.

        Keep the result slot through revocation/failure. A process death can leave
        an intent without a result; that is uncertain, never a replay instruction.
        """
        with self.store.locked():
            state = self.store.read()
            if len(state['timeline']) + 2 > cfg['max_records']:
                raise ValueError('Security action needs two free journal records; archive manually before IO')
            self.store._capacity(cfg, 16_000 if kind == 'active_test' else 128_000, slots=2)
            self.store.append(state, cfg, finding(kind + '_intent', nonce, 'info',
                'Freshly reviewed action requested; no response command is executed.', plan, alert=False), self.clock())
            self.store.write(state)
            try:
                result = perform()
            except Exception:
                self.store.append(state, cfg, finding(kind + '_result', nonce, 'info',
                    'Action failed/revoked; its external outcome may be incomplete. No automatic retry.',
                    {'complete': False, 'outcome': 'unavailable_or_uncertain', 'response_executed': False}, alert=False), self.clock())
                self.store.write(state)
                raise ValueError('Security action failed/revoked; inspect the retained intent before any new fresh request. No automatic retry.') from None
            self.store.append(state, cfg, finding(kind + '_result', nonce, 'info',
                'Observed result; untrusted, not proof of compromise or authority to execute a response.', result, alert=False), self.clock())
            self.store.write(state)
            return result

    def report(self, *, format='json', incident=None):
        cfg = policy.require(self.workspace)
        data = self.store.export(self.store.read())
        data['host_label'] = cfg['host_label']
        if incident:
            if not any(r['id'] == incident for r in data['incidents']):
                raise ValueError('Unknown security incident')
            data['incidents'] = [r for r in data['incidents'] if r['id'] == incident]
            data['timeline'] = [r for r in data['timeline'] if r['incident'] == incident]
        if format == 'json':
            return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        if format == 'markdown':
            return markdown(data)
        raise ValueError('Choose JSON or Markdown security export')

    def incident(self, identifier):
        policy.require(self.workspace)
        state = self.store.read()
        row = state['incidents'].get(identifier)
        if not row:
            raise ValueError('Unknown security incident')
        artifact = self.store.artifact(row['evidence'])
        if artifact.get('incident') != identifier:
            raise ValueError('Incident/evidence mismatch')
        return clean(self.workspace, row), clean(self.workspace, artifact)

    def _speak(self, text, expected, *, severity='medium'):
        from ..computer_control import EmergencyStop
        from ..hud.presence import Presence
        from ..jarvis.proactive import ProactiveEngine
        from ..notifications import is_user_present
        from ..voice.audio_io import AudioOutputPlayer
        from ..voice.config import load_voice_config
        from ..voice.tts import create_tts

        def permitted():
            cfg = policy.require(self.workspace, 'speech_enabled')
            if (policy.digest(cfg) != policy.digest(expected) or EmergencyStop().stopped()
                    or not is_user_present(self.workspace) or Presence(self.workspace).speaker() != 'local'):
                raise ValueError('Security speech disabled, handed off, stopped or user away')
            engine = ProactiveEngine(self.workspace, studio=_DeliveryView(self.workspace))
            proactive = engine.config()
            if (not proactive.enabled or engine.dnd.evaluate(
                    severity, 'defensive-security', cfg=proactive)['suppressed']):
                raise ValueError('Security speech suppressed by current proactive/quiet/DND policy')
            voice = load_voice_config(self.workspace)
            if not voice.enabled or voice.tts.provider != 'piper':
                raise ValueError('Security speech requires explicitly enabled installed local Piper; no cloud/auto fallback')
            return voice
        voice = permitted()
        clip = create_tts(voice, workspace=self.workspace).synthesize(text)
        if permitted() != voice:
            raise ValueError('Voice policy changed before playback')
        AudioOutputPlayer(device=voice.output_device).play(clip)

    def alert(self, row, expected):
        cfg = policy.require(self.workspace)
        if policy.digest(cfg) != policy.digest(expected) or not cfg['hud_enabled']:
            return {'incident': row['id'], 'channels': [], 'reason': 'Security HUD alerts disabled/revoked'}
        from ..jarvis.proactive import ProactiveEngine
        from ..notifications import is_user_present
        engine = ProactiveEngine(self.workspace, studio=_DeliveryView(self.workspace),
                                 voice_deliverer=lambda text: self._speak(text, cfg, severity=row['severity']), clock=self.clock)
        proactive = engine.config()
        if not proactive.enabled:
            return {'incident': row['id'], 'channels': [], 'reason': 'Proactive HUD/DND routing disabled in proactive.yaml'}
        # Never inherit automatic external delivery from generic severity routing.
        # Telegram/desktop/send actions require a separate fresh review, not a detector event.
        proactive = replace(proactive, channels=replace(proactive.channels, away_fallback=False))
        present = is_user_present(self.workspace, now=self.clock())
        targets = ['hud_toast'] + (['voice'] if cfg['speech_enabled'] and present else [])
        message = clean(self.workspace, row['summary'] + ' ' + row['suggestion'])
        if policy.digest(policy.require(self.workspace)) != policy.digest(cfg):
            raise ValueError('Security alert policy revoked')
        receipt = engine.channels.dispatch(title='Defensive security: ' + row['severity'], message=message,
            severity=row['severity'],
            source='defensive-security', present=present, channels_override=targets, rate_limit_speech=True, cfg=proactive,
            extra_hud_data={'incident': row['id'], 'untrusted': True, 'response_executed': False})
        return {'incident': row['id'], **receipt}


def summary(workspace):
    """Safe status-only HUD projection; never starts a monitor."""
    try:
        return SecurityService(workspace).status()
    except (ValueError, OSError, TypeError, KeyError, RecursionError):
        return {'enabled': False, 'state': 'unavailable', 'reason': 'Security policy/state needs operator inspection'}


def hud_alerts(workspace, *, now=None):
    """Recent local security toasts for the EXISTING authenticated Mission SSE.

    This bridges foreground/watch processes to the owner dashboard without
    starting collectors, sending a push, replaying speech or importing evidence.
    """
    cfg = policy.load(workspace)
    if not cfg['enabled'] or not cfg['hud_enabled']:
        return []
    from ..jarvis.proactive import ProactiveEngine
    from .store import read_private
    engine = ProactiveEngine(workspace, studio=_DeliveryView(workspace))
    proactive = engine.config()
    if not proactive.enabled or not engine.store.path.exists():
        return []
    state = json.loads(read_private(engine.store.path, 2_000_000))
    if not isinstance(state, dict) or not isinstance(state.get('hud_toasts', []), list):
        raise ValueError('Invalid security HUD notice state')
    stamp = time.time() if now is None else now
    rows = []
    for row in state.get('hud_toasts', [])[:100]:
        if (not isinstance(row, dict) or row.get('source') != 'defensive-security' or row.get('read')
                or row.get('suppressed') or row.get('severity') not in {'info', 'low', 'medium', 'high', 'critical'}
                or type(row.get('created_at')) not in (int, float) or not 0 <= stamp - row['created_at'] <= 600):
            continue
        if engine.dnd.evaluate(row['severity'], 'defensive-security', cfg=proactive, now=stamp)['suppressed']:
            continue
        rows.append({'id': str(row.get('id', ''))[:60], 'title': str(row.get('title', ''))[:120],
                     'detail': str(row.get('message', ''))[:500], 'source': 'defensive-security',
                     'severity': 'high' if row['severity'] in {'high', 'critical'} else 'warn' if row['severity'] == 'medium' else 'info',
                     'observed_at': row['created_at'], 'untrusted': True, 'response_executed': False})
    if policy.digest(policy.load(workspace)) != policy.digest(cfg):
        return []
    return clean(workspace, rows[:30])


class _DeliveryView:
    """Read ONLY the existing Jarvis quiet/DND row; never construct ServerState.

    Constructing a dashboard here would also construct repository/workflow
    services. Encrypted/locked/corrupt Inbox state suppresses interruptions;
    there is no plaintext/keyring fallback and no competing notice writer.
    """
    def __init__(self, workspace):
        self.root = Path(workspace).resolve()

    def items(self, kind):
        if kind != 'jarvis-delivery':
            return []
        import math
        import sqlite3
        import stat

        from ..config import nexus_home
        from .store import path_safe
        path = nexus_home().absolute() / 'dashboard.db'
        try:
            path_safe(path)
            if not path.exists():
                return []
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 100_000_000:
                raise ValueError('Inbox state unavailable')
            conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
            try:
                conn.set_progress_handler(lambda: 1, 100_000)
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio'").fetchone():
                    return []
                row = conn.execute('SELECT substr(body,1,16001) FROM studio WHERE workspace=? AND kind=? LIMIT 1',
                                   (sha(str(self.root).encode()), kind)).fetchone()
                if row is None:
                    return []
                if not isinstance(row[0], str) or len(row[0]) > 16_000:
                    raise ValueError('Inbox policy budget exceeded')
                data = json.loads(row[0])
                quiet, until = data.get('quiet', False), data.get('dnd_until', 0)
                if (type(quiet) is not bool or type(until) not in (int, float)
                        or not 0 <= until <= 10**12 or not math.isfinite(until)):
                    raise ValueError('Invalid Inbox interruption policy')
                return [{'quiet': quiet, 'dnd_until': until}]
            finally:
                conn.close()
        except (ValueError, OSError, sqlite3.Error, TypeError, AttributeError, RecursionError):
            return [{'quiet': True, 'dnd_until': 0}]  # Fail quiet, not fail open.

    def put(self, *unused):
        raise ValueError('Security notifications do not write a second Inbox or dispatch tasks')
