"""Gesture control exposes no agent tools on purpose.

Gestures are a human input channel.  If the model could read or trigger them
it could confirm its own requests, so nothing is registered here.
"""
from __future__ import annotations


def register(reg, ctx) -> None:
    return None
