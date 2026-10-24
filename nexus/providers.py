"""Model adapters.

Nexus talks to the OpenAI chat-completions dialect, which is what all three
supported API providers speak:

* ``gemini``      - Google Gemini (`generativelanguage.googleapis.com/v1beta/openai`)
* ``deepseek``    - DeepSeek (`api.deepseek.com`)
* ``openrouter``  - OpenRouter (`openrouter.ai/api/v1`, any hosted open model)

``openai-compatible`` still works for anything else exposing
``/chat/completions`` (vLLM, llama.cpp, LM Studio, Together...), and the
legacy ``anthropic`` adapter is kept for people who point ``agents.yaml`` at
the Claude Messages API.

Every adapter normalizes to :class:`nexus.types.Completion`, and every adapter
can also fall back to *text-mode* tool calling for models whose chat template
has no tool parser (``tool_mode: text``).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from typing import Any

from . import keys as keyring
from .config import AgentConfig
from .types import (
    Completion,
    Message,
    Provider,
    ProviderError,
    TextBlock,
    ToolCall,
    ToolSpec,
)

try:  # optional, but preferred
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
TRANSIENT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}

# `provider:` values in agents.yaml that mean "OpenAI chat-completions dialect"
OPENAI_COMPAT_KINDS = {
    "openai",
    "openai-compatible",
    "openai_compat",
    "openai_compatible",
    "gemini",
    "google",
    "deepseek",
    "openrouter",
    "ollama",
    "hermes",
    "vllm",
    "lmstudio",
}
ANTHROPIC_KINDS = {"anthropic", "claude"}
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _retry(fn, *, attempts: int = 3, base_delay: float = 1.2):
    """Retry transient failures with a small linear backoff."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except ProviderError as exc:
            last = exc
            if not exc.retryable or i == attempts - 1:
                raise
            time.sleep(base_delay * (i + 1))
        except Exception as exc:  # connection resets and friends
            last = exc
            if i == attempts - 1:
                raise ProviderError(str(exc), retryable=False) from exc
            time.sleep(base_delay * (i + 1))
    raise ProviderError(str(last or "provider call failed"))  # pragma: no cover


def tool_specs_to_prompt(tools: Sequence[ToolSpec]) -> str:
    """Render tool schemas as a text contract for models without native tools."""
    lines = [
        "You can use tools by replying with a fenced JSON block. Use EXACTLY this shape:",
        "```tool",
        '{"name": "<tool_name>", "arguments": { ... }}',
        "```",
        "Rules:",
        "- At most ONE tool call per reply, then stop and wait for the result.",
        "- The tool result comes back in the next user message.",
        "- When you are done, reply with normal prose and NO tool block.",
        "",
        "Available tools:",
    ]
    for tool in tools:
        lines.append(f"- {tool.name}: {tool.description}")
        lines.append(f"  arguments schema: {json.dumps(tool.input_schema, ensure_ascii=False)}")
    return "\n".join(lines)


_FENCE = re.compile(r"```[a-zA-Z_-]*\s*(.*?)```", re.DOTALL)
_NAME_START = re.compile(r'\{\s*"name"\s*:')


def _json_objects(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Every ``{...}`` object in ``text`` that carries a ``name`` key (nesting safe)."""
    decoder = json.JSONDecoder()
    found: list[tuple[str, dict[str, Any]]] = []
    for match in _NAME_START.finditer(text):
        try:
            payload, end = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("name"):
            found.append((text[match.start() : match.start() + end], payload))
    return found


def parse_tool_call_from_text(text: str) -> tuple[ToolCall | None, str]:
    """Extract a text-mode tool call from prose. Returns (call, remaining_text).

    Local models without a tool template emit the call as text - fenced or not,
    with or without spaces - so we take the first JSON object carrying a
    ``name`` key, preferring one inside a code fence.
    """
    if not text or '"name"' not in text:
        return None, text

    candidates: list[tuple[str, dict[str, Any]]] = []
    for fenced in _FENCE.findall(text):
        candidates.extend(_json_objects(fenced))
    if not candidates:
        candidates = _json_objects(text)

    for blob, payload in candidates:
        args = payload.get("arguments")
        if args is None:
            args = payload.get("input")
        if args is None:
            args = payload.get("args")
        if args is None:
            args = {}
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
                args = parsed if isinstance(parsed, dict) else {"value": parsed}
            except json.JSONDecodeError:
                args = {"raw": args}
        if not isinstance(args, dict):
            args = {"value": args}
        call = ToolCall(
            id=f"txt_{abs(hash(blob)) % 10**10}",
            name=str(payload["name"]).strip(),
            input=args,
        )
        remaining = text.replace(blob, " ", 1)
        remaining = re.sub(r"```[a-zA-Z_-]*|```", "", remaining).strip()
        return call, remaining
    return None, text


def truncate(text: str, limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n... [truncated {len(text) - limit} chars] ...\n{tail}"


def _status_and_body(response: Any) -> tuple[int, str]:
    status = getattr(response, "status_code", 200)
    body = getattr(response, "text", "") or ""
    return int(status), str(body)


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #


class AnthropicProvider(Provider):
    kind = "anthropic"

    def __init__(self, cfg: AgentConfig, *, client: Any | None = None, transport: Any | None = None):
        self.cfg = cfg
        self.name = cfg.name
        self.model = cfg.model
        self.supports_tools = cfg.supports_tools
        self.tool_mode = cfg.tool_mode if cfg.supports_tools else "none"
        self.fallback = cfg.fallback
        self.base_url = (cfg.base_url or ANTHROPIC_DEFAULT_BASE_URL).rstrip("/")
        self.api_key = cfg.resolved_api_key
        self._client = client or transport     # injected transport (tests / custom http)
        self._sdk = None
        self._http = None

    # -- clients ----------------------------------------------------------- #
    def _ensure_client(self) -> None:
        if self._sdk is not None or self._http is not None or self._client is not None:
            return
        if not self.api_key:
            raise ProviderError(
                f"agent '{self.name}' needs an API key ({self.cfg.missing_key_hint()})",
                retryable=False,
            )
        try:
            import anthropic  # type: ignore

            self._sdk = anthropic.Anthropic(
                api_key=self.api_key, base_url=self.base_url or None, timeout=self.cfg.timeout
            )
            return
        except Exception:  # pragma: no cover - SDK missing
            pass
        if httpx is None:  # pragma: no cover
            raise ProviderError("install `anthropic` or `httpx` to use the Claude provider", retryable=False)
        self._http = httpx.Client(timeout=self.cfg.timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    # -- API --------------------------------------------------------------- #
    def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
    ) -> Completion:
        use_model = model or self.model
        native_tools = bool(tools) and self.supports_tools and self.tool_mode == "native"
        effective_system = system
        if tools and not native_tools:
            effective_system = f"{system}\n\n{tool_specs_to_prompt(tools)}"

        payload_messages = [m for m in (self._to_api_message(m) for m in messages) if m is not None]
        if not payload_messages:
            payload_messages = [{"role": "user", "content": "(no message)"}]

        payload: dict[str, Any] = {
            "model": use_model,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "system": effective_system,
            "messages": payload_messages,
        }
        temp = temperature if temperature is not None else self.cfg.temperature
        if temp is not None:
            payload["temperature"] = temp
        if native_tools:
            payload["tools"] = [t.to_anthropic() for t in (tools or [])]
            payload["tool_choice"] = {"type": "auto"}
        for key, value in (self.cfg.extra or {}).items():
            payload.setdefault(key, value)

        data = _retry(lambda: self._post(payload))
        return self._from_api(data, use_model)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/v1/messages"
        if self._client is not None:  # injected transport
            response = self._client.post(url, json=payload, headers=self._headers())
            if isinstance(response, dict):
                return response
            status, body = _status_and_body(response)
            if status >= 400:
                raise ProviderError(
                    f"anthropic {status}: {truncate(body, 1200)}",
                    retryable=status in TRANSIENT_STATUS_CODES,
                    status=status,
                )
            return response.json()

        self._ensure_client()
        if self._sdk is not None:
            try:
                message = self._sdk.messages.create(**payload)
            except Exception as exc:  # noqa: BLE001 - normalize SDK errors
                raise self._normalize_sdk_error(exc) from exc
            return json.loads(message.model_dump_json())

        assert self._http is not None
        try:
            response = self._http.post(url, json=payload, headers=self._headers())
        except Exception as exc:
            raise ProviderError(f"anthropic request failed: {exc}", retryable=True) from exc
        status, body = _status_and_body(response)
        if status >= 400:
            raise ProviderError(
                f"anthropic {status}: {truncate(body, 1200)}",
                retryable=status in TRANSIENT_STATUS_CODES,
                status=status,
            )
        return response.json()

    @staticmethod
    def _normalize_sdk_error(exc: Exception) -> ProviderError:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        retryable = status in TRANSIENT_STATUS_CODES or status is None
        return ProviderError(f"anthropic error: {exc}", retryable=retryable, status=status)

    def _to_api_message(self, message: Message) -> dict[str, Any] | None:
        if isinstance(message.content, str):
            if not message.content.strip():
                return None
            return {"role": message.role, "content": message.content}
        blocks: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                if block.text.strip():
                    blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolCall):
                blocks.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
            else:
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.tool_use_id,
                        "content": block.content,
                        "is_error": block.is_error,
                    }
                )
        if not blocks:
            return None
        return {"role": message.role, "content": blocks}

    def _from_api(self, data: dict[str, Any], model: str) -> Completion:
        content: list[Any] = []
        for block in data.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                content.append(TextBlock(text=block.get("text", "")))
            elif btype == "tool_use":
                content.append(
                    ToolCall(id=block.get("id", ""), name=block.get("name", ""), input=block.get("input") or {})
                )
        usage = data.get("usage") or {}
        completion = Completion(
            content=content,
            stop_reason=data.get("stop_reason") or "",
            model=data.get("model") or model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            raw=data,
        )
        if not completion.tool_calls and self.tool_mode == "text" and completion.text:
            call, remaining = parse_tool_call_from_text(completion.text)
            if call is not None:
                blocks = [TextBlock(text=remaining)] if remaining.strip() else []
                completion.content = blocks + [call]
                completion.stop_reason = "tool_use"
        return completion

    # -- introspection ----------------------------------------------------- #
    def health(self) -> tuple[bool, str]:
        if not self.api_key and self._client is None:
            return False, f"missing API key - {self.cfg.missing_key_hint()}"
        try:
            models = self.list_models()
            if models:
                return True, f"reachable (e.g. {models[0]})"
            self._ensure_client()
            return True, "reachable"
        except ProviderError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"{exc.__class__.__name__}: {exc}"

    def list_models(self) -> list[str]:
        self._ensure_client()
        if self._client is not None:
            response = self._client.get(f"{self.base_url}/v1/models", headers=self._headers())
            status, _ = _status_and_body(response)
            if status != 200:
                raise ProviderError(f"anthropic {status}", retryable=False, status=status)
            payload = response if isinstance(response, dict) else response.json()
            return [m.get("id", "") for m in (payload.get("data") or [])]
        if self._sdk is not None:
            return [m.id for m in self._sdk.models.list(limit=100).data]
        assert self._http is not None
        response = self._http.get(f"{self.base_url}/v1/models", headers=self._headers())
        status, body = _status_and_body(response)
        if status != 200:
            raise ProviderError(f"anthropic {status}: {truncate(body, 300)}",
                                retryable=status in TRANSIENT_STATUS_CODES, status=status)
        return [m.get("id", "") for m in (response.json().get("data") or [])]


# --------------------------------------------------------------------------- #
# OpenAI-compatible (Ollama / Hermes / vLLM / OpenRouter / LM Studio)
# --------------------------------------------------------------------------- #


class OpenAICompatProvider(Provider):
    """Gemini, DeepSeek, OpenRouter and any other OpenAI-dialect endpoint."""

    kind = "openai-compatible"

    def __init__(self, cfg: AgentConfig, *, transport: Any | None = None):
        self.cfg = cfg
        self.name = cfg.name
        self.model = cfg.model
        self.base_url = (cfg.effective_base_url or "http://localhost:11434/v1").rstrip("/")
        self.api_key = cfg.resolved_api_key or ""
        self.family = cfg.key_family
        self.supports_tools = cfg.supports_tools
        self.tool_mode = cfg.tool_mode if cfg.supports_tools else "none"
        self.fallback = cfg.fallback
        self.is_local = any(host in self.base_url for host in LOCAL_HOSTS)
        if httpx is None and transport is None:  # pragma: no cover
            raise ProviderError("httpx is required for openai-compatible providers", retryable=False)
        self._http = transport or httpx.Client(timeout=cfg.timeout)

    # -- URLs -------------------------------------------------------------- #
    def _endpoint(self, suffix: str) -> str:
        """Join a base URL with an API path.

        Handles the three shapes in the wild: ``.../v1`` (OpenAI, DeepSeek,
        OpenRouter), ``.../v1beta/openai`` (Gemini), and a full
        ``.../chat/completions`` URL pasted by hand.
        """
        base = self.base_url.rstrip("/")
        if base.endswith(suffix):
            return base
        if base.endswith(("/v1", "/openai", "/v1beta", "/api/v1beta")):
            return f"{base}{suffix}"
        return f"{base}/v1{suffix}"

    @property
    def chat_url(self) -> str:
        return self._endpoint("/chat/completions")

    @property
    def models_url(self) -> str:
        return self._endpoint("/models")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.family == "openrouter":
            headers.setdefault("HTTP-Referer", "https://github.com/BFH-HAMID/Nexus-Agent")
            headers.setdefault("X-Title", "Nexus Agent")
        return headers

    def _get(self, url: str) -> Any:
        """GET with a short timeout - health checks must never hang on a dead box."""
        timeout = min(float(self.cfg.timeout or 10.0), 10.0)
        try:
            return self._http.get(url, headers=self._headers(), timeout=timeout)
        except TypeError:  # injected transports / very old clients: no timeout kwarg
            return self._http.get(url, headers=self._headers())

    def _require_key(self) -> None:
        """Fail fast (and locally) when a hosted provider has no key.

        Keyless endpoints are allowed when they are loopback (`ollama serve`) or
        when the agent is marked `optional: true` (a self-hosted box on the LAN).
        """
        if self.api_key or self.is_local or self.cfg.optional:
            return
        raise ProviderError(
            f"agent '{self.name}' ({self.cfg.provider}) has no API key - {self.cfg.missing_key_hint()}",
            retryable=False,
        )

    def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
    ) -> Completion:
        self._require_key()
        native_tools = bool(tools) and self.supports_tools and self.tool_mode == "native"
        effective_system = system
        if tools and not native_tools:
            effective_system = f"{system}\n\n{tool_specs_to_prompt(tools)}"

        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": self._to_api_messages(effective_system, messages),
            "stream": False,
        }
        max_out = max_tokens or self.cfg.max_tokens
        if max_out:
            payload["max_tokens"] = max_out
        temp = temperature if temperature is not None else self.cfg.temperature
        if temp is not None:
            payload["temperature"] = temp
        if native_tools:
            payload["tools"] = [t.to_openai() for t in (tools or [])]
        for key, value in (self.cfg.extra or {}).items():
            payload.setdefault(key, value)

        data = _retry(lambda: self._post(payload))
        return self._from_api(data, payload["model"])

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._http.post(self.chat_url, json=payload, headers=self._headers())
        except Exception as exc:
            raise ProviderError(
                f"{self.name}: cannot reach {self.chat_url} ({exc.__class__.__name__}). "
                "Is the local model server running?",
                retryable=True,
            ) from exc
        status, body = _status_and_body(response)
        if status >= 400:
            hint = ""
            if status in {401, 403}:
                hint = f" - check the API key: {self.cfg.missing_key_hint()}"
            elif status == 404:
                if "does not exist" in body or "not found" in body.lower():
                    hint = f" (pull the model: `ollama pull {self.model}`)" if self.is_local else (
                        f" - is '{self.model}' a valid model id? try `nexus models {self.name}`"
                    )
            raise ProviderError(
                f"{self.name} HTTP {status}: {truncate(body, 1000)}{hint}",
                retryable=status in TRANSIENT_STATUS_CODES,
                status=status,
            )
        if hasattr(response, "json"):
            return response.json()
        return json.loads(response)  # pragma: no cover

    def _to_api_messages(self, system: str, messages: Sequence[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if system.strip():
            out.append({"role": "system", "content": system})
        for message in messages:
            if isinstance(message.content, str):
                if message.content.strip():
                    out.append({"role": message.role, "content": message.content})
                continue
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ToolCall):
                    tool_calls.append(
                        {
                            "id": block.id,
                            "type": "function",
                            "function": {"name": block.name, "arguments": json.dumps(block.input)},
                        }
                    )
                else:
                    tool_results.append(
                        {"role": "tool", "tool_call_id": block.tool_use_id, "content": block.content}
                    )
            if tool_results:
                # OpenAI style: every tool result is its own message
                if text_parts:
                    out.append({"role": message.role, "content": "\n".join(text_parts)})
                out.extend(tool_results)
                continue
            entry: dict[str, Any] = {"role": message.role, "content": "\n".join(p for p in text_parts if p)}
            if tool_calls:
                entry["tool_calls"] = tool_calls
                # Gemini's compatibility layer rejects `content: null`
                entry["content"] = entry.get("content") or ""
            if entry.get("content") or entry.get("tool_calls"):
                out.append(entry)
        if not out:
            out.append({"role": "user", "content": "(no message)"})
        return out

    def _from_api(self, data: dict[str, Any], model: str) -> Completion:
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(
                f"{self.name}: empty response ({truncate(json.dumps(data), 400)})", retryable=True
            )
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):  # some servers return content parts
            text = "\n".join(part.get("text", "") for part in text if isinstance(part, dict))
        content: list[Any] = []
        if text.strip():
            content.append(TextBlock(text=text))

        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    args = {"_raw": raw_args}
            else:
                args = raw_args
            if not isinstance(args, dict):
                args = {"value": args}
            content.append(
                ToolCall(id=call.get("id") or f"call_{len(content)}", name=fn.get("name", ""), input=args)
            )

        finish = choices[0].get("finish_reason") or ""
        usage = data.get("usage") or {}
        completion = Completion(
            content=content,
            stop_reason="tool_use" if any(isinstance(c, ToolCall) for c in content) else finish,
            model=data.get("model") or model,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            raw=data,
        )

        # Local models frequently leak tool calls into prose. Recover them so
        # the loop keeps working even when the server has no tool parser.
        if not completion.tool_calls and text:
            call, remaining = parse_tool_call_from_text(text)
            if call is not None:
                completion.content = ([TextBlock(text=remaining)] if remaining.strip() else []) + [call]
                completion.stop_reason = "tool_use"
        return completion

    # -- introspection ----------------------------------------------------- #
    def health(self) -> tuple[bool, str]:
        if not self.api_key and not self.is_local and not self.cfg.optional:
            return False, f"no API key - {self.cfg.missing_key_hint()}"
        try:
            response = self._get(self.models_url)
        except Exception as exc:
            if self.is_local:
                return False, (f"no local server at {self.base_url} ({exc.__class__.__name__}) "
                               "- start it with `ollama serve`")
            return False, f"unreachable at {self.base_url} ({exc.__class__.__name__})"
        status, body = _status_and_body(response)
        if status in {401, 403}:
            return False, f"HTTP {status} - the API key was rejected ({self.cfg.missing_key_hint()})"
        if status != 200:
            return False, f"HTTP {status} from {self.models_url}: {truncate(body, 200)}"
        models = self.list_models()
        short = self.model.split(":")[0]
        if models and not any(m == self.model or m == short or m.startswith(short) for m in models):
            where = "pulled" if self.is_local else "available"
            return True, f"reachable, but model '{self.model}' is not {where} ({len(models)} models listed)"
        return True, f"reachable, {len(models)} model(s) listed"

    def list_models(self) -> list[str]:
        try:
            response = self._get(self.models_url)
            status, _ = _status_and_body(response)
            if status != 200:
                return []
            data = response.json()
        except Exception:  # noqa: BLE001
            return []
        out: list[str] = []
        for item in data.get("data") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("id") or "")
            if name.startswith("models/"):
                name = name[len("models/"):]
            if name:
                out.append(name)
        return out


# --------------------------------------------------------------------------- #
# registry + fallback chain (Phase 8)
# --------------------------------------------------------------------------- #


class ProviderRegistry:
    """Builds and caches providers, and walks the ``fallback`` chain."""

    def __init__(self, agents: dict[str, AgentConfig], *, factories: dict[str, Any] | None = None):
        self.agents = agents
        self._cache: dict[str, Provider] = {}
        self._factories = factories or {}

    def get(self, name: str) -> Provider:
        if name in self._cache:
            return self._cache[name]
        if name in self._factories:  # injected provider (tests / custom setups)
            provider = self._factories[name]
            self._cache[name] = provider
            return provider
        cfg = self.agents.get(name)
        if cfg is None:
            raise ProviderError(
                f"unknown agent '{name}' (known: {', '.join(sorted(self.agents)) or 'none'})", retryable=False
            )
        provider: Provider
        kind = str(cfg.provider or "").strip().lower()
        if kind in ANTHROPIC_KINDS:
            provider = AnthropicProvider(cfg)
        elif kind in OPENAI_COMPAT_KINDS or keyring.provider_spec(kind) is not None:
            provider = OpenAICompatProvider(cfg)
        else:
            raise ProviderError(
                f"agent '{name}': unsupported provider '{cfg.provider}' "
                f"(use gemini, deepseek, openrouter or openai-compatible)",
                retryable=False,
            )
        self._cache[name] = provider
        return provider

    def call_with_fallback(
        self,
        name: str,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
        on_event: Any = None,
    ) -> tuple[Provider, Completion]:
        """Try ``name``; on failure walk its ``fallback`` chain."""
        from .types import Event, EventType

        tried: list[str] = []
        current: str | None = name
        last_error: Exception | None = None
        while current and current not in tried:
            tried.append(current)
            provider = self.get(current)
            try:
                completion = provider.complete(
                    system,
                    messages,
                    tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    model=model if current == name else None,
                )
                return provider, completion
            except ProviderError as exc:
                last_error = exc
                if on_event is not None:
                    on_event(Event(EventType.AGENT, {"level": "warn", "agent": current, "error": str(exc)}))
                current = provider.fallback
                if current and on_event is not None:
                    on_event(
                        Event(EventType.AGENT, {"level": "info", "message": f"falling back to agent '{current}'"})
                    )
        raise ProviderError(f"all providers failed ({' -> '.join(tried)}): {last_error}", retryable=False)


def build_providers(agents: dict[str, AgentConfig], **kwargs: Any) -> ProviderRegistry:
    return ProviderRegistry(agents, **kwargs)
