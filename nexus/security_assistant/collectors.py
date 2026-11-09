"""Bounded passive host collectors. No subprocesses, probes, downloads or imports of project code."""
from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path

from .store import path_safe, sha


class Unavailable(ValueError):
    """A coverage gap, never a clean bill of health."""


def finding(kind, subject, severity, summary, evidence, *, suggestion=None, alert=True):
    return {'kind': kind, 'subject': subject, 'severity': severity, 'summary': summary, 'evidence': evidence,
            'suggestion': suggestion or 'Review the evidence and verify the change with its owner; no response was executed.',
            'alert': alert}


def address(value):
    try:
        if not isinstance(value, str) or '%' in value:
            raise ValueError
        result = ipaddress.ip_address(value)
        if (result.is_unspecified or result.is_multicast or str(result) == '255.255.255.255'
                or getattr(result, 'ipv4_mapped', None)):
            raise ValueError
        return str(result)
    except ValueError:
        raise ValueError('A single unambiguous unicast IP literal is required') from None


def account_rows(data):
    rows = {}
    for line in data.decode('utf-8').splitlines():
        fields = line.split(':')
        if len(fields) != 7 or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]{0,63}', fields[0]):
            raise Unavailable('Unsupported passwd record')
        if not fields[2].isdigit() or not fields[3].isdigit() or len(fields[2]) > 10 or len(fields[3]) > 10:
            raise Unavailable('Invalid account ID')
        shell = fields[6]
        if shell and (not shell.startswith('/') or not re.fullmatch(r'/[A-Za-z0-9_./-]{1,240}', shell)):
            raise Unavailable('Unsupported account shell')
        if fields[0] in rows:
            raise Unavailable('Duplicate account name in passwd input')
        rows[fields[0]] = {'uid': int(fields[2]), 'gid': int(fields[3]), 'shell': shell}
        if len(rows) > 4096:
            raise Unavailable('Account count budget exceeded')
    return rows


def observed_time(line, now):
    """ISO timestamps or traditional host-local syslog time; otherwise arrival time."""
    iso = re.match(r'^(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:\d\d)?)', line)
    try:
        if iso:
            stamp = datetime.fromisoformat(iso[1].replace(',', '.').replace('Z', '+00:00')).timestamp()
            return stamp, 'log_time'
        old = re.match(r'^([A-Z][a-z]{2}\s+\d{1,2}\s+\d\d:\d\d:\d\d)', line)
        if old:
            current = datetime.fromtimestamp(now)
            parsed = datetime.strptime(f'{current.year} {old[1]}', '%Y %b %d %H:%M:%S')
            if parsed.timestamp() > now + 86400:
                parsed = parsed.replace(year=current.year - 1)
            return parsed.timestamp(), 'syslog_host_local_time'
    except (ValueError, OverflowError):
        pass
    return now, 'arrival_time_no_parseable_log_timestamp'


class Collector:
    def __init__(self, workspace, cfg, now):
        self.workspace = Path(workspace).resolve()
        self.cfg = cfg
        self.now = now
        self.remaining = cfg['max_tick_bytes']
        self.findings = []
        self.coverage = []

    def active(self):
        from ..computer_control import EmergencyStop
        from . import policy
        if (policy.digest(policy.require(self.workspace, 'monitoring_enabled')) != policy.digest(self.cfg)
                or EmergencyStop().stopped()):
            raise Unavailable('Security monitor authority changed or emergency stop latched')

    def path(self, value):
        self.active()
        path = Path(value).expanduser()
        path = path if path.is_absolute() else self.workspace / path
        path_safe(path)
        return path

    def read(self, value):
        path = self.path(value)
        limit = min(self.cfg['max_read_bytes'], self.remaining)
        if limit <= 0:
            raise Unavailable('Tick byte budget exhausted')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                raise Unavailable('Input requires a bounded single-link regular file')
            raw = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
        self.remaining -= len(raw)
        if len(raw) > limit or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise Unavailable('Input changed during collection or exceeded budget')
        return raw, before

    def tail(self, value, previous):
        """Complete lines only; inode plus anchor catches rotation/copytruncate/regrowth."""
        path = self.path(value)
        limit = min(self.cfg['max_read_bytes'], self.remaining)
        if limit < 4096:
            raise Unavailable('Tick byte budget exhausted')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise Unavailable('Auth/event logs must be single-link regular files')
            identity = [before.st_dev, before.st_ino]
            cursor = previous.get('cursor', {})
            offset = cursor.get('offset', 0)
            same = cursor.get('identity') == identity and 0 <= offset <= before.st_size
            if same and offset:
                handle.seek(max(0, offset - 128))
                same = sha(handle.read(min(128, offset))) == cursor.get('anchor')
            offset = offset if same else max(0, before.st_size - limit)
            gap = bool(not same and offset > 0)
            handle.seek(offset)
            raw = handle.read(min(limit, before.st_size - offset))
            self.remaining -= len(raw)
            end = raw.rfind(b'\n') + 1
            if end == 0 and len(raw) >= limit:
                raise Unavailable('Overlong log line; no cursor advanced')
            complete = raw[:end]
            if gap:
                complete = complete.partition(b'\n')[2]
            offset += end
            handle.seek(max(0, offset - 128))
            anchor = sha(handle.read(min(128, offset)))
            after = os.fstat(handle.fileno())
            if after.st_size < before.st_size:
                raise Unavailable('Log truncated during collection; retry before advancing cursor')
        return complete.decode('utf-8', errors='replace'), {'identity': identity, 'offset': offset, 'anchor': anchor}, gap

    def log(self, kind, path, prior):
        text, cursor, gap = self.tail(path, prior)
        state = copy.deepcopy(prior)
        state['cursor'] = cursor
        groups = {}
        buckets = {ip: {k: n for k, n in counts.items() if self.now - self.cfg['ssh_window_seconds'] <= int(k) <= self.now}
                   for ip, counts in state.get('attempts', {}).items()}
        buckets = {k: v for k, v in buckets.items() if v}
        for line in text.splitlines():
            if len(line) > 8192:
                raise Unavailable('Log line exceeds parser budget')
            ip = None
            event = ''
            if kind == 'auth_logs' and re.search(r'\bsshd(?:\[\d+\])?(?::|\s)', line):
                match = re.search(r'\bFailed (?:password|publickey|keyboard-interactive(?:/pam)?) for .{1,200}? from ([0-9a-fA-F:.]+)\b', line)
                if match:
                    ip, event = match[1], 'ssh_bruteforce'
            elif kind == 'fail2ban_logs':
                match = re.search(r'\b(Ban|Unban)\s+([0-9a-fA-F:.]+)(?:\s|$)', line)
                if match:
                    event, ip = 'fail2ban_' + match[1].lower(), match[2]
            elif kind == 'ufw_logs' and '[UFW BLOCK]' in line:
                match = re.search(r'\bSRC=([0-9a-fA-F:.]+)(?:\s|$)', line)
                if match:
                    ip, event = match[1], 'ufw_block'
            if not ip:
                continue
            try:
                ip = address(ip)
            except ValueError:
                continue
            stamp, time_source = observed_time(line, self.now)
            if not self.now - self.cfg['ssh_window_seconds'] <= stamp <= self.now + 5:
                continue  # Old/future logs do not trigger a fresh brute-force window.
            item = groups.setdefault((event, ip), {'count': 0, 'line_sha256': [], 'time_source': time_source})
            item['count'] += 1
            if len(item['line_sha256']) < 3:
                item['line_sha256'].append(sha(line.encode()))
            if len(groups) > 128:
                raise Unavailable('Log address-group budget exceeded')
            if event == 'ssh_bruteforce':
                bucket = str(int(min(stamp, self.now)) // 10 * 10)
                counts = buckets.setdefault(ip, {})
                counts[bucket] = min(10000, counts.get(bucket, 0) + 1)
                if len(buckets) > 128:
                    raise Unavailable('SSH rolling-address budget exceeded')
        state['attempts'] = buckets if kind == 'auth_logs' else {}
        output = []
        for (event, ip), item in sorted(groups.items()):
            count = sum(buckets[ip].values()) if event == 'ssh_bruteforce' else item['count']
            if event == 'ssh_bruteforce' and count < self.cfg['ssh_threshold']:
                continue
            summary = (f'{count} SSH authentication failures from {ip} within the configured window.' if event == 'ssh_bruteforce'
                       else f'{event.replace("_", " ")} observed for {ip} ({count} events).')
            output.append(finding(event, path + ':' + ip, 'high' if event == 'ssh_bruteforce' else 'info', summary,
                {'source': path, 'ip': ip, 'window_count': count, 'cursor': cursor, **item},
                suggestion=('Review an IP-block proposal and its rollback? Fresh approval is required; nothing will be blocked automatically.'
                            if event == 'ssh_bruteforce' else 'Verify firewall/fail2ban policy and the source; this may be normal defensive activity.')))
        return state, output, 'bounded_tail_gap' if gap else 'ok'

    def files(self, path, directory=False):
        if not directory:
            data, info = self.read(path)
            return {'file': {'sha256': sha(data), 'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid}}
        root = self.path(path)
        if not root.is_dir():
            raise Unavailable('Selected unit/cron path is not a directory')
        result = {}
        with os.scandir(root) as iterator:
            for index, item in enumerate(iterator):
                if index >= 256 or len(item.name) > 200 or any(ord(c) < 32 for c in item.name):
                    raise Unavailable('Directory entry budget or filename validation failed')
                info = item.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    # Track enablement links, NEVER follow their targets or read outside this directory.
                    row = {'symlink_target_sha256': sha(os.readlink(item.path).encode())}
                elif stat.S_ISDIR(info.st_mode):
                    row = {'directory': True}  # Deliberately non-recursive; select child directories explicitly.
                else:
                    data, info = self.read(item.path)
                    row = {'sha256': sha(data)}
                result[item.name] = {**row, 'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid}
        return result

    def ports(self):
        import psutil
        self.active()
        rows = {}
        try:
            connections = psutil.net_connections(kind='inet')
        except psutil.Error:
            raise Unavailable('Socket table unavailable with the current OS permissions') from None
        for index, conn in enumerate(connections):
            if index >= 8192:
                raise Unavailable('Socket table budget exceeded')
            if conn.status != psutil.CONN_LISTEN or not conn.laddr:
                continue
            host, port = conn.laddr.ip, conn.laddr.port
            key = f'tcp:{host}:{port}'
            rows[key] = {'protocol': 'tcp', 'address': host, 'port': port}
            if len(rows) > 1024:
                raise Unavailable('Listener budget exceeded')
        return rows

    def inventory(self, path):
        raw, _ = self.read(path)
        result = []
        for paragraph in raw.decode('utf-8').split('\n\n'):
            fields = dict(re.findall(r'^(Package|Version|Status): ([^\r\n]+)$', paragraph, re.M))
            if paragraph.strip() and not {'Package', 'Status'} <= fields.keys():
                raise Unavailable('Incomplete or unsupported dpkg status record')
            if fields.get('Status') == 'install ok installed':
                result.append(self.package('deb', fields.get('Package'), fields.get('Version')))
                if len(result) > 10000:
                    raise Unavailable('Package inventory budget exceeded')
        return result

    @staticmethod
    def package(ecosystem, name, version):
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+_.-]{0,160}', name)
                or not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+_.:~!-]{0,160}', version)):
            raise Unavailable('Unsupported package name/version')
        return {'ecosystem': ecosystem, 'package': re.sub(r'[-_.]+', '-', name.lower()) if ecosystem == 'pypi' else name,
                'version': version}

    def python_inventory(self):
        from importlib.metadata import distributions
        self.active()
        rows = []
        for index, dist in enumerate(distributions()):
            if index >= 10000:
                raise Unavailable('Python package inventory budget exceeded')
            # Avoid Distribution.metadata/read_text: those can read an unbounded
            # body (or custom/zip backend). Only standard local metadata files.
            location = getattr(dist, '_path', None)
            if not isinstance(location, Path) or location.suffix not in {'.dist-info', '.egg-info'}:
                raise Unavailable('Only ordinary local dist-info/egg-info package metadata is supported')
            location = location.absolute()
            file = location if location.is_file() else location / ('METADATA' if location.suffix == '.dist-info' else 'PKG-INFO')
            raw, _ = self.read(str(file))
            headers = raw.split(b'\n\n', 1)[0].decode('utf-8')
            names = re.findall(r'^Name: ([^\r\n]+)\r?$', headers, re.M)
            versions = re.findall(r'^Version: ([^\r\n]+)\r?$', headers, re.M)
            if len(names) != 1 or len(versions) != 1:
                raise Unavailable('Python package metadata requires one literal Name/Version pair')
            rows.append(self.package('pypi', names[0], versions[0]))
        return rows

    def cves(self, path, inventory):
        data, _ = self.read(path)
        try:
            feed = json.loads(data)
        except (ValueError, RecursionError):
            raise Unavailable('Malformed offline CVE snapshot') from None
        if (not isinstance(feed, dict) or set(feed) != {'schema', 'generated_at', 'advisories'}
                or type(feed['schema']) is not int or feed['schema'] != 1
                or not isinstance(feed['advisories'], list) or len(feed['advisories']) > 10000):
            raise Unavailable('Unsupported offline CVE snapshot schema')
        try:
            generated = datetime.fromisoformat(feed['generated_at'].replace('Z', '+00:00'))
            if generated.tzinfo is None or generated.timestamp() > self.now + 300:
                raise ValueError
            stale = self.now - generated.timestamp() > self.cfg['cve_max_age_days'] * 86400
        except (TypeError, ValueError, AttributeError, OverflowError):
            raise Unavailable('CVE snapshot needs a valid timezone-aware generation timestamp') from None
        installed = {(r['ecosystem'], r['package'], r['version']) for r in inventory}
        output = []
        feed_digest = sha(data)
        for advisory in feed['advisories']:
            if (not isinstance(advisory, dict) or set(advisory) != {'id', 'ecosystem', 'package', 'affected_versions', 'severity', 'fixed_version'}
                    or not isinstance(advisory['id'], str) or not re.fullmatch(r'CVE-\d{4}-\d{4,9}', advisory['id'])
                    or advisory['ecosystem'] not in ('deb', 'pypi') or advisory['severity'] not in ('low', 'medium', 'high', 'critical')
                    or not isinstance(advisory['affected_versions'], list) or not 1 <= len(advisory['affected_versions']) <= 128):
                raise Unavailable('Unsupported CVE advisory; exact affected versions required')
            fixed = advisory['fixed_version']
            if not isinstance(fixed, str):
                raise Unavailable('CVE fixed_version must be a literal version or empty string')
            self.package(advisory['ecosystem'], advisory['package'], fixed or 'unknown')
            for version in advisory['affected_versions']:
                item = self.package(advisory['ecosystem'], advisory['package'], version)
                if (item['ecosystem'], item['package'], item['version']) not in installed:
                    continue
                if len(output) + len(self.findings) >= 512:
                    raise Unavailable('CVE finding budget exceeded; snapshot not fully evaluated')
                evidence = {**item, 'cve': advisory['id'], 'fixed_version': fixed, 'feed': path,
                            'feed_sha256': feed_digest, 'generated_at': generated.isoformat(), 'feed_stale': stale}
                output.append(finding('package_cve', ':'.join((item['ecosystem'], item['package'], version, advisory['id'])),
                    advisory['severity'], f"{advisory['id']} matches installed {item['package']} {version} in the supplied feed.", evidence,
                    suggestion='Verify the distributor advisory/backports and plan a reviewed upgrade; no package changes were executed.'))
        if stale:
            output.append(finding('cve_feed_stale', path, 'medium', 'Offline CVE snapshot is stale; coverage is incomplete.',
                                  {'feed': path, 'sha256': sha(data), 'generated_at': generated.isoformat()}))
        return output

    def certificate(self, path):
        try:
            from cryptography import x509
        except ImportError:
            raise Unavailable('Certificate parsing needs the optional nexus-agent[security] extra') from None
        raw, _ = self.read(path)
        if b'PRIVATE KEY' in raw:
            raise Unavailable('Supply a public certificate file, never a private-key bundle')
        try:
            cert = x509.load_pem_x509_certificate(raw) if b'BEGIN CERTIFICATE' in raw else x509.load_der_x509_certificate(raw)
            expiry = cert.not_valid_after_utc
        except (ValueError, AttributeError):
            raise Unavailable('Public certificate parsing failed; use cryptography>=44') from None
        days = (expiry.timestamp() - self.now) / 86400
        if days > self.cfg['certificate_days']:
            return []
        severity = 'critical' if days <= 0 else 'high' if days <= 7 else 'medium'
        return [finding('certificate_expiry', path, severity,
                        'Public certificate expired.' if days <= 0 else 'Public certificate is approaching expiry.',
                        {'source': path, 'sha256': sha(raw), 'not_after': expiry.isoformat()},
                        suggestion='Review certificate renewal and service reload; no certificate or key was changed.')]

    def snapshot(self, kind, path, values, prior):
        from .store import clean
        values = clean(self.workspace, values)
        source = kind + ':' + path
        if 'snapshot' not in prior:
            self.findings.append(finding('baseline', source, 'info', 'Initial local baseline established (not a security clearance).',
                                         {'source': source, 'items': len(values), 'sha256': sha(values)}, alert=False))
        else:
            old = prior['snapshot']
            for item in sorted(set(values) | set(old)):
                if old.get(item) == values.get(item):
                    continue
                if kind in ('users_files', 'listening_ports') and item not in values:
                    continue
                change = 'added' if item not in old else 'removed' if item not in values else 'modified'
                severity = 'critical' if kind == 'users_files' and values.get(item, {}).get('uid') == 0 else 'high'
                self.findings.append(finding(kind + '_' + change, source + ':' + item, severity,
                    f'{kind.replace("_", " ")} {change}: {item}',
                    {'source': path, 'item': item, 'before': old.get(item), 'after': values.get(item)},
                    suggestion=('Review an account-disable proposal and its rollback? It never executes automatically.'
                                if kind == 'users_files' else None)))
        return {'snapshot': values}

    def collect(self, previous):
        sources = {}
        monitors = self.cfg['monitors']
        inventory = []
        for kind, configured in monitors.items():
            values = ['local'] if configured is True else configured if isinstance(configured, list) else []
            if kind == 'cve_feeds':
                continue
            for path in values:
                key = sha([kind, path])
                prior = previous.get(key, {})
                sources[key] = copy.deepcopy(prior)
                try:
                    if kind.endswith('_logs'):
                        source, events, coverage = self.log(kind, path, prior)
                        sources[key] = source
                        self.findings.extend(events)
                    elif kind == 'certificate_files':
                        self.findings.extend(self.certificate(path))
                        coverage = 'ok'
                    elif kind == 'dpkg_files' or kind == 'python_packages':
                        inventory.extend(self.inventory(path) if kind == 'dpkg_files' else self.python_inventory())
                        coverage = 'ok'
                    else:
                        if kind == 'listening_ports':
                            current = self.ports()
                        elif kind == 'users_files':
                            current = account_rows(self.read(path)[0])
                        else:
                            try:
                                current = self.files(path, directory=kind.endswith('_dirs'))
                            except FileNotFoundError:
                                # A child disappearing mid-read is a coverage gap,
                                # not proof that every entry in the directory vanished.
                                if 'snapshot' not in prior or self.path(path).exists():
                                    raise
                                current = {}
                        sources[key] = self.snapshot(kind, path, current, prior)
                        coverage = 'ok'
                    self.coverage.append({'monitor': kind, 'source': path, 'state': coverage})
                except (ValueError, OSError, UnicodeError, ImportError) as exc:
                    self.coverage.append({'monitor': kind, 'source': path, 'state': 'unavailable',
                        'reason': str(exc) if type(exc) is Unavailable else type(exc).__name__})
        for path in monitors['cve_feeds']:
            try:
                self.findings.extend(self.cves(path, inventory))
                self.coverage.append({'monitor': 'cve_feeds', 'source': path, 'state': 'ok'})
            except (ValueError, OSError, UnicodeError, TypeError, RecursionError) as exc:
                self.coverage.append({'monitor': 'cve_feeds', 'source': path, 'state': 'unavailable',
                    'reason': str(exc) if type(exc) is Unavailable else type(exc).__name__})
        if (monitors['dpkg_files'] or monitors['python_packages']) and not monitors['cve_feeds']:
            self.coverage.append({'monitor': 'cve_feeds', 'source': 'none', 'state': 'unavailable',
                                  'reason': 'No offline advisory snapshot configured; CVE coverage unavailable'})
        if monitors['cve_feeds'] and not (monitors['dpkg_files'] or monitors['python_packages']):
            self.coverage.append({'monitor': 'package_inventory', 'source': 'none', 'state': 'unavailable',
                                  'reason': 'No installed-package inventory configured; CVE coverage unavailable'})
        if len(self.findings) > 512:
            raise Unavailable('Finding budget exceeded; cursor/baseline not advanced')
        return sources, self.findings, self.coverage
