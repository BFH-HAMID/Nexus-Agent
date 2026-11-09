"""Literal credential masking across stream boundaries, logs, and OS keyring helpers."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any


def secrets(workspace=None, env=None):
    values = set()
    for source in (os.environ, env or {}):
        for name, value in source.items():
            if (re.search(r'(?i)(?:TOKEN|API_KEY|PRIVATE_KEY|SECRET|PASSWORD|AUTHORIZATION|_PIN)$', name)
                    and isinstance(value, str) and 6 <= len(value) <= 4096):
                values.add(value)
    if workspace is not None:
        from .github_safety import GitHubSafetyError, token_from_environment
        try:
            values.add(token_from_environment(workspace))
        except GitHubSafetyError:
            pass
    return sorted(values, key=len, reverse=True)


def mask(text, values=()):
    from .studio import redact
    text = str(text)
    for value in values:
        if value:
            text = text.replace(value, '[REDACTED_SECRET]')
    return redact(text)


def redact_text(text: Any, workspace: Path | str | None = None) -> str:
    """Mask environment secrets and known token/key patterns in *text*."""
    return str(mask(str(text), secrets(workspace)))


def get_secret(name: str, *, service: str = 'nexus-agent', env_name: str = '') -> str:
    """Resolve a secret from environment/.env first, then native OS keyring (no repo plaintext)."""
    if env_name:
        env_val = os.environ.get(env_name, '').strip()
        if env_val:
            return env_val
    try:
        import keyring
        backend = keyring.get_keyring()
        if type(backend).__module__ in {
            'keyring.backends.SecretService',
            'keyring.backends.macOS',
            'keyring.backends.Windows',
        }:
            return str(keyring.get_password(service, name) or '')
    except Exception:  # noqa: BLE001 - optional/headless fallback is empty string, never plaintext file
        return ''
    return ''


def set_secret(name: str, value: str, *, service: str = 'nexus-agent') -> bool:
    """Store a secret exclusively in the unlocked native OS keyring (never plaintext in repo/YAML)."""
    clean = str(value or '').strip()
    if not clean:
        raise ValueError('Secret value must not be empty')
    try:
        import keyring
    except ImportError as exc:
        raise ValueError('Install secret storage: pip install -e ".[secure-state]"') from exc
    backend = keyring.get_keyring()
    if type(backend).__module__ not in {
        'keyring.backends.SecretService',
        'keyring.backends.macOS',
        'keyring.backends.Windows',
    }:
        raise ValueError('A native unlocked OS credential vault is required; no plaintext fallback')
    keyring.set_password(service, name, clean)
    return True


class RedactingLogFilter(logging.Filter):
    """Logging filter that strips literal secrets and token patterns from log records."""

    def __init__(self, workspace: Path | str | None = None) -> None:
        super().__init__()
        self.workspace = workspace

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            message = str(record.msg)
        record.msg = redact_text(message, self.workspace)
        record.args = ()
        return True


def install_log_redaction(
    workspace: Path | str | None = None,
    *,
    logger_names: tuple[str, ...] = ('', 'nexus', 'httpx', 'uvicorn'),
) -> RedactingLogFilter:
    """Attach a deduplicated :class:`RedactingLogFilter` to application/HTTP loggers."""
    filt = RedactingLogFilter(workspace)
    for name in logger_names:
        logger = logging.getLogger(name)
        if not any(isinstance(existing, RedactingLogFilter) for existing in logger.filters):
            logger.addFilter(filt)
    return filt


class StreamMask:
    def __init__(self, values, *, hold_words=False):
        self.values = values
        self.pending = ''
        # Shared transcripts must not publish part of an unknown device/API token
        # before its pattern is complete. Legacy stream callers keep their timing.
        self.hold_words = hold_words
        self._discard_word = False

    def feed(self, text, *, final=False):
        if self._discard_word:
            boundary = re.search(r'\s', text)
            if not boundary:
                self._discard_word = not final
                return ''
            text = text[boundary.start():]
            self._discard_word = False
        text = self.pending + text
        for value in self.values:
            text = text.replace(value, '[REDACTED_SECRET]')
        keep = 0
        for value in self.values:
            for length in range(min(len(value) - 1, len(text)), keep, -1):
                if text.endswith(value[:length]):
                    keep = length
                    break
        if final:
            self.pending = ''
            # A capped/truncated credential prefix is also sensitive.
            text = text[:-keep] + '[REDACTED_PREFIX]' if keep >= 8 else text
            if self.hold_words:
                text = re.sub(r'(?<![A-Za-z0-9_-])nd1_[a-f0-9]{0,24}(?:_[A-Za-z0-9_-]{0,43})?(?![A-Za-z0-9_-])',
                              '[REDACTED_SECRET]', text)
            return mask(text, self.values)
        if self.hold_words:
            word = re.search(r'\S+$', text)
            keep = max(keep, len(word[0]) if word else 0)
            if keep > 4096:
                self.pending = ''
                self._discard_word = True
                return mask(text[:-keep] + '[REDACTED_LONG_TOKEN]', self.values)
        self.pending = text[-keep:] if keep else ''
        return mask(text[:-keep] if keep else text, self.values)
