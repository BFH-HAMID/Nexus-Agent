"""Bounded synchronous MCP JSON-RPC transports; zero retries of uncertain calls."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time

import httpx

from ..live_process import terminate_group

LIMIT = 64000
VERSION = '2025-03-26'


def secret(ref):
    if 'env' in ref:
        value = os.environ.get(ref['env'])
    else:
        import keyring
        value = keyring.get_password('nexus-mcp', ref['keychain'])
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or '\r' in value or '\n' in value:
        raise ValueError('Referenced extension secret unavailable/invalid; no inline fallback')
    return value


class Session:
    def __init__(self, root, config, guard):
        self.root, self.cfg, self.guard = root, config, guard
        self.deadline = time.monotonic() + config['timeout_seconds']
        self.process = self.client = None
        self.session_id = None
        self.seq = 0
        self.stop = threading.Event()
        self.closed = threading.Event()
        self.kill_lock = threading.Lock()
        self.killed = False
        self.messages = queue.Queue(maxsize=8)
        self.values = []

    def __enter__(self):
        try:
            self.guard()
            if self.cfg['transport'] == 'http':
                headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': VERSION, 'Accept-Encoding': 'identity'}
                for name, ref in self.cfg['headers'].items():
                    value = secret(ref)
                    self.values.append(value)
                    headers[name] = value
                self.client = httpx.Client(headers=headers, trust_env=False, follow_redirects=False)
            else:
                allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'USERPROFILE', 'HOME', 'TEMP', 'TMP', 'LANG'}
                env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
                for name, ref in self.cfg['env'].items():
                    value = secret(ref)
                    self.values.append(value)
                    env[name] = value
                self.process = subprocess.Popen(self.cfg['command'], cwd=self.root, env=env, stdin=subprocess.PIPE,
                                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=os.name == 'posix')
                def read():
                    try:
                        while not self.stop.is_set():
                            line = self.process.stdout.readline(LIMIT + 1)
                            if not line or len(line) > LIMIT:
                                self.stop.set()
                                break
                            try:
                                self.messages.put_nowait(json.loads(line))
                            except (ValueError, queue.Full):
                                self.stop.set()
                    except Exception:
                        self.stop.set()
                def watchdog():
                    while not self.closed.wait(.05):
                        try:
                            self.remaining()
                        except Exception:
                            self.stop.set()
                            try:
                                self.terminate()
                            except (OSError, subprocess.TimeoutExpired):
                                pass
                            return
                threading.Thread(target=read, daemon=True, name='mcp-bounded-output').start()
                threading.Thread(target=watchdog, daemon=True, name='mcp-deadline').start()
            return self
        except BaseException:
            self.__exit__()
            raise

    def remaining(self):
        self.guard()
        value = self.deadline - time.monotonic()
        if value <= 0 or self.stop.is_set():
            raise ValueError('Extension deadline/lease/output budget exceeded; inspect before retry')
        return value

    def exchange(self, method, params=None, notification=False):
        self.seq += 1
        ident = self.seq
        payload = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if not notification:
            payload['id'] = ident
        encoded = json.dumps(payload, allow_nan=False).encode()
        if len(encoded) > LIMIT:
            raise ValueError('Extension input exceeds budget')
        remaining = self.remaining()
        if self.process:
            self.process.stdin.write(encoded + b'\n')
            self.process.stdin.flush()
            if notification:
                return None
            for _ in range(20):
                while True:
                    try:
                        message = self.messages.get(timeout=min(.1, self.remaining()))
                        break
                    except queue.Empty:
                        continue
                if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                    raise ValueError('Invalid MCP response')
                if 'method' in message:
                    if 'id' in message:
                        # Sampling/elicitation/file access is never delegated back to Nexus.
                        self.process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                            'error': {'code': -32601, 'message': 'Client capability not granted'}}) + '\n').encode())
                        self.process.stdin.flush()
                    continue
                if message.get('id') == ident:
                    return self.result(message)
            raise ValueError('MCP notification/message budget exceeded')
        headers = {'MCP-Session-Id': self.session_id} if self.session_id else {}
        with self.client.stream('POST', self.cfg['url'], content=encoded, headers={**headers, 'Content-Type': 'application/json'}, timeout=remaining) as response:
            if response.status_code not in (200, 202, 204):
                raise ValueError('MCP HTTP refused; no response body logged and no retry')
            session = response.headers.get('mcp-session-id')
            if session:
                if not session.isascii() or len(session) > 200 or any(ord(c) < 33 for c in session):
                    raise ValueError('Invalid MCP session identity')
                self.session_id = session
            if notification:
                return None
            body = bytearray()
            for chunk in response.iter_raw():
                self.remaining()
                if len(body) + len(chunk) > LIMIT:
                    raise ValueError('MCP HTTP output exceeds budget')
                body.extend(chunk)
                if response.headers.get('content-type', '').split(';')[0] == 'text/event-stream':
                    events = bytes(body).decode().replace('\r\n', '\n').split('\n\n')
                    for event in events[:-1][:20]:
                        data = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
                        if data:
                            message = json.loads(data)
                            if isinstance(message, dict) and message.get('jsonrpc') == '2.0' and message.get('id') == ident:
                                return self.result(message)
            if response.headers.get('content-type', '').split(';')[0] == 'text/event-stream':
                for event in bytes(body).decode().split('\n\n')[:20]:
                    data = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
                    if data:
                        message = json.loads(data)
                        if message.get('id') == ident:
                            return self.result(message)
                raise ValueError('Matching SSE MCP response absent')
            message = json.loads(body)
            if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or message.get('id') != ident:
                raise ValueError('Invalid MCP HTTP response identity')
            return self.result(message)

    @staticmethod
    def result(message):
        if 'error' in message or 'result' not in message:
            raise ValueError('MCP server reported failure; untrusted error withheld')
        return message['result']

    def terminate(self):
        with self.kill_lock:
            if not self.killed:
                self.killed = True
                terminate_group(self.process)

    def __exit__(self, *exc):
        self.closed.set()
        self.stop.set()
        if self.process:
            self.terminate()
            for pipe in (self.process.stdin, self.process.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass
        if self.client:
            # No speculative/retried tool calls, including during cleanup.
            self.client.close()
