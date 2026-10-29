"""Literal credential masking across stream boundaries, with bounded retained prefixes."""
from __future__ import annotations

import os
import re


def secrets(workspace=None, env=None):
    values = set()
    for source in (os.environ, env or {}):
        for name, value in source.items():
            if (re.search(r'(?i)(?:TOKEN|API_KEY|SECRET|PASSWORD|AUTHORIZATION|_PIN)$', name)
                    and isinstance(value, str) and 8 <= len(value) <= 4096):
                values.add(value)
    if workspace is not None:
        from .github_safety import GitHubSafetyError, token_from_environment
        try:
            values.add(token_from_environment(workspace))
        except GitHubSafetyError:
            pass
    return sorted(values, key=len, reverse=True)


def mask(text, values):
    from .studio import redact
    for value in values:
        text = text.replace(value, '[REDACTED_SECRET]')
    return redact(text)


class StreamMask:
    def __init__(self, values):
        self.values = values
        self.pending = ''

    def feed(self, text, *, final=False):
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
            return mask(text[:-keep] + '[REDACTED_PREFIX]' if keep >= 8 else text, self.values)
        self.pending = text[-keep:] if keep else ''
        return mask(text[:-keep] if keep else text, self.values)
