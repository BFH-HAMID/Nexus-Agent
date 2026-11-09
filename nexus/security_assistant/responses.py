"""DANGEROUS response proposals with exact rollback text. Deliberately NO executor."""
from __future__ import annotations

import json
import re
import shlex
import stat
import time
from datetime import date, datetime, timezone
from pathlib import Path

from ..jarvis.sources import bounded
from . import policy
from .collectors import account_rows, address
from .service import SecurityService
from .store import path_safe, sha


def public_file(root, value):
    path = Path(value).expanduser()
    path = path if path.is_absolute() else Path(root) / path
    path_safe(path)
    before = path.stat()
    data = bounded(path, 64_000)
    after = path.stat()
    keys = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_mode', 'st_uid', 'st_gid')
    if any(getattr(before, k) != getattr(after, k) for k in keys):
        raise ValueError('Response preimage changed during review preparation')
    return path, data, {k: getattr(after, k) for k in keys} | {'sha256': sha(data)}


def authorized_keys(data):
    """Only public SSH keys, no private material or authorized_keys command/options."""
    if b'PRIVATE KEY' in data:
        raise ValueError('Key rotation proposals accept PUBLIC authorized_keys files only')
    lines = [line for line in data.decode('ascii').splitlines() if line.strip() and not line.startswith('#')]
    if not 1 <= len(lines) <= 32:
        raise ValueError('Provide 1–32 public SSH keys in the staged authorized_keys file')
    for line in lines:
        match = re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) ([A-Za-z0-9+/=]{20,8192})(?: [^\r\n]{0,200})?', line)
        if not match:
            raise ValueError('Unsupported public key record/options; review manually')
        try:
            from cryptography.hazmat.primitives.serialization import load_ssh_public_key
        except ImportError:
            raise ValueError('Public SSH key validation needs the optional nexus-agent[security] extra') from None
        try:
            load_ssh_public_key((match[1] + ' ' + match[2]).encode('ascii'))
        except (ValueError, TypeError, UnicodeError):
            raise ValueError('Malformed public SSH key record') from None


def plan(workspace, args, nonce):
    cfg = policy.require(workspace, 'responses_enabled')
    if not isinstance(args, dict) or set(args) - {'action', 'incident', 'backend', 'account', 'key'}:
        raise ValueError('Unknown response fields; no raw commands accepted')
    action = args.get('action')
    required = {'block_ip': {'action', 'incident', 'backend'}, 'disable_account': {'action', 'incident', 'account'},
                'rotate_key': {'action', 'incident', 'key'}}
    if action not in required or set(args) != required[action]:
        raise ValueError('Use an exact block_ip, disable_account or rotate_key proposal')
    if not isinstance(args['incident'], str) or not re.fullmatch(r'[a-f0-9]{24}', args['incident']):
        raise ValueError('A recorded incident ID is required')
    incident, artifact = SecurityService(workspace).incident(args['incident'])
    observed = artifact['evidence']
    result = {'action': action, 'incident': incident['id'], 'incident_evidence': incident['evidence'],
              'risk_class': 'DANGEROUS', 'proposal_only': True, 'executes_host_commands': False,
              'approval_scope': 'Review/record this proposal only. Approval NEVER authorizes command execution.',
              'preconditions': ['Use an out-of-band console; verify target ownership, current state and a recovery path.',
                                'Operator must independently review every later command, including rollback.']}
    if action == 'block_ip':
        if incident['kind'] not in {'ssh_bruteforce', 'ufw_block', 'fail2ban_ban'} or args['backend'] not in {'ufw', 'iptables'}:
            raise ValueError('IP blocking requires an observed SSH/firewall IP and a supported local firewall backend')
        ip = address(observed.get('ip'))
        comment = 'nexus-' + nonce[:12]
        if args['backend'] == 'ufw':
            rule = f'deny from {ip} to any comment {comment}'
            command, rollback = 'sudo ufw insert 1 ' + rule, 'sudo ufw delete ' + rule
        else:
            binary = 'ip6tables' if ':' in ip else 'iptables'
            rule = f'-s {ip} -m comment --comment {comment} -j DROP'
            command, rollback = f'sudo {binary} -I INPUT 1 {rule}', f'sudo {binary} -D INPUT {rule}'
        result.update(target=ip, command=command, rollback=rollback,
            limitations='A proposed host INPUT rule only. No flushing, remote firewall update or rule-persistence changes. Verify backend syntax and existing rules before applying.')
    elif action == 'disable_account':
        user = args['account']
        if (incident['kind'] not in {'users_files_added', 'users_files_modified'} or user != observed.get('item')
                or user not in cfg['response_accounts'] or observed.get('source') not in cfg['monitors']['users_files']):
            raise ValueError('Account must match observed evidence AND the operator response_accounts allowlist')
        path, raw, preimage = public_file(workspace, observed['source'])
        current = account_rows(raw).get(user)
        if (not current or current['uid'] == 0
                or user not in cfg['account_state_files'] and current['shell'] in {'/usr/sbin/nologin', '/sbin/nologin', '/bin/false'}):
            raise ValueError('Account absent, privileged, or already has a disabled interactive shell')
        result.update(target=user, preimages={str(path): preimage},
            command=f'sudo usermod --shell /usr/sbin/nologin -- {shlex.quote(user)}',
            rollback=f'sudo usermod --shell {shlex.quote(current["shell"])} -- {shlex.quote(user)}',
            limitations='Disables INTERACTIVE SHELL LOGIN only, not running processes, tokens, non-shell SSH forwarding or service sessions. Broader account-login expiry requires a separately verified operator expiry snapshot and manual admin review; no shadow/password data is read.')
        if user in cfg['account_state_files']:
            snapshot, data, metadata = public_file(workspace, cfg['account_state_files'][user])
            try:
                account = json.loads(data)
                if (not isinstance(account, dict) or set(account) != {'schema', 'account', 'uid', 'expires', 'observed_at'}
                        or type(account['schema']) is not int or account['schema'] != 1
                        or account['account'] != user or type(account['uid']) is not int or account['uid'] != current['uid']
                        or not isinstance(account['expires'], str)):
                    raise ValueError
                seen = datetime.fromisoformat(account['observed_at'].replace('Z', '+00:00'))
                if seen.tzinfo is None or not -5 <= time.time() - seen.timestamp() <= 600:
                    raise ValueError
                previous = account['expires']
                if previous and (date.fromisoformat(previous).isoformat() != previous
                                 or date.fromisoformat(previous) <= datetime.fromtimestamp(time.time(), timezone.utc).date()):
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                raise ValueError('Account expiry proposal needs a fresh (<10 minute) operator snapshot of a currently non-expired account') from None
            result['preimages'][str(snapshot)] = metadata
            result.update(command=f'sudo usermod --expiredate 1 -- {shlex.quote(user)}',
                rollback=f'sudo usermod --expiredate {shlex.quote(previous)} -- {shlex.quote(user)}',
                account_state_source='Operator-provided non-secret expiry snapshot; not an independent host attestation.',
                limitations='Expires account login through services that honor account expiry. Does not kill existing sessions or revoke application/API tokens. Verify PAM/service behavior and current expiry before applying; no shadow or password data is read.')
    else:
        alias = args['key']
        if not isinstance(alias, str) or alias not in cfg['rotation_paths']:
            raise ValueError('Select an operator-owned rotation_paths alias')
        paths = cfg['rotation_paths'][alias]
        current, old, preimage = public_file(workspace, paths['current'])
        staged, replacement, new_image = public_file(workspace, paths['replacement'])
        if current.name != 'authorized_keys' or current == staged or old == replacement:
            raise ValueError('Rotation supports a changed, explicitly staged PUBLIC SSH authorized_keys file')
        authorized_keys(old)
        authorized_keys(replacement)
        backup = current.with_name(current.name + '.nexus-' + nonce[:12] + '.bak')
        if backup.exists() or backup.is_symlink():
            raise ValueError('Key rotation backup destination already exists')
        mode = stat.S_IMODE(preimage['st_mode'])
        if mode & 0o077 or mode & 0o7000:
            raise ValueError('Current authorized_keys must have private ordinary permissions')
        c, s, b = (shlex.quote(str(p)) for p in (current, staged, backup))
        result.update(target=alias, preimages={str(current): preimage, str(staged): new_image},
            command=f'sudo cp --preserve=all --no-clobber -- {c} {b} && sudo install -m {mode:04o} -o {preimage["st_uid"]} -g {preimage["st_gid"]} -- {s} {c}',
            rollback=f'sudo cp --preserve=all -- {b} {c}',
            limitations='PUBLIC SSH authorized_keys replacement proposal, not private-key generation/export. Stage the new public key and verify alternate console access yourself. Linux GNU cp/install syntax; metadata/ACL/native acceptance is not guaranteed.')
    return result
