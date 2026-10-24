"""Provider adapters: Anthropic + OpenAI-compatible, and the fallback chain."""

from __future__ import annotations

import json

import pytest

from nexus.config import AgentConfig
from nexus.providers import (
    AnthropicProvider,
    OpenAICompatProvider,
    ProviderRegistry,
    parse_tool_call_from_text,
    tool_specs_to_prompt,
    truncate,
)
from nexus.types import (
    Completion,
    Message,
    ProviderError,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
)

from .conftest import FakeProvider, fake_registry, text_completion


class FakeResponse:
    def __init__(self, payload, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or json.dumps(payload)

    def json(self):
        return self._payload


class FakeHttp:
    """httpx.Client stand-in that records requests and replays canned responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def _next(self, default):
        if not self.responses:
            return FakeResponse(default)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, json=None, headers=None):  # noqa: A002
        self.requests.append({"url": url, "json": json, "headers": headers})
        return self._next({})

    def get(self, url, headers=None):
        self.requests.append({"url": url, "headers": headers})
        return self._next({"data": []})


def tool_spec(name: str = "write_file") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} does things",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #


def anthropic(raw, cfg=None):
    cfg = cfg or AgentConfig(
        name="claude", provider="anthropic", model="claude-sonnet-5-5", api_key="test-key", max_tokens=1024
    )
    transport = FakeHttp([FakeResponse(raw)]) if isinstance(raw, dict) else raw
    return AnthropicProvider(cfg, transport=transport)


def test_anthropic_request_shape():
    provider = anthropic({
        "id": "msg_1", "model": "claude-sonnet-5-5", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": 11, "output_tokens": 3},
    })
    completion = provider.complete("system prompt", [Message(role="user", content="hello")], [tool_spec()])

    request = provider._client.requests[0]
    assert request["url"].endswith("/v1/messages")
    assert request["headers"]["anthropic-version"]
    payload = request["json"]
    assert payload["model"] == "claude-sonnet-5-5"
    assert payload["system"] == "system prompt"
    assert payload["max_tokens"] == 1024
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert payload["tools"][0]["input_schema"]["type"] == "object"
    assert payload["tool_choice"] == {"type": "auto"}
    assert completion.text == "hi"
    assert completion.stop_reason == "end_turn"
    assert (completion.input_tokens, completion.output_tokens) == (11, 3)


def test_anthropic_tool_use_round_trip():
    provider = anthropic({
        "content": [
            {"type": "text", "text": "let me write that"},
            {"type": "tool_use", "id": "toolu_1", "name": "write_file", "input": {"path": "a.py", "content": "x"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    completion = provider.complete("s", [Message(role="user", content="go")], [tool_spec()])
    assert completion.wants_tools
    call = completion.tool_calls[0]
    assert (call.id, call.name) == ("toolu_1", "write_file")
    assert call.input["path"] == "a.py"

    # and the tool result goes back in the right shape
    provider2 = anthropic({"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn", "usage": {}})
    provider2.complete("s", [
        Message(role="user", content="go"),
        Message(role="assistant", content=[
            TextBlock(text="ok"), ToolCall(id="toolu_1", name="write_file", input={"path": "a.py"})
        ]),
        Message(role="user", content=[ToolResultBlock(tool_use_id="toolu_1", content="created a.py")]),
    ], [tool_spec()])
    sent = provider2._client.requests[0]["json"]["messages"]
    assert sent[-1]["content"][0] == {
        "type": "tool_result", "tool_use_id": "toolu_1", "content": "created a.py", "is_error": False,
    }


def test_anthropic_drops_empty_messages():
    provider = anthropic({"content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn", "usage": {}})
    provider.complete("s", [Message(role="user", content="   "), Message(role="user", content="real")], None)
    sent = provider._client.requests[0]["json"]["messages"]
    assert sent == [{"role": "user", "content": "real"}]


def test_anthropic_http_error_is_retryable_only_for_transient_codes():
    provider = anthropic(FakeHttp([FakeResponse({}, 429, "rate limited")] * 3))
    with pytest.raises(ProviderError) as exc:
        provider.complete("s", [Message(role="user", content="x")], None)
    assert exc.value.retryable and exc.value.status == 429

    provider = anthropic(FakeHttp([FakeResponse({}, 401, "bad key")]))
    with pytest.raises(ProviderError) as exc:
        provider.complete("s", [Message(role="user", content="x")], None)
    assert not exc.value.retryable and exc.value.status == 401


def test_anthropic_requires_an_api_key():
    cfg = AgentConfig(
        name="claude", provider="anthropic", model="claude-sonnet-5-5", api_key_env="NOT_SET_ANYWHERE_XYZ"
    )
    provider = AnthropicProvider(cfg)
    ok, detail = provider.health()
    assert not ok and "missing API key" in detail


def test_anthropic_health_lists_models():
    provider = anthropic(FakeHttp([FakeResponse({"data": [{"id": "claude-sonnet-5-5"}]})]))
    ok, detail = provider.health()
    assert ok and "claude-sonnet-5-5" in detail


def test_anthropic_text_tool_mode_parses_a_fenced_call():
    cfg = AgentConfig(
        name="claude", provider="anthropic", model="m", api_key="k", supports_tools=True, tool_mode="text"
    )
    provider = AnthropicProvider(cfg, transport=FakeHttp([FakeResponse({
        "content": [{"type": "text", "text":
            'I will write it.\n```tool\n{"name": "write_file", "arguments": {"path": "a.py", "content": "x"}}\n```'}],
        "stop_reason": "end_turn", "usage": {},
    })]))
    completion = provider.complete("s", [Message(role="user", content="go")], [tool_spec()])
    assert completion.stop_reason == "tool_use"
    assert completion.tool_calls[0].name == "write_file"
    assert "I will write it." in completion.text
    # the tool contract was injected into the system prompt, no native tools sent
    payload = provider._client.requests[0]["json"]
    assert "Available tools" in payload["system"]
    assert "tools" not in payload


# --------------------------------------------------------------------------- #
# OpenAI-compatible (Ollama / Hermes)
# --------------------------------------------------------------------------- #


def openai_compat(responses, *, tool_mode="native", supports_tools=True, name="hermes", fallback=None):
    cfg = AgentConfig(
        name=name, provider="openai-compatible", model="hermes3:8b",
        base_url="http://localhost:11434/v1", api_key="ollama", max_tokens=2048,
        tool_mode=tool_mode, supports_tools=supports_tools, fallback=fallback,
    )
    return OpenAICompatProvider(cfg, transport=FakeHttp(responses))


def test_openai_request_shape_and_tool_calls():
    provider = openai_compat([FakeResponse({
        "model": "hermes3:8b",
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path": "app/main.py"}'}}],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 30, "completion_tokens": 8},
    })])
    completion = provider.complete("sys", [Message(role="user", content="read it")], [tool_spec("read_file")])
    payload = provider._http.requests[0]["json"]
    assert payload["model"] == "hermes3:8b"
    assert payload["messages"][0] == {"role": "system", "content": "sys"}
    assert payload["messages"][1] == {"role": "user", "content": "read it"}
    assert payload["tools"][0]["function"]["name"] == "read_file"
    assert completion.tool_calls[0].input == {"path": "app/main.py"}
    assert completion.stop_reason == "tool_use"
    assert completion.input_tokens == 30


def test_openai_tool_results_become_tool_messages():
    provider = openai_compat(
        [FakeResponse({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}})]
    )
    provider.complete("sys", [
        Message(role="assistant", content=[ToolCall(id="c1", name="read_file", input={"path": "a"})]),
        Message(role="user", content=[ToolResultBlock(tool_use_id="c1", content="file body")]),
    ], [tool_spec("read_file")])
    sent = provider._http.requests[0]["json"]["messages"]
    assert sent[1]["tool_calls"][0]["function"]["arguments"] == '{"path": "a"}'
    assert sent[2] == {"role": "tool", "tool_call_id": "c1", "content": "file body"}


def test_openai_base_url_normalization():
    cfg = AgentConfig(name="x", provider="openai-compatible", model="m", base_url="http://localhost:11434")
    provider = OpenAICompatProvider(cfg, transport=FakeHttp([]))
    assert provider.chat_url == "http://localhost:11434/v1/chat/completions"
    assert provider.models_url == "http://localhost:11434/v1/models"
    cfg2 = AgentConfig(name="x", provider="openai-compatible", model="m", base_url="https://openrouter.ai/api/v1")
    provider2 = OpenAICompatProvider(cfg2, transport=FakeHttp([]))
    assert provider2.chat_url.endswith("/api/v1/chat/completions")


def test_dolphin_mixtral_leaks_tool_calls_into_text_and_we_recover():
    """dolphin-mixtral has no tool template - the JSON shows up in `content`."""
    provider = openai_compat(
        [FakeResponse({"choices": [{"message": {"content":
            'Sure.\n```json\n{"name": "run_command", "arguments": {"command": "nmap -sV 10.0.0.5"}}\n```'},
            "finish_reason": "stop"}], "usage": {}})],
        tool_mode="text",
        name="ollama-uncensored",
    )
    completion = provider.complete("sys", [Message(role="user", content="scan")], [tool_spec("run_command")])
    assert completion.tool_calls, "text-mode tool call was not recovered"
    assert completion.tool_calls[0].name == "run_command"
    assert completion.tool_calls[0].input["command"].startswith("nmap")
    assert completion.stop_reason == "tool_use"
    payload = provider._http.requests[0]["json"]
    assert "tools" not in payload  # no native tools array was sent
    assert "Available tools" in payload["messages"][0]["content"]


def test_openai_unreachable_server_raises_retryable_error():
    provider = openai_compat([ConnectionError("connection refused")] * 3)
    with pytest.raises(ProviderError) as exc:
        provider.complete("s", [Message(role="user", content="x")], None)
    assert exc.value.retryable
    assert "Is the local model server running?" in str(exc.value)


def test_openai_missing_model_hint():
    provider = openai_compat([FakeResponse({}, 404, '{"error":"model \'hermes3:8b\' does not exist"}')])
    with pytest.raises(ProviderError) as exc:
        provider.complete("s", [Message(role="user", content="x")], None)
    assert "ollama pull" in str(exc.value)


def test_openai_health_reports_missing_model():
    provider = openai_compat([FakeResponse({"data": [{"id": "llama3.1:8b"}]})] * 2)
    provider.model = "hermes3:8b"
    ok, detail = provider.health()
    assert ok
    assert "not pulled" in detail


def test_openai_health_reports_dead_server():
    provider = openai_compat([ConnectionError("refused")])
    ok, detail = provider.health()
    assert not ok
    assert "ollama serve" in detail or "unreachable" in detail


def test_openai_empty_response_is_an_error():
    provider = openai_compat([FakeResponse({"choices": []})])
    with pytest.raises(ProviderError):
        provider.complete("s", [Message(role="user", content="x")], None)


# --------------------------------------------------------------------------- #
# registry + fallback (Phase 8)
# --------------------------------------------------------------------------- #


def test_registry_builds_the_right_adapter():
    from nexus.providers import build_providers

    agents = {
        "claude": AgentConfig(name="claude", provider="anthropic", model="m", api_key="k"),
        "hermes": AgentConfig(name="hermes", provider="openai-compatible", model="m", base_url="http://x/v1"),
    }
    registry = build_providers(agents)
    assert isinstance(registry.get("claude"), AnthropicProvider)
    assert isinstance(registry.get("hermes"), OpenAICompatProvider)
    assert registry.get("claude") is registry.get("claude")  # cached
    with pytest.raises(ProviderError):
        registry.get("nope")


def test_unsupported_provider_type_is_rejected():
    agents = {"weird": AgentConfig(name="weird", provider="carrier-pigeon", model="m")}
    with pytest.raises(ProviderError):
        ProviderRegistry(agents).get("weird")


def test_fallback_chain_moves_to_the_next_agent():
    failing = FakeProvider("ollama-uncensored", [ProviderError("ollama is down", retryable=True)], fallback="claude")
    working = FakeProvider("claude", [text_completion("claude answered")])
    registry = fake_registry(failing, working)
    seen: list[dict] = []
    provider, completion = registry.call_with_fallback(
        "ollama-uncensored", "sys", [Message(role="user", content="task")], None,
        on_event=lambda e: seen.append(e.to_json()),
    )
    assert provider.name == "claude"
    assert completion.text == "claude answered"
    assert any("falling back" in str(e.get("message", "")) for e in seen)
    assert any(e.get("error") for e in seen)


def test_fallback_gives_up_after_the_whole_chain():
    a = FakeProvider("a", [ProviderError("down", retryable=True)], fallback="b")
    b = FakeProvider("b", [ProviderError("also down", retryable=True)], fallback="a")  # cycle
    with pytest.raises(ProviderError) as exc:
        fake_registry(a, b).call_with_fallback("a", "s", [Message(role="user", content="x")], None)
    assert "all providers failed" in str(exc.value)


def test_no_fallback_configured_raises_directly():
    a = FakeProvider("a", [ProviderError("down", retryable=False)])
    with pytest.raises(ProviderError):
        fake_registry(a).call_with_fallback("a", "s", [Message(role="user", content="x")], None)


# --------------------------------------------------------------------------- #
# text-mode helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected",
    [
        ('```tool\n{"name": "git", "arguments": {"args": ["status"]}}\n```', "git"),
        ('```json\n{"name": "read_file", "input": {"path": "a"}}\n```', "read_file"),
        ('{"name": "finish", "arguments": {"summary": "done"}}', "finish"),
        ('{"name":"run_command","arguments":{"command":"ls"}}', "run_command"),
        ('Here you go:\n```\n{"name": "write_file", "arguments": {"path": "a"}}\n```\nThanks!', "write_file"),
        ("no tool call here", None),
        ("```tool\n{broken json}\n```", None),
        ("", None),
    ],
)
def test_parse_tool_call_from_text(text, expected):
    call, _ = parse_tool_call_from_text(text)
    assert (call.name if call else None) == expected


def test_parse_tool_call_handles_nested_and_string_arguments():
    call, _ = parse_tool_call_from_text(
        '{"name": "run_command", "arguments": "{\\"command\\": \\"ls\\"}"}'
    )
    assert call.input == {"command": "ls"}

    call, _ = parse_tool_call_from_text(
        '```tool\n{"name": "gh_create_pr", "arguments": {"title": "x", "labels": ["a", "b"]}}\n```'
    )
    assert call.input == {"title": "x", "labels": ["a", "b"]}


def test_parse_tool_call_returns_clean_remaining_text():
    call, remaining = parse_tool_call_from_text('Sure!\n```tool\n{"name":"finish","arguments":{}}\n```\nBye.')
    assert call.name == "finish"
    assert "```" not in remaining
    assert "Sure!" in remaining and "Bye." in remaining


def test_tool_specs_to_prompt_lists_every_tool():
    prompt = tool_specs_to_prompt([tool_spec("a"), tool_spec("b")])
    assert "- a:" in prompt and "- b:" in prompt
    assert '"name"' in prompt


def test_truncate():
    assert truncate("abc", 10) == "abc"
    out = truncate("x" * 500, 100)
    assert len(out) < 500 and "truncated" in out


def test_completion_helpers():
    completion = Completion(
        content=[TextBlock(text="a"), ToolCall(id="1", name="t", input={})], stop_reason="tool_use"
    )
    assert completion.text == "a"
    assert completion.wants_tools
    assert len(completion.tool_calls) == 1
