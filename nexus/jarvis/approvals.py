"""One-use background approvals. Queue approval is never action approval."""
from __future__ import annotations

import hashlib
import json
import time

from .queue import Queue


class Approvals:
    def __init__(self, studio):
        self.studio = studio
        self.queue = Queue(studio)

    def items(self):
        return self.studio.items('jarvis-approval')

    def decide(self, identifier, allow, confirmation=''):
        from ..computer_control import EmergencyStop
        with self.queue.locked():
            row = self.studio.find('jarvis-approval', identifier)
            job = self.studio.find('jarvis-job', row['job_id'])
            if row['state'] != 'pending' or row['expires'] <= time.time() or job['status'] != 'running' or row['lease'] != job['lease']:
                raise ValueError('Approval is stale, terminal or expired')
            if allow and (confirmation != row['confirmation'] or EmergencyStop().stopped()):
                raise ValueError('Exact fresh action approval required')
            if job.get('active_role'):
                from ..sessions import SessionStore
                row['feedback_id'] = SessionStore().record_routing_feedback(job['task'], [job['active_role']], allow,
                    workspace=self.queue.root, reason='Explicit background action consent, not test success: ' + row['tool'])
            row.update(state='allowed' if allow else 'denied', decided=time.time())
            return self.studio.put('jarvis-approval', row, identifier)

    def callback(self, job):
        from ..computer_control import EmergencyStop
        from ..github_safety import scan_text
        from ..tools import ApprovalDecision
        from ..workbench import proposal
        def approve(spec, args, risk, preview):
            serialized = json.dumps(args, sort_keys=True)
            scan_text(serialized, workspace=self.queue.root)
            scan_text(preview, workspace=self.queue.root)
            if len(serialized.encode()) > 100000 or len(self.items()) >= 1000:
                return ApprovalDecision(False, reason='Background approval quota reached')
            detail = proposal(self.queue.root, spec.name, args)
            scan_text(json.dumps(detail), workspace=self.queue.root)
            path = None
            before = None
            if spec.name in {'write_file', 'edit_file', 'delete_file'}:
                from ..permissions import Permissions
                path = Permissions(self.queue.root).path(args['path'])
                from .sources import bounded
                before = hashlib.sha256(bounded(path, 500000)).hexdigest() if path.is_file() else None
            digest = hashlib.sha256(serialized.encode()).hexdigest()
            row = self.studio.put('jarvis-approval', {'job_id': job['id'], 'lease': job['lease'], 'tool': spec.name,
                                   'arguments': args, 'args_digest': digest, 'risk': risk.value, 'preview': preview,
                                   'proposal': detail, 'before_digest': before, 'state': 'pending',
                                   'expires': min(job['expires'], job['started'] + job['runtime']['max_seconds']),
                                   'created': time.time()})
            row['confirmation'] = 'ALLOW ACTION ' + row['id'] + ' ' + digest[:12]
            self.studio.put('jarvis-approval', row, row['id'])
            while time.time() < row['expires']:
                current = self.studio.find('jarvis-job', job['id'])
                policy = self.queue.policy()
                if (current['status'] != 'running' or current.get('lease') != job['lease'] or EmergencyStop().stopped()
                        or hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest() != job['runtime_policy']):
                    break
                with self.queue.locked():
                    current = self.studio.find('jarvis-job', job['id'])
                    if (current['status'] != 'running' or current.get('lease') != job['lease']
                            or hashlib.sha256(json.dumps(self.queue.policy(), sort_keys=True).encode()).hexdigest() != job['runtime_policy']):
                        break
                    decision = self.studio.find('jarvis-approval', row['id'])
                    if decision['state'] in {'allowed', 'denied'}:
                        allowed = decision['state'] == 'allowed'
                        if path is not None:
                            from .sources import bounded
                            after = hashlib.sha256(bounded(path, 500000)).hexdigest() if path.is_file() else None
                            allowed = allowed and before == after
                        decision.update(state='consumed', approved=allowed, consumed=time.time())
                        self.studio.put('jarvis-approval', decision, decision['id'])
                        return ApprovalDecision(allowed, reason='one-use background action review; changed files invalidate consent')
                # Not time.sleep: independent of provider retry backoff and tests.
                import threading
                threading.Event().wait(.2)
            with self.queue.locked():
                row = self.studio.find('jarvis-approval', row['id'])
                if row['state'] in {'pending', 'allowed', 'denied'}:
                    row['state'] = 'expired'
                    self.studio.put('jarvis-approval', row, row['id'])
            return ApprovalDecision(False, reason='Action approval expired or revoked')
        return approve
