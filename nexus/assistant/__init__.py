"""Opt-in personal-assistant capabilities built on the existing Nexus safety core.

Nothing in this package starts a microphone, camera, external account connection,
background service, or external mutation at import time. Optional native engines
are probed only when :func:`capabilities` is explicitly called.
"""

from .capabilities import capabilities

__all__ = ["capabilities"]
