"""Durable single-instance lifecycle. Starting never grants task/tool approval.
Only operator-enabled local runtime and exact-approved jobs can dispatch.

Control and heartbeat are separate records under the existing Studio store. Never
kill a recorded PID: it may have been reused. Stop/kill are cooperative, and status
reports an unresponsive process rather than inventing a successful termination.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
import uuid

from filelock import FileLock, Timeout

from .queue import Queue


class Daemon:
    def __init__(self, studio):
        self.studio = studio
        self.queue = Queue(studio)
        suffix = hashlib.sha256(str(studio.root.resolve()).encode()).hexdigest()[:32]
        self.control_id = 'jarvis-control-' + suffix
        self.heartbeat_id = 'jarvis-heartbeat-' + suffix

    def _record(self, kind, identifier):
        return next((r for r in self.studio.items(kind) if r['id'] == identifier), {})

    def lock(self):
        path = self.queue.directory() / 'daemon.lock'
        if path.is_symlink():
            raise ValueError('Unsafe daemon lock')
        return FileLock(str(path), timeout=0)

    def _held(self):
        try:
            with self.lock():
                return False
        except Timeout:
            return True

    def status(self):
        with self.queue.locked():
            return self._status()

    def _status(self):
        control = self._record('jarvis-control', self.control_id)
        heartbeat = self._record('jarvis-heartbeat', self.heartbeat_id)
        held = self._held()
        matches = heartbeat.get('instance') == control.get('instance')
        age = time.time() - heartbeat.get('heartbeat', 0)
        fresh = matches and 0 <= age < 10
        status = 'stopped'
        if held:
            status = heartbeat.get('state', 'starting') if fresh else 'starting' if not matches and 0 <= time.time() - control.get('requested', 0) < 15 else 'unresponsive'
        elif matches and heartbeat.get('state') in {'stopped', 'killed'}:
            status = heartbeat['state']
        elif control.get('desired') in {'running', 'paused'}:
            status = 'starting' if time.time() - control.get('requested', 0) < 15 and not matches else 'orphaned'
        try:
            policy = self.queue.policy()
            configured = policy['runtime']['enabled'] and policy['level'] != 'L0'
        except (ValueError, OSError):
            configured = False
        return {'state': status, 'lock_held': held, 'control': control, 'heartbeat': heartbeat,
                'execution_enabled': bool(configured and held and status == 'running'),
                'dispatch_configured': configured, 'note': 'Only explicitly configured loopback/scoped jobs can dispatch. PID is informational.'}

    def request(self, action):
        if action not in {'start', 'stop', 'pause', 'resume', 'kill'}:
            raise ValueError('Unknown daemon control')
        with self.queue.locked():
            policy = self.queue.policy() if action in {'start', 'resume'} else None
            status = self._status()
            control = status['control']
            if action == 'start':
                from ..computer_control import EmergencyStop
                if policy['level'] == 'L0' or EmergencyStop().stopped():
                    raise ValueError('Policy/emergency stop blocks daemon start')
                if status['lock_held'] or status['state'] == 'starting':
                    raise ValueError('Daemon already running or starting')
                control = {'instance': uuid.uuid4().hex, 'desired': 'running', 'requested': time.time()}
            else:
                if action in {'pause', 'resume'} and not status['lock_held']:
                    raise ValueError('Daemon is not running')
                if action == 'resume':
                    from ..computer_control import EmergencyStop
                    if policy['level'] == 'L0' or EmergencyStop().stopped():
                        raise ValueError('Policy/emergency stop blocks resume')
                control.update(desired={'stop': 'stopped', 'kill': 'killed', 'pause': 'paused', 'resume': 'running'}[action],
                               requested=time.time())
            saved = self.studio.put('jarvis-control', control, self.control_id)
            return {'request': action, 'control': saved, 'confirmed': False,
                    'note': 'Durable intent saved; inspect status for worker acknowledgement. No PID was signalled.'}

    def start(self):
        receipt = self.request('start')
        instance = receipt['control']['instance']
        try:
            # -I: no cwd/PYTHONPATH injection; process is explicit and independent of CLI.
            process = subprocess.Popen([sys.executable, '-I', '-m', 'nexus', 'jarvis', '-C', str(self.studio.root),
                                        'run', '--instance', instance], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=os.name == 'posix', close_fds=True,
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
            receipt['launched_pid'] = process.pid
            return receipt
        except OSError:
            self.request('stop')
            raise

    def run(self, instance):
        if not re.fullmatch(r'[a-f0-9]{32}', instance):
            raise ValueError('Invalid daemon instance')
        with self.lock():
            from .runtime import Runtime
            runtime = Runtime(self.studio)
            trigger_thread = None
            with self.queue.locked():
                control = self._record('jarvis-control', self.control_id)
                if control.get('instance') != instance:
                    raise ValueError('Daemon instance no longer owns control')
                for row in self.queue._jobs():
                    if row['status'] == 'running':
                        row.update(status='interrupted', result={'summary': 'Orphaned worker lease revoked; explicit retry/reapproval required'})
                        self.studio.put('jarvis-job', row, row['id'])
            try:
                while True:
                    with self.queue.locked():
                        control = self._record('jarvis-control', self.control_id)
                        if control.get('instance') != instance:
                            raise ValueError('Daemon instance no longer owns control')
                        desired = control.get('desired')
                        from ..computer_control import EmergencyStop
                        policy = self.queue.policy()
                        stopped = EmergencyStop().stopped()
                        state = 'stopped' if desired == 'stopped' else 'killed' if desired == 'killed' or stopped else 'paused' if desired == 'paused' or policy['level'] == 'L0' else 'running'
                        jobs = self.queue._jobs()  # Bounded TTL maintenance, not tool execution.
                        self.studio.put('jarvis-heartbeat', {'instance': instance, 'pid': os.getpid(),
                                        'heartbeat': time.time(), 'state': state, 'execution_enabled': policy['runtime']['enabled'],
                                        'pending': sum(j['status'] == 'awaiting_approval' for j in jobs),
                                        'approved': sum(j['status'] == 'approved' for j in jobs),
                                        'active_job': runtime.active[1]['id'] if runtime.active else None}, self.heartbeat_id)
                    if state == 'running' and (trigger_thread is None or not trigger_thread.is_alive()):
                        import threading

                        from .events import Events
                        def poll_triggers():
                            try:
                                Events(self.queue).poll()
                                from .inbox import Inbox
                                Inbox(self.studio).push()
                            except (ValueError, OSError):
                                pass  # Per-probe errors persist; malformed policy is handled by the main loop.
                        trigger_thread = threading.Thread(target=poll_triggers, daemon=True, name='jarvis-probes')
                        trigger_thread.start()
                    runtime.tick(state == 'running' and policy['runtime']['enabled'])
                    if state in {'stopped', 'killed'}:
                        return 0
                    time.sleep(1)  # Bounded idle CPU; no watchers/model polling by default.
            except BaseException as exc:
                with self.queue.locked():
                    self.studio.put('jarvis-heartbeat', {'instance': instance, 'pid': os.getpid(),
                                    'heartbeat': time.time(), 'state': 'interrupted',
                                    'error_type': type(exc).__name__, 'execution_enabled': False}, self.heartbeat_id)
                raise
            finally:
                runtime.close()
