"""End-to-end adapter test against a real (local, offline) HTTP server.

The point is to prove the three supported providers are wired correctly:
base URL → endpoint path, `Authorization: Bearer`, request body shape, and
tool-call parsing on the way back. Nothing leaves the machine.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nexus.config import AgentConfig
from nexus.providers import OpenAICompatProvider
from nexus.types import Message, ToolCall, ToolSpec


class _OpenAIStub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[dict] = []
    reply: dict = {}

    def log_message(self, *args):  # silence
        pass

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        type(self).requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
        self._send({"data": [{"id": "models/gemini-3.8-flash"}, {"id": "deepseek-flash"}]})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(
            {"method": "POST", "path": self.path, "headers": dict(self.headers), "body": body}
        )
        self._send(type(self).reply)


@pytest.fixture()
def stub():
    _OpenAIStub.requests = []
    _OpenAIStub.reply = {
        "id": "chatcmpl-1",
        "model": "stub-model",
        "choices": [{"message": {"role": "assistant", "content": "hello from the stub"}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _agent(base: str, *, provider: str, model: str = "stub-model", **kwargs) -> AgentConfig:
    return AgentConfig(name=provider, provider=provider, model=model, base_url=base,
                       api_key="test-key-123456", **kwargs)


@pytest.mark.parametrize(
    "provider,suffix",
    [
        ("gemini", "/v1beta/openai"),
        ("deepseek", "/v1"),
        ("openrouter", "/v1"),
    ],
)
def test_chat_roundtrip_for_each_provider(stub, provider, suffix):
    cfg = _agent(f"{stub}{suffix}", provider=provider)
    provider_obj = OpenAICompatProvider(cfg)
    assert provider_obj.chat_url == f"{stub}{suffix}/chat/completions"

    completion = provider_obj.complete("you are a test", [Message(role="user", content="hi")])

    assert completion.text == "hello from the stub"
    assert completion.input_tokens == 11 and completion.output_tokens == 7
    request = _OpenAIStub.requests[-1]
    assert request["method"] == "POST"
    assert request["path"] == f"{suffix}/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer test-key-123456"
    assert request["body"]["model"] == "stub-model"
    assert request["body"]["messages"][0] == {"role": "system", "content": "you are a test"}
    assert request["body"]["messages"][1] == {"role": "user", "content": "hi"}


def test_native_tools_are_sent_and_parsed_back(stub):
    _OpenAIStub.reply = {
        "model": "stub-model",
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{\"path\": \"README.md\"}"},
                }],
            },
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }
    spec = ToolSpec(
        name="read_file",
        description="read a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    provider = OpenAICompatProvider(_agent(f"{stub}/v1", provider="deepseek"))
    completion = provider.complete("sys", [Message(role="user", content="read it")], [spec])

    assert completion.stop_reason == "tool_use"
    calls = completion.tool_calls
    assert len(calls) == 1 and calls[0].name == "read_file"
    assert calls[0].input == {"path": "README.md"}

    sent_tools = _OpenAIStub.requests[-1]["body"]["tools"]
    assert sent_tools[0]["type"] == "function"
    assert sent_tools[0]["function"]["name"] == "read_file"
    assert sent_tools[0]["function"]["parameters"]["properties"]["path"]["type"] == "string"


def test_tool_call_only_assistant_messages_use_an_empty_content(stub):
    """Gemini's compatibility layer rejects `content: null` on assistant turns."""
    history = [
        Message(role="user", content="read it"),
        Message(role="assistant", content=[ToolCall(id="call_1", name="read_file", input={"path": "a"})]),
    ]
    provider = OpenAICompatProvider(_agent(f"{stub}/v1", provider="gemini"))
    provider.complete("sys", history)

    messages = _OpenAIStub.requests[-1]["body"]["messages"]
    assistant = [m for m in messages if m.get("tool_calls")][0]
    assert assistant["content"] == ""
    assert assistant["tool_calls"][0]["function"]["name"] == "read_file"


def test_text_mode_recovers_a_leaked_tool_call(stub):
    _OpenAIStub.reply = {
        "model": "stub-model",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "I will read it.\n```tool\n{\"name\": \"read_file\", \"arguments\": {\"path\": \"x.py\"}}\n```",
            },
        }],
    }
    spec = ToolSpec(name="read_file", description="read a file",
                    input_schema={"type": "object", "properties": {}})
    provider = OpenAICompatProvider(_agent(f"{stub}/v1", provider="openrouter", tool_mode="text"))
    completion = provider.complete("sys", [Message(role="user", content="go")], [spec])

    assert completion.stop_reason == "tool_use"
    assert completion.tool_calls[0].name == "read_file"
    # text mode injects the tool contract into the system prompt
    system = _OpenAIStub.requests[-1]["body"]["messages"][0]["content"]
    assert "Available tools:" in system


def test_models_endpoint_is_normalised(stub):
    provider = OpenAICompatProvider(_agent(f"{stub}/v1beta/openai", provider="gemini"))
    assert provider.models_url == f"{stub}/v1beta/openai/models"
    assert provider.list_models() == ["gemini-3.8-flash", "deepseek-flash"]
