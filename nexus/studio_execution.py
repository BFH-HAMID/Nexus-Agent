"""Explicit execution services: bounded processes, container isolation and HTTPS egress.

Nothing is executed merely by inspecting inventory or editing a workflow.
"""
from __future__ import annotations

import http.client
import ipaddress
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .config import nexus_home


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=15, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def validate_domains(domains):
    if not isinstance(domains, list) or len(domains) > 100:
        raise ValueError('Domains must be an exact-host list (maximum 100)')
    clean = []
    for host in domains:
        if not isinstance(host, str) or host != host.lower() or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', host):
            raise ValueError('Use lowercase exact DNS hosts, no schemes, ports or wildcards')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if '.' not in host or '..' in host:
                raise ValueError('Public DNS host required') from None
        else:
            raise ValueError('IP literals are not allowed')
        clean.append(host)
    return sorted(set(clean))


def fetch_allowed(url, domains):
    parsed = urlsplit(url)
    allowed = validate_domains(domains)
    if parsed.scheme != 'https' or parsed.hostname not in allowed or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('URL must be HTTPS on port 443 at an explicitly allowed exact host')
    answers = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    addresses = sorted({a[4][0] for a in answers})
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError('Private, loopback, link-local, reserved and mixed DNS answers refused')
    # Connect to the inspected IP, preserving TLS SNI/certificate hostname checks.
    # Never follow redirects implicitly or consult environment proxy credentials.
    conn = PinnedHTTPS(parsed.hostname, addresses[0])
    try:
        conn.request('GET', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''), headers={'User-Agent': 'Nexus-Reviewed-Egress/1'})
        response = conn.getresponse()
        deadline = time.monotonic() + 15
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('Egress response exceeded 15-second read deadline')
            if getattr(conn, 'sock', None):
                conn.sock.settimeout(remaining)
            chunk = response.read1(min(8192, 300001 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > 300000:
                raise ValueError('Response exceeds 300 KB')
        data = b''.join(chunks)
        return {'url': url, 'status': response.status, 'text': data.decode('utf-8', errors='replace'),
                'location': response.getheader('Location'), 'redirect_followed': False,
                'source': 'DNS-pinned HTTPS egress; content is untrusted data'}
    finally:
        conn.close()


def docker_available():
    executable = shutil.which('docker')
    if not executable:
        raise ValueError('Docker is required for enforced execution isolation; no host fallback')
    result = subprocess.run([executable, 'info', '--format', '{{.ServerVersion}}'], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError('Docker daemon unavailable; no host fallback')
    return executable


def image_ready(executable, image):
    if not isinstance(image, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:@-]{0,200}', image):
        raise ValueError('Invalid container image')
    result = subprocess.run([executable, 'image', 'inspect', image], capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError('Image not installed. Pull a trusted pinned image explicitly in your terminal first')


class Execution:
    def __init__(self, studio):
        self.studio = studio
        self.processes = {}
        self.lock = threading.RLock()

    def isolated(self, payload):
        settings = self.studio.settings().get('isolation', {})
        executable = docker_available()
        image = settings.get('image', 'python:3.12-slim')
        image_ready(executable, image)
        checkout = self.studio.find('checkout', str(payload.get('checkout_id', '')))
        directory = Path(checkout['path']).resolve()
        if not directory.is_relative_to((nexus_home() / 'worktrees').resolve()) or not directory.is_dir():
            raise ValueError('Only registered detached checkouts can be executed')
        command = payload.get('command')
        if not isinstance(command, str) or not 0 < len(command) <= 10000:
            raise ValueError('Command required (maximum 10 KB)')
        timeout = payload.get('timeout', 120)
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ValueError('Timeout must be 1–300 seconds')
        # No ports, daemon socket, host credentials, host environment or Docker network.
        # No implicit image pull and no host-execution fallback.
        ident = 'nexus-' + uuid.uuid4().hex[:16]
        argv = [executable, 'run', '--pull=never', '--rm', '--name', ident, '--network=none',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--read-only', '--pids-limit=128',
                '--memory=512m', '--cpus=1', '--tmpfs=/tmp:rw,noexec,nosuid,size=64m',
                '--user', f'{os.getuid()}:{os.getgid()}' if hasattr(os, 'getuid') else '65534:65534',
                '--mount', f'type=bind,source={directory},target=/workspace', '--workdir=/workspace',
                '--entrypoint=/bin/sh', image, '-lc', command]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return {'exit_code': result.returncode, 'output': self.studio.redact_text(result.stdout[-75000:] + result.stderr[-75000:]),
                    'checkout_id': checkout['id'], 'image': image, 'network': 'none', 'source': 'actual constrained Docker execution'}
        finally:
            subprocess.run([executable, 'rm', '-f', ident], capture_output=True, timeout=10)

    def start(self, payload):
        if payload.get('confirm') != 'START TRUSTED DEV APP':
            raise ValueError('Type START TRUSTED DEV APP; this executes repository code on the host')
        argv = payload.get('argv')
        port = payload.get('port')
        if not isinstance(argv, list) or not 1 <= len(argv) <= 40 or any(not isinstance(a, str) or not a or len(a) > 2000 or '\x00' in a for a in argv):
            raise ValueError('Provide a bounded command argv array, not a shell string')
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError('Port must be 1024–65535; bind the app to 0.0.0.0 and allow the preview host')
        if self.studio.settings().get('isolation', {}).get('enabled'):
            raise ValueError('Host dev apps disabled under enforced isolation; use isolated execution')
        with self.lock:
            if sum(p['process'].poll() is None for p in self.processes.values()) >= 2:
                raise ValueError('At most two managed dev apps')
            if any(p['port'] == port and p['process'].poll() is None for p in self.processes.values()):
                raise ValueError('Port is already managed')
            ident = uuid.uuid4().hex[:12]
            env = {k: v for k, v in os.environ.items() if k in {'PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'LANG', 'TMPDIR'}}
            env.update(PORT=str(port), HOST='0.0.0.0', BROWSER='none')
            proc = subprocess.Popen(argv, cwd=self.studio.root, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            record = {'id': ident, 'argv': argv, 'port': port, 'started': time.time(), 'process': proc, 'log': bytearray()}
            self.processes[ident] = record
            def reader():
                while data := proc.stdout.read1(1024):
                    with self.lock:
                        record['log'].extend(data)
                        del record['log'][:-150000]
                proc.stdout.close()
            threading.Thread(target=reader, daemon=True, name='studio-dev-log').start()
        return self.list()

    def stop(self, ident):
        with self.lock:
            if ident not in self.processes:
                raise ValueError('Unknown managed process')
            process = self.processes[ident]['process']
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGTERM)
            elif process.poll() is None:
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                if os.name == 'posix':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        return self.list()

    def listeners(self, record):
        try:
            import psutil
            pid = record['process'].pid
            pids = {pid} | {p.pid for p in psutil.Process(pid).children(recursive=True)}
            return sorted({c.laddr.ip for c in psutil.net_connections(kind='inet')
                           if c.pid in pids and c.status == 'LISTEN' and c.laddr.port == record['port']})
        except Exception:
            return None

    def list(self):
        with self.lock:
            return [{k: v for k, v in r.items() if k not in {'process', 'log'}} | {
                'listen_addresses': self.listeners(r), 'exit_code': r['process'].poll(), 'running': r['process'].poll() is None,
                'log': self.studio.redact_text(bytes(r['log']).decode('utf-8', errors='replace')),
                'note': 'Host process; not sandboxed. Preview only works if app binds 0.0.0.0 and accepts the preview origin.'}
                for r in self.processes.values()]
