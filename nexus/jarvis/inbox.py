"""Persisted local result inbox, not an unapproved external notification transport."""
import time


class Inbox:
    def __init__(self, studio):
        self.studio = studio

    def items(self):
        return sorted(self.studio.items('jarvis-notice'), key=lambda r: r['created'], reverse=True)[:1000]

    def acknowledge(self, identifier):
        from .queue import Queue
        with Queue(self.studio).locked():
            row = self.studio.find('jarvis-notice', identifier)
            row.update(read=True, read_at=time.time())
            return self.studio.put('jarvis-notice', row, identifier)

    def pending(self):
        from .approvals import Approvals
        from .queue import Queue
        return {'jobs': [r for r in Queue(self.studio)._jobs() if r['status'] == 'awaiting_approval'],
                'actions': [r for r in Approvals(self.studio).items() if r['state'] == 'pending' and r['expires'] > time.time()]}

    def digest(self):
        rows = self.items()
        states = {}
        for row in rows:
            states[row['state']] = states.get(row['state'], 0) + 1
        return {'total': len(rows), 'unread': sum(not r['read'] for r in rows), 'states': states, 'pending_jobs': len(self.pending()['jobs']), 'pending_actions': len(self.pending()['actions']),
                'source': 'actual persisted local notices; no push/Telegram/OS delivery implied'}

    def explain(self, identifier):
        row = self.studio.find('jarvis-job', identifier)
        return {'job': row, 'approval_current': row.get('approval_digest'),
                'scope': 'Local loopback inference; operator-scoped tools; queue approval never approves individual mutations',
                'reservation_refunded': False,
                'retry_policy': 'No automatic replay. Error/interrupted jobs require retry then fresh approval; max 3 attempts.'}

    def delivery_policy(self):
        rows = self.studio.items('jarvis-delivery')
        return rows[0] if rows else {'quiet': False, 'dnd_until': 0}

    def configure(self, *, quiet=None, minutes=None, transport=None):
        from .queue import Queue
        with Queue(self.studio).locked():
            policy = self.delivery_policy()
            if transport is not None:
                if transport not in ('local', 'native', 'off'):
                    raise ValueError('Unknown notification transport')
                policy['transport'] = transport
            if quiet is not None:
                if type(quiet) is not bool:
                    raise ValueError('Boolean quiet mode required')
                policy['quiet'] = quiet
            if minutes is not None:
                if type(minutes) is not int or not 0 <= minutes <= 1440:
                    raise ValueError('DND must be 0–1440 minutes')
                policy['dnd_until'] = time.time() + 60 * minutes
            import hashlib
            ident = 'jarvis-delivery-' + hashlib.sha256(str(self.studio.root).encode()).hexdigest()[:24]
            return self.studio.put('jarvis-delivery', policy, ident)

    def notify(self):
        from .queue import Queue
        with Queue(self.studio).locked():
            policy = self.delivery_policy()
            if policy['quiet'] or policy['dnd_until'] > time.time():
                return {'suppressed': True, 'notices': [], 'source': 'quiet/DND; inbox still retains every result'}
            rows = [r for r in self.items() if not r.get('delivered') and not r['read']][:20]
            for row in rows:
                row.update(delivered=True, delivered_at=time.time())
                self.studio.put('jarvis-notice', row, row['id'])
            return {'suppressed': False, 'notices': rows, 'source': 'explicit local CLI notification pull; no external/OS push'}

    def push(self):
        """Best-effort OS submission; failures retained, never mark results read/delivered."""
        import hashlib
        import json
        import subprocess

        from .approvals import Approvals
        from .queue import Queue
        with Queue(self.studio).locked():
            policy = self.delivery_policy()
            if (policy.get('transport', 'local') != 'native' or policy['quiet'] or policy['dnd_until'] > time.time()
                    or time.time() - policy.get('last_push_attempt', 0) < 60):
                return {'submitted': False, 'suppressed': True}
            notices = [r['id'] for r in self.items() if not r['read']]
            jobs = [r['id'] for r in Queue(self.studio)._jobs() if r['status'] == 'awaiting_approval']
            approvals = [r['id'] for r in Approvals(self.studio).items() if r['state'] == 'pending' and r['expires'] > time.time()]
            digest = hashlib.sha256(json.dumps([sorted(notices), sorted(approvals), sorted(jobs)]).encode()).hexdigest()
            if not (notices or approvals or jobs) or digest == policy.get('push_digest'):
                return {'submitted': False, 'suppressed': True}
            # Reserve before releasing lock. A crash may skip one minute, not flood desktop.
            policy['last_push_attempt'] = time.time()
            self.studio.put('jarvis-delivery', policy, policy['id'])
        try:
            from .notifications import submit
            result = submit(len(notices), len(approvals) + len(jobs))
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            result = {'submitted': False, 'error_type': type(exc).__name__}
        with Queue(self.studio).locked():
            current = self.delivery_policy()
            if result['submitted']:
                current['push_digest'] = digest
            current['last_push_result'] = result
            self.studio.put('jarvis-delivery', current, current['id'])
        return result
