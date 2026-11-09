"""Per-step provider timeouts and graceful degradation (failover + spoken explanation)."""

from __future__ import annotations

import threading
import time

import pytest

from nexus.config import AgentConfig
from nexus.providers import ProviderError, ProviderRegistry
from nexus.types import Completion, Message, TextBlock


class _SlowProvider:
    """Provider that blocks on an event then returns a result.

    Uses threading.Event instead of time.sleep so the conftest's
    ``time.sleep`` no-op patch doesn't defeat the timeout test.
    """

    kind = "slow"

    def __init__(self, name: str, block: bool = False, text: str = "ok",
                 raise_exc: Exception | None = None):
        self.name = name
        self.block = block
        self.text = text
        self.raise_exc = raise_exc
        self.calls = 0
        self.fallback: str | None = None
        self._blocker = threading.Event()

    def complete(self, system, messages, tools=None, *, max_tokens=None, temperature=None, model=None):
        self.calls += 1
        if self.block:
            # Block until timeout kills the waiter thread.
            self._blocker.wait(timeout=30)
        if self.raise_exc is not None:
            raise self.raise_exc
        return Completion(
            content=[TextBlock(text=self.text)],
            stop_reason="end_turn",
            model=model or "fake",
            input_tokens=1,
            output_tokens=1,
        )

    def health(self):
        return True, "ok"


class _FailingProvider:
    """Provider that always raises."""

    kind = "fail"

    def __init__(self, name: str, text: str = "boom"):
        self.name = name
        self.text = text
        self.calls = 0
        self.fallback: str | None = None

    def complete(self, system, messages, tools=None, *, max_tokens=None, temperature=None, model=None):
        self.calls += 1
        raise ProviderError(self.text, retryable=True)

    def health(self):
        return False, "down"


def _agent_config(name: str, provider: str = "openai-compatible") -> AgentConfig:
    return AgentConfig(
        name=name,
        provider=provider,
        model="fake",
        api_key="test",
        timeout=180.0,
    )


def _registry(*providers, offline=False):
    agents = {}
    factories = {}
    for p in providers:
        agents[p.name] = _agent_config(p.name)
        factories[p.name] = p
    return ProviderRegistry(agents, factories=factories, offline=offline)


def test_per_step_timeout_causes_fallback():
    """A slow provider times out and the call falls back to the next provider."""
    fast = _SlowProvider("fast", text="fast-result")
    slow = _SlowProvider("slow", block=True, text="slow-result")
    slow.fallback = "fast"
    reg = _registry(slow, fast)

    provider, completion = reg.call_with_fallback("slow", "sys", [Message(role="user", content="hi")],
                                                   timeout=0.1)
    assert provider.name == "fast"
    assert completion.content[0].text == "fast-result"
    assert slow.calls == 1  # slow was attempted but timed out
    assert fast.calls == 1


def test_all_providers_timeout_raises():
    """When every provider times out, call_with_fallback raises."""
    slow1 = _SlowProvider("slow-a", block=True)
    slow2 = _SlowProvider("slow-b", block=True)
    slow1.fallback = "slow-b"
    reg = _registry(slow1, slow2)

    with pytest.raises(ProviderError):
        reg.call_with_fallback(
            "slow-a", "sys", [Message(role="user", content="hi")], timeout=0.05
        )


def test_provider_error_triggers_fallback():
    """A provider error (non-timeout) falls back to the next provider."""
    failing = _FailingProvider("fail-a", text="service down")
    ok = _SlowProvider("fail-b", text="backup-result")
    failing.fallback = "fail-b"
    reg = _registry(failing, ok)

    provider, completion = reg.call_with_fallback(
        "fail-a", "sys", [Message(role="user", content="hi")]
    )
    assert provider.name == "fail-b"
    assert completion.content[0].text == "backup-result"


def test_no_timeout_means_no_timeout_wrapper():
    """Without timeout, a slow call is allowed to complete."""
    fast = _SlowProvider("fast", text="done")
    reg = _registry(fast)
    provider, completion = reg.call_with_fallback("fast", "sys", [Message(role="user", content="hi")])
    assert completion.content[0].text == "done"


def test_offline_mode_refuses_cloud_providers():
    """Offline profile refuses cloud provider construction."""
    agents = {
        "gemini": _agent_config("gemini", provider="gemini"),
    }
    reg = ProviderRegistry(agents, offline=True)

    with pytest.raises(ProviderError, match="offline mode|refuses to construct"):
        reg.get("gemini")


def test_offline_mode_allows_local_provider():
    """Offline mode allows non-cloud (local/openai-compatible) providers."""
    local = _SlowProvider("ollama", text="local-result")
    agents = {"ollama": _agent_config("ollama", provider="openai-compatible")}
    factories = {"ollama": local}
    reg = ProviderRegistry(agents, factories=factories, offline=True)

    provider, completion = reg.call_with_fallback(
        "ollama", "sys", [Message(role="user", content="hi")]
    )
    assert completion.content[0].text == "local-result"


def test_timeout_is_per_step_not_cumulative():
    """Each provider call gets its own timeout budget."""
    p1 = _SlowProvider("p1", text="p1-result")
    p2 = _SlowProvider("p2", block=True, text="p2-result")
    p1.fallback = "p2"
    reg = _registry(p1, p2)

    # p1 succeeds within its timeout, so p2 never gets called.
    provider, completion = reg.call_with_fallback("p1", "sys", [Message(role="user", content="hi")],
                                                   timeout=0.1)
    assert provider.name == "p1"
    assert p2.calls == 0


def test_thread_cleanup_after_timeout():
    """A timed-out provider call doesn't leave a stuck thread."""
    slow = _SlowProvider("slow", block=True)
    reg = _registry(slow)

    try:
        reg.call_with_fallback("slow", "sys", [Message(role="user", content="hi")], timeout=0.05)
    except ProviderError:
        pass

    # Give the daemon thread time to finish (or be abandoned as daemon).
    time.sleep(0.2)
    # The slow thread is a daemon, so it won't block process exit.
    assert slow.calls == 1
