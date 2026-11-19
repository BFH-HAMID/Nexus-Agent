"""Shared feature-policy helpers: what counts as a cloud agent, and keeping local-only calls local."""
from __future__ import annotations

from dataclasses import replace

import pytest

from nexus import policyfile
from nexus.types import ProviderError

from .conftest import FakeProvider, text_completion
from .featurepack_helpers import scripted_agents


def test_cloud_means_the_endpoint_not_the_provider_name(config):
    base = replace(config.agents["ollama"], provider="openai-compatible")
    config.agents.update({
        "this-machine": replace(base, name="this-machine", base_url="http://localhost:11434/v1"),
        "lan-box": replace(base, name="lan-box", base_url="http://192.168.1.20:11434/v1"),
        "hosted-compatible": replace(base, name="hosted-compatible", base_url="https://api.example.com/v1"),
        "no-endpoint": replace(base, name="no-endpoint", provider="anthropic", base_url=None),
    })
    assert not policyfile.is_cloud_agent(config, "this-machine")
    assert not policyfile.is_cloud_agent(config, "lan-box")
    # a hosted OpenAI-compatible service is a cloud agent although it has no built-in key family
    assert config.agents["hosted-compatible"].key_family is None
    assert policyfile.is_cloud_agent(config, "hosted-compatible")
    for name in ("gemini", "deepseek", "openrouter", "no-endpoint", "not-defined-anywhere"):
        assert policyfile.is_cloud_agent(config, name), name


def test_a_local_only_call_is_never_handed_to_a_cloud_fallback(config):
    box = FakeProvider("box", [ProviderError("connection refused")], fallback="hosted")
    hosted = FakeProvider("hosted", [text_completion('{"answer": 1}')])
    providers = scripted_agents(config, box, hosted, cloud=("hosted",))
    with pytest.raises(ProviderError, match="'hosted' is not allowed for this call; 'box' failed: connection refused"):
        policyfile.ask_json(providers, "box", "Summarise.", {"text": "private notes"}, allow_cloud=False)
    assert len(box.calls) == 1 and hosted.calls == [], "the payload must not leave the machine"
    # naming a cloud agent directly is refused as well
    with pytest.raises(ProviderError, match="'hosted' is not allowed"):
        policyfile.ask_json(providers, "hosted", "Summarise.", {"text": "private notes"}, allow_cloud=False)
    assert hosted.calls == []
    # with the operator's opt-in the normal fallback chain applies
    box.script.append(ProviderError("connection refused"))
    value, usage = policyfile.ask_json(providers, "box", "Summarise.", {"text": "private notes"}, allow_cloud=True)
    assert value == {"answer": 1} and usage["agent"] == "hosted" and len(hosted.calls) == 1


def test_a_local_fallback_is_still_used(config):
    box = FakeProvider("box", [ProviderError("model not loaded")], fallback="spare")
    spare = FakeProvider("spare", [text_completion('{"answer": 2}')])
    providers = scripted_agents(config, box, spare)
    value, usage = policyfile.ask_json(providers, "box", "Summarise.", {"text": "notes"}, allow_cloud=False)
    assert value == {"answer": 2} and usage["agent"] == "spare"


@pytest.mark.parametrize("reply", ['```json\n{"answer": 3}\n```', '```\n{"answer": 3}```', 'Sure:\n{"answer": 3}\nDone.',
                                   '```json\n{"answer": 3}' + " " * 59_000 + "x"])
def test_fences_and_chatter_around_the_object_are_ignored(config, reply):
    import time

    box = FakeProvider("box", [text_completion(reply)])
    providers = scripted_agents(config, box)
    started = time.monotonic()
    value, _usage = policyfile.ask_json(providers, "box", "Summarise.", {"text": "notes"}, allow_cloud=False)
    assert value == {"answer": 3}
    assert time.monotonic() - started < 3, "a long run of spaces took about 5 s when fences were removed by a pattern"


@pytest.mark.parametrize("reply", ["I think the answer is two.", '{"answer": ', "[1, 2]", "{" + '"a": "' + "x" * 60_001 + '"}'])
def test_an_unusable_reply_still_reports_what_it_cost(config, reply):
    box = FakeProvider("box", [text_completion(reply)])
    providers = scripted_agents(config, box)
    with pytest.raises(policyfile.UnusableReply) as caught:
        policyfile.ask_json(providers, "box", "Summarise.", {"text": "notes"}, allow_cloud=False)
    assert isinstance(caught.value, ValueError), "callers that fall back on ValueError keep working"
    assert caught.value.usage == {"agent": "box", "model": "fake-model", "input_tokens": 10, "output_tokens": 5}


def test_a_sealed_store_says_early_when_it_could_not_be_written(repo):
    """An empty store loads without the keyring, so a missing keyring would only show at the first save."""
    from nexus.policyfile import SealedJSON

    from .featurepack_helpers import BrokenKeyring, MemoryKeyring

    keyring = MemoryKeyring()
    store = SealedJSON(repo, "probe", "data", keyring_module=keyring)
    assert store.problem() == "" and keyring.data == {} and not store.exists()   # looking creates no key and no file
    store.save({"a": 1})
    assert store.problem() == "" and len(keyring.data) == 1
    broken = SealedJSON(repo, "probe", "other", keyring_module=BrokenKeyring())
    assert broken.load({"empty": True}) == {"empty": True}
    assert "OS keyring unavailable for sealed probe state" in broken.problem()
    assert SealedJSON(repo, "probe", "plain", encrypt=False, keyring_module=BrokenKeyring()).problem() == ""
