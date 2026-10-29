"""Constrained GitHub HTTPS transport shared by toolbar and typed tools.

No SSH, ambient credential helpers, URL rewrites, hooks, force, redirects or fetch.
This is a bounded text-project transport, not a general Git client or sandbox.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .github_safety import GitHubSafetyError, github_environment, safe_output, scan_text


def git_environment(workspace):
    env = github_environment(workspace)
    for key in list(env):
        if key.startswith('GIT_') or key.upper() in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                                                    'SSH_AUTH_SOCK', 'SSH_ASKPASS', 'CURL_CA_BUNDLE', 'SSL_CERT_FILE'}:
            env.pop(key, None)
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0',
               GIT_ASKPASS=os.devnull, GCM_INTERACTIVE='Never', GH_HOST='github.com')
    return env


def local_git(workspace, parts, env):
    result = subprocess.run(['git', '--no-pager', '-c', 'core.fsmonitor=false', '-c', 'log.showSignature=false', *parts], cwd=workspace, env=env,
                            capture_output=True, timeout=30)
    if result.returncode or len(result.stdout) > 4_000_000:
        raise GitHubSafetyError('Cannot safely inspect local Git state; output withheld')
    return result.stdout


def validate_branch(branch):
    if (not isinstance(branch, str) or len(branch) > 200 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', branch)
            or '..' in branch or '//' in branch or '@{' in branch or branch.endswith(('/', '.', '.lock'))
            or any(p.startswith('.') or p.endswith('.lock') for p in branch.split('/'))):
        raise GitHubSafetyError('Use a simple, non-option Git branch name')
    return branch


def repository(workspace, env):
    # --no-includes prevents a local include from importing a second configuration.
    result = subprocess.run(['git', 'config', '--no-includes', '--name-only', '--get-regexp',
                             r'^(include.*|url\..*|http\..*|credential\..*|core\.(sshcommand|gitproxy)|remote\..*\.(pushurl|proxy))$'],
                            cwd=workspace, env=env, capture_output=True, timeout=30)
    if result.returncode not in {0, 1} or result.stdout:
        raise GitHubSafetyError('Local Git transport rewrites/includes/credentials/proxy overrides are refused; values withheld')
    top = local_git(workspace, ['rev-parse', '--show-toplevel'], env).decode().strip()
    if Path(top).resolve() != Path(workspace).resolve():
        raise GitHubSafetyError('GitHub transport requires the exact workspace repository root')
    url = local_git(workspace, ['config', '--local', '--no-includes', '--get', 'remote.origin.url'], env).decode().strip()
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?', url)
    if not match or '..' in match[1].split('/'):
        raise GitHubSafetyError('Token-only transport requires a credential-free HTTPS github.com origin; SSH is refused')
    scan_text(match[1], workspace=workspace)
    return match[1]


def scan_outbound(workspace, head, remote_head, env):
    deadline = time.monotonic() + 60
    def budget():
        if time.monotonic() >= deadline:
            raise GitHubSafetyError('Outbound scan time budget exhausted; nothing pushed')
    revision = [head] + (['--not', remote_head] if remote_head else [])
    commits = local_git(workspace, ['rev-list', '--max-count=201', *revision], env).decode().splitlines()
    if len(commits) > 200:
        raise GitHubSafetyError('Outbound history exceeds 200 commits; split/review outside this bounded transport')
    seen, total = set(), 0
    for commit in commits:
        budget()
        message = local_git(workspace, ['show', '-s', '--format=%B', commit], env).decode('utf-8')
        scan_text(message, workspace=workspace)
        for row in local_git(workspace, ['ls-tree', '-r', '-z', commit], env).split(b'\0'):
            if not row:
                continue
            header, raw_name = row.split(b'\t', 1)
            mode, kind, sha = header.split()
            name = raw_name.decode('utf-8')
            from .workbench import safe_path
            safe_path(Path(workspace), name)
            if mode not in {b'100644', b'100755'} or kind != b'blob':
                raise GitHubSafetyError('Outbound symlink/submodule cannot be secret-scanned')
            if sha in seen:
                continue
            seen.add(sha)
            if len(seen) > 1000:
                raise GitHubSafetyError('Outbound history exceeds 1000 unique blobs')
            budget()
            size = int(local_git(workspace, ['cat-file', '-s', sha.decode()], env))
            total += size
            if size > 2_000_000 or total > 20_000_000:
                raise GitHubSafetyError('Outbound blob scan exceeds its size budget')
            try:
                text = local_git(workspace, ['cat-file', 'blob', sha.decode()], env).decode('utf-8')
            except UnicodeError as exc:
                raise GitHubSafetyError('Outbound binary data cannot be scanned by this text-only transport') from exc
            scan_text(text, workspace=workspace)
    return {'commits': len(commits), 'unique_blobs': len(seen)}


def transport(workspace, operation, branch, *, execute=None, cancel=None):
    validate_branch(branch)
    scan_text(branch, workspace=workspace)
    if operation not in {'push', 'ls-remote'}:
        raise GitHubSafetyError('Only explicit push/remote-head lookup is supported')
    if operation == 'push' and branch in {'main', 'master'}:
        raise GitHubSafetyError('Protected branch push refused; use reviewed PR approval')
    def cancelled():
        if cancel is not None and cancel.is_set():
            raise GitHubSafetyError('GitHub transport cancelled before network mutation')
    cancelled()
    env = git_environment(workspace)
    slug = repository(workspace, env)
    url = 'https://github.com/' + slug + '.git'
    if operation == 'push':
        current = local_git(workspace, ['symbolic-ref', '--short', 'HEAD'], env).decode().strip()
        if current != branch:
            raise GitHubSafetyError('Push target must match the current local branch; no implicit branch mapping')
    with tempfile.TemporaryDirectory(prefix='nexus-empty-git-hooks-') as hooks:
        prefix = ['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=' + hooks,
                  '-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential',
                  '-c', 'credential.interactive=false', '-c', 'http.followRedirects=false',
                  '-c', 'http.sslVerify=true', '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always',
                  '-c', 'gc.auto=0', '-c', 'maintenance.auto=false']
        def network(parts):
            cancelled()
            argv = [*prefix, *parts]
            if execute is None:
                result = subprocess.run(argv, cwd=workspace, env=env, capture_output=True, text=True,
                                        timeout=120, stdin=subprocess.DEVNULL)
                output, code = result.stdout, result.returncode
            else:
                output, code = execute(argv, env)
            if code or len(output) > 2_000_000 or '[output capped at ' in output:
                raise GitHubSafetyError('GitHub transport failed or outcome uncertain; inspect GitHub before retrying (output withheld)')
            return safe_output(output, env)
        text = network(['ls-remote', '--heads', url, 'refs/heads/' + branch])
        rows = text.strip().splitlines()
        if len(rows) > 1 or (rows and not re.fullmatch(r'[a-f0-9]{40}\trefs/heads/' + re.escape(branch), rows[0])):
            raise GitHubSafetyError('Unexpected remote-head response; nothing pushed')
        remote_head = rows[0].split('\t')[0] if rows else None
        if operation == 'ls-remote':
            return text
        default = network(['ls-remote', '--symref', url, 'HEAD']).strip().splitlines()
        if default:
            if (len(default) != 2 or not default[0].startswith('ref: refs/heads/') or not default[0].endswith('\tHEAD')
                    or not re.fullmatch(r'[a-f0-9]{40}\tHEAD', default[1])):
                raise GitHubSafetyError('Remote default branch could not be verified; nothing pushed')
            if default[0][16:-5] == branch:
                raise GitHubSafetyError('Default branch push refused; use reviewed PR approval')
        cancelled()
        head = local_git(workspace, ['rev-parse', 'HEAD'], env).decode().strip()
        if not re.fullmatch(r'[a-f0-9]{40}', head):
            raise GitHubSafetyError('Invalid local commit identity')
        scan_outbound(workspace, head, remote_head, env)
        if local_git(workspace, ['rev-parse', 'HEAD'], env).decode().strip() != head:
            raise GitHubSafetyError('Local HEAD advanced during scan; review again')
        # Push immutable reviewed/scanned commit, never a moving ref, without force.
        network(['push', '--porcelain', url, head + ':refs/heads/' + branch])
        return 'Pushed scanned commit ' + head + ' to ' + slug + ':' + branch + ' (non-force HTTPS)'


def typed_transport(workspace, parts, **kwargs):
    if len(parts) == 3 and parts[0:2] == ['push', 'origin']:
        return transport(workspace, 'push', parts[2], **kwargs)
    if len(parts) == 4 and parts[:3] == ['push', '-u', 'origin']:
        # No implicit follow-up mutation of local upstream configuration.
        return transport(workspace, 'push', parts[3], **kwargs)
    if len(parts) == 4 and parts[:3] == ['ls-remote', '--heads', 'origin'] and parts[3].startswith('refs/heads/'):
        return transport(workspace, 'ls-remote', parts[3][11:], **kwargs)
    raise GitHubSafetyError('Use push origin BRANCH or ls-remote --heads origin refs/heads/BRANCH; custom remotes/flags are refused')
