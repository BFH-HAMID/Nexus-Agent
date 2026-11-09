"""One supported active test: a reviewed, IP-literal bounded TCP connect check.

Opaque executors are unavailable while defensive mode is enabled. This is tool
surface confinement, not an OS/network sandbox for other processes or sessions.
"""
from __future__ import annotations

import errno
import ipaddress
import json
import re
import socket
import time
from pathlib import Path

from . import policy
from .collectors import address
from .store import sha

PASSIVE_TOOLS = frozenset({'list_files', 'read_file', 'grep', 'skill_view', 'recall', 'finish', 'request_clarification',
    'security_status', 'security_monitor', 'security_report', 'security_response', 'security_scan', 'security_analyze'})
SCANNER = re.compile(r'(?<![a-z0-9])(?:nmap|masscan|zmap|rustscan|naabu|nuclei|nikto|sqlmap|hydra|medusa|msfconsole|msfvenom|metasploit|nessus|openvas|hping3|netcat|ncat)(?![a-z0-9])', re.I)


def refusal(root, name, args, reason):
    from .. import audit
    # Unknown strings/command bodies may be credentials or hostile terminal text.
    # Only a digest goes into the shared hash-linked audit trail.
    try:
        audit.record(root, 'security.scope_refused', target=str(name)[:80], result='refused', risk='danger',
                     detail={'request_sha256': sha(json.dumps(args, sort_keys=True, default=str).encode()), 'reason': reason})
    except (ValueError, OSError, TimeoutError):
        pass  # A failed log never enables a refused test.


def tool_guard(root, spec, args):
    """Called before AND after approval, also for raw MCP and legacy entry points."""
    root = Path(root)
    name = spec.name
    if name in PASSIVE_TOOLS:
        return None  # Typed tools independently enforce their exact scope/feature/lease.
    opaque = name in {'run_command', 'sandbox_command', 'git', 'gh', 'project_action'} or name.startswith('mcp_')
    reason = None
    if SCANNER.search(name) or opaque and SCANNER.search(json.dumps(args, default=str)[:64000]):
        reason = 'Raw scanners/exploit runners are refused. Use security_scan with operator-owned security.yaml scope; no exploit execution.'
    path = root / 'security.yaml'
    if reason is None and (path.exists() or path.is_symlink()):
        try:
            cfg = policy.load(root)
            if cfg['enabled']:
                reason = 'Defensive mode refuses opaque execution/mutation tools (including shell, Git, MCP and desktop/browser relays). Use the scoped defensive tools.'
        except (ValueError, OSError, TypeError, RecursionError):
            reason = 'Invalid security.yaml; execution refused until operator review.'
    if reason:
        refusal(root, name, args, reason)
    return reason


def allowed(root, target, selected_ports):
    try:
        cfg = policy.require(root, 'scan_enabled')
        ip = ipaddress.ip_address(address(target))
        if not any(ip.version == net.version and ip in net
                   and (net.num_addresses <= 2 or ip not in (net.network_address, net.broadcast_address))
                   for net in (ipaddress.ip_network(value) for value in cfg['targets'])):
            raise ValueError('Active test target is outside the explicit security.yaml allowlist')
        chosen = policy.ports(selected_ports if selected_ports is not None else cfg['scan_ports'])
        if not set(chosen) <= set(cfg['scan_ports']):
            raise ValueError('Active test ports are outside the explicit security.yaml allowlist')
    except (ValueError, OSError, TypeError, RecursionError):
        refusal(root, 'security_scan', {'target': target, 'ports': selected_ports}, 'target/port/capability outside operator scope')
        raise ValueError('Active test refused: enable scan_enabled and explicitly allowlist the single IP target and every port in security.yaml') from None
    return cfg, str(ip), chosen


def connect(target, port, timeout):
    family = socket.AF_INET6 if ':' in target else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as handle:
        handle.settimeout(timeout)
        try:
            code = handle.connect_ex((target, port))
        except (OSError, TimeoutError):
            return 'timeout_or_unavailable'
    return 'open' if code == 0 else 'closed' if code == errno.ECONNREFUSED else 'timeout_or_unavailable'


def scan(root, args, cfg, *, cancelled=lambda: False):
    """No DNS, shell, banner read, UDP, raw socket, script, payload or retry."""
    from .. import audit
    from ..computer_control import EmergencyStop
    target, selected = args['target'], args['ports']
    audit.record(root, 'security.scan', target=target, result='attempt', risk='danger',
                 detail={'ports': selected, 'policy_sha256': policy.digest(cfg), 'method': 'tcp_connect_only'})
    started = time.monotonic()
    results = []
    interrupted = False
    for port in selected:
        try:
            current, actual, ports = allowed(root, target, selected)
        except ValueError:
            interrupted = True
            break
        if (policy.digest(current) != policy.digest(cfg) or actual != target or ports != selected
                or cancelled() or EmergencyStop().stopped() or time.monotonic() - started > 12):
            interrupted = True
            break
        results.append({'port': port, 'state': connect(target, port, .3)})
    return {'target': target, 'ports': results, 'complete': not interrupted and len(results) == len(selected),
            'method': 'TCP connect only; no exploit, service discovery or vulnerability assertion'}
