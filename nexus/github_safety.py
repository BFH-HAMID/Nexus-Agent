"""Shared review-first GitHub credential and pre-commit checks.

No keyring/gh-login/GH_TOKEN fallback. Never include credentials in argv or errors.
Secret detection is heuristic, not a guarantee that every secret can be found.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .env import parse_env_text


class GitHubSafetyError(ValueError):
    pass


def token_from_environment(workspace):
    value = os.environ.get('GITHUB_TOKEN')
    if value is None:
        path = Path(workspace) / '.env'
        if path.is_symlink():
            raise GitHubSafetyError('GitHub .env must be a regular workspace file, not a symlink')
        if path.exists():
            if not path.is_file() or path.stat().st_size > 65536:
                raise GitHubSafetyError('GitHub .env must be a bounded regular file')
            value = parse_env_text(path.read_text(encoding='utf-8')).get('GITHUB_TOKEN')
    if not value or not 8 <= len(value) <= 4096 or not value.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value):
        raise GitHubSafetyError('Set GITHUB_TOKEN in the server environment or workspace .env; GitHub CLI login/GH_TOKEN fallback is disabled')
    return value


def github_environment(workspace):
    token = token_from_environment(workspace)
    env = dict(os.environ)
    for name in ('GH_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN', 'GH_DEBUG'):
        env.pop(name, None)
    env.update(GITHUB_TOKEN=token, GH_TOKEN=token, GH_HOST='github.com', GH_PROMPT_DISABLED='1',
               GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never')
    return env


def safe_output(text, env):
    from .studio import redact
    return redact(str(text).replace(env['GITHUB_TOKEN'], '[REDACTED_SECRET]'))


ASSIGNMENT = re.compile(r'''(?i)\b(?:github_token|gh_token|api[_-]?key|password|access[_-]?token|secret|client_secret)["']?[ \t]*[:=][ \t]*["']?([^\s"']{8,})''')


def scan_text(text, *, workspace=None):
    """Reject before storing previews/blobs/commits; never echo a finding's value."""
    from .studio import redact
    if not isinstance(text, str) or len(text.encode('utf-8')) > 2_000_000 or '\x00' in text:
        raise GitHubSafetyError('Secret scan requires bounded text; binary/oversized content is refused')
    unsafe = redact(text) != text or bool(re.search(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----', text))
    for match in ASSIGNMENT.finditer(text):
        value = match.group(1)
        if not value.startswith(('${', '${{', '{{')):
            unsafe = True
    if workspace is not None:
        # Exact matching also protects opaque credentials not recognized by heuristics.
        try:
            token = token_from_environment(workspace)
        except GitHubSafetyError:
            token = None  # Scanning a local commit does not require remote credentials.
        unsafe = unsafe or bool(token and token in text)
    from .redaction import secrets
    unsafe = unsafe or any(value in text for value in secrets(workspace))
    if unsafe:
        raise GitHubSafetyError('Potential secret detected; refused before approval/commit (value withheld)')


def conventional_message(message):
    if (not isinstance(message, str) or not message.strip() or len(message) > 2000 or '\x00' in message
            or not re.fullmatch(r'(?:feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(?:\([A-Za-z0-9_. /-]{1,60}\))?!?: [^\r\n]{1,120}', message.splitlines()[0])):
        raise GitHubSafetyError('Use a conventional commit subject, e.g. feat(api): add login (maximum 120-character summary)')
    scan_text(message)
    return message


def scan_files(root, paths):
    from .workbench import safe_path
    for name in paths:
        path = safe_path(Path(root), name)
        if (Path(root) / name).is_symlink():
            raise GitHubSafetyError('Secret scan refuses symlink changes')
        if path.exists():
            if not path.is_file() or path.stat().st_size > 2_000_000:
                raise GitHubSafetyError('Secret scan requires bounded regular text files')
            try:
                scan_text(path.read_text(encoding='utf-8'), workspace=root)
            except UnicodeError as exc:
                raise GitHubSafetyError('Unscannable binary file refused before commit') from exc


def scan_index(workspace):
    """Inspect actual staged blobs, not clean working files over a dirty index.

    Approval must already have occurred. No hooks, filters or textconv are run.
    Callers must not add/stage more content between this check and the commit.
    """
    import subprocess

    from .workbench import safe_path

    def git(*parts):
        result = subprocess.run(['git', '-c', 'core.fsmonitor=false', *parts], cwd=workspace,
                                capture_output=True, timeout=30)
        if result.returncode:
            raise GitHubSafetyError('Cannot inspect Git index; no commit attempted (Git output withheld)')
        return result.stdout

    raw = git('diff', '--cached', '--name-only', '--diff-filter=ACMRT', '-z')
    if len(raw) > 100_000:
        raise GitHubSafetyError('Too many staged paths for a bounded secret scan')
    paths = [name.decode('utf-8') for name in raw.split(b'\0') if name]
    if len(paths) > 200:
        raise GitHubSafetyError('Split commits into at most 200 staged files')
    modes = {}
    for item in git('ls-files', '--stage', '-z').split(b'\0'):
        if item:
            header, name = item.split(b'\t', 1)
            mode, _, stage = header.split()
            if stage != b'0':
                raise GitHubSafetyError('Resolve conflicted index entries before committing')
            modes[name.decode('utf-8')] = mode
    total = 0
    for name in paths:
        safe_path(Path(workspace), name)
        if modes.get(name) not in {b'100644', b'100755'}:
            raise GitHubSafetyError('Only regular staged text blobs can be secret-scanned')
        size = int(git('cat-file', '-s', ':' + name))
        total += size
        if size > 2_000_000 or total > 20_000_000:
            raise GitHubSafetyError('Staged blobs exceed the bounded secret-scan budget')
        try:
            text = git('cat-file', 'blob', ':' + name).decode('utf-8')
        except UnicodeError as exc:
            raise GitHubSafetyError('Unscannable staged binary refused before commit') from exc
        scan_text(text, workspace=workspace)
    return paths


def validate_gh_args(parts, workspace):
    """Keep typed gh commands on github.com and out of auth/alias/plugin shells."""
    from urllib.parse import urlsplit

    if not isinstance(parts, list) or not parts or any(not isinstance(p, str) or '\x00' in p for p in parts):
        raise GitHubSafetyError('Expected a bounded GitHub argument list')
    if len(parts) > 100 or sum(len(p) for p in parts) > 40000:
        raise GitHubSafetyError('GitHub arguments exceed the safety budget')
    if parts[0] not in {'api', 'pr', 'issue', 'repo', 'run', 'workflow', 'release', 'search'}:
        raise GitHubSafetyError('Typed gh only permits API/repository/PR/issue/workflow operations; auth/aliases/extensions are refused')
    if parts[0] == 'api':
        if len(parts) < 2 or parts[1].startswith('-'):
            raise GitHubSafetyError('Place the GitHub API endpoint immediately after api')
        endpoint = parts[1]
        if 'graphql' in endpoint.split('?')[0].lower():
            raise GitHubSafetyError('Generic GraphQL is refused; use reviewed operations')
        for i, arg in enumerate(parts):
            if arg in {'-f', '-F', '--field', '--raw-field'} or arg.startswith(('-f', '-F', '--field=', '--raw-field=')):
                raise GitHubSafetyError('Generic API writes/field expansion are refused; use reviewed operations')
            if arg in {'-X', '--method'} and (i + 1 >= len(parts) or parts[i + 1] != 'GET'):
                raise GitHubSafetyError('Generic API is GET-only')
            if arg.startswith(('--method=', '-X=')) and arg.split('=', 1)[1] != 'GET':
                raise GitHubSafetyError('Generic API is GET-only')
            if arg.startswith('-X') and arg != '-X' and arg != '-X=GET':
                raise GitHubSafetyError('Use an explicit GET method')
        if '://' in endpoint:
            url = urlsplit(endpoint)
            if url.scheme != 'https' or url.netloc != 'api.github.com' or url.username or url.password:
                raise GitHubSafetyError('API requests must target api.github.com without credentials')
        elif not re.fullmatch(r'/?[A-Za-z0-9_./?=&%:+{}-]+', endpoint) or endpoint.startswith('//'):
            raise GitHubSafetyError('Invalid relative GitHub API endpoint')
    if parts[0] == 'repo' and (len(parts) < 2 or parts[1] not in {'list', 'view'}):
        raise GitHubSafetyError('Use the reviewed nexus github create flow for repository mutations')
    if parts[0] == 'release' and (len(parts) < 2 or parts[1] not in {'list', 'view'}):
        raise GitHubSafetyError('Asset/file upload operations are not available through generic gh')
    if len(parts) > 2 and parts[0] in {'repo', 'pr', 'issue', 'run', 'workflow'} and '://' in parts[2]:
        raise GitHubSafetyError('Use repository-relative IDs, not host-qualified positional targets')
    for index, part in enumerate(parts):
        if any(part == flag or part.startswith(flag + '=') for flag in
               ('--hostname', '--host', '--web', '--browser', '--config', '--input', '--body-file', '--recover', '--auto', '--admin')):
            raise GitHubSafetyError('Host/config/file-input/browser/approval-bypass overrides are refused in typed GitHub tools')
        if part in {'-R', '--repo'} or part.startswith(('--repo=', '-R=')):
            value = part.split('=', 1)[1] if '=' in part else (parts[index + 1] if index + 1 < len(parts) else '')
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value) or '..' in value.split('/'):
                raise GitHubSafetyError('Use an owner/repository target on github.com')
        if part.startswith('-R') and part != '-R' and not part.startswith('-R='):
            raise GitHubSafetyError('Use a separate --repo owner/repository argument')
        if part in {'-H', '--header'} or part.startswith(('-H', '--header=')) or part == '-b' or part.startswith('-b='):
            raise GitHubSafetyError('Custom headers and shorthand file inputs are refused by typed GitHub tools')
        if parts[0] != 'api' and part.startswith('-F'):
            raise GitHubSafetyError('Short file-body input is refused by typed GitHub tools')
        if '=@' in part or part == '-' or part.startswith('@'):
            raise GitHubSafetyError('File/stdin expansion is refused by typed GitHub tools')
    token = token_from_environment(workspace)
    if token in '\n'.join(parts):
        raise GitHubSafetyError('Credentials must not appear in GitHub arguments')
    scan_text('\n'.join(parts), workspace=workspace)
