"""Core data types shared by the router, providers, tools and agent loop.

Everything here is provider-agnostic: the Anthropic adapter and the
OpenAI-compatible adapter (Ollama / Hermes / vLLM / OpenRouter ...) both
translate to and from these types.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #

VALID_ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def sanitize_role_name(raw: str) -> str:
    """Sanitize an LLM/CLI supplied role name -> ``a-z0-9-`` only."""
    cleaned = re.sub(r"[^a-z0-9-]+", "-", (raw or "").strip().lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned[:64]


class Risk(str, Enum):
    """How dangerous a tool call is. Drives the approval gate."""

    READ = "read"          # never needs approval
    WRITE = "write"        # needs approval unless --yes / always-allow
    DANGEROUS = "danger"   # always needs explicit approval, --yes won't skip


@dataclass
class Keyword:
    """A router keyword. ``weight`` > 1 boosts strong signals (e.g. ``nmap``)."""

    text: str
    weight: float = 1.0

    @classmethod
    def parse(cls, value: Any) -> Keyword:
        if isinstance(value, Keyword):
            return value
        if isinstance(value, dict):
            return cls(str(value.get("text", value.get("kw", ""))), float(value.get("weight", 1.0)))
        text = str(value)
        # "nmap:3" style inline weight
        if ":" in text:
            head, _, tail = text.rpartition(":")
            try:
                return cls(head.strip().lower(), float(tail))
            except ValueError:
                pass
        return cls(text.strip().lower(), 1.0)


@dataclass
class Role:
    """One specialist "employee" of the Nexus team."""

    name: str
    persona: str = ""
    agent: str = ""
    category: str = "general"
    title: str = ""
    keywords: list[Keyword] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    tools: list[str] | None = None          # None => inherit agent default
    focus: list[str] = field(default_factory=list)
    model: str | None = None                # optional per-role model override
    temperature: float | None = None
    extra_instructions: str = ""
    source: str = ""                        # which yaml file it came from

    # -- matching helpers -------------------------------------------------- #
    @property
    def match_names(self) -> list[str]:
        return [self.name, *self.aliases]

    def keyword_score(self, haystack: str) -> tuple[float, list[str]]:
        """Score this role against a task string. Returns (score, hits)."""
        text = haystack.lower()
        score = 0.0
        hits: list[str] = []
        for kw in self.keywords:
            token = kw.text
            if not token:
                continue
            if _contains_token(text, token):
                score += kw.weight
                hits.append(token)
        return score, hits

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "category": self.category,
            "agent": self.agent,
            "persona": self.persona,
            "keywords": [k.text for k in self.keywords],
        }


def _contains_token(haystack: str, token: str) -> bool:
    """Word-ish boundary match. Alphanumeric tokens must not match inside a
    longer word (``api`` should not fire on ``rapid``), but tokens that carry
    punctuation (``next.js``, ``c++``, ``node:fs``) fall back to substring."""
    if token in haystack:
        if token.isalnum():
            return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", haystack) is not None
        return True
    return False


# --------------------------------------------------------------------------- #
# Messages / content blocks
# --------------------------------------------------------------------------- #


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    type: str = "tool_use"


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: str = "tool_result"


ContentBlock = TextBlock | ToolCall | ToolResultBlock


@dataclass
class Message:
    role: str  # "user" | "assistant" | "system"
    content: str | list[ContentBlock]

    @property
    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "\n".join(b.text for b in self.content if isinstance(b, TextBlock))

    @property
    def tool_calls(self) -> list[ToolCall]:
        if isinstance(self.content, str):
            return []
        return [b for b in self.content if isinstance(b, ToolCall)]

    def char_len(self) -> int:
        if isinstance(self.content, str):
            return len(self.content)
        total = 0
        for b in self.content:
            if isinstance(b, TextBlock):
                total += len(b.text)
            elif isinstance(b, ToolCall):
                total += len(b.name) + len(str(b.input))
            else:
                total += len(b.content)
        return total


@dataclass
class ToolSpec:
    """JSON-schema description of a tool, as sent to the model."""

    name: str
    description: str
    input_schema: dict[str, Any]
    risk: Risk = Risk.WRITE
    read_only: bool = False
    # subcommands of a wrapper tool (``git``) that stay read-only: they never
    # need approval, even in plan mode
    readonly_subcommands: frozenset[str] = frozenset()

    def is_readonly_call(self, args: dict[str, Any]) -> bool:
        if self.read_only or self.risk is Risk.READ:
            return True
        if not self.readonly_subcommands:
            return False
        raw = args.get("args")
        if isinstance(raw, str):
            import shlex

            try:
                raw = shlex.split(raw)
            except ValueError:
                raw = raw.split()
        if not isinstance(raw, (list, tuple)) or not raw:
            return False
        return str(raw[0]).strip("-") in self.readonly_subcommands

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass
class Completion:
    """Normalized model response."""

    content: list[ContentBlock] = field(default_factory=list)
    stop_reason: str = ""            # "end_turn" | "tool_use" | "max_tokens" | ...
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Any = None

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.content if isinstance(b, TextBlock)).strip()

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [b for b in self.content if isinstance(b, ToolCall)]

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls) or self.stop_reason == "tool_use"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.calls += other.calls

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# --------------------------------------------------------------------------- #
# Events (CLI rendering + VS Code sidebar / SSE)
# --------------------------------------------------------------------------- #


class EventType(str, Enum):
    STATUS = "status"            # info line
    ROLE = "role"                # routing decision
    AGENT = "agent"              # provider switch / fallback
    THINK = "think"              # a model call is in flight (per turn)
    USAGE = "usage"              # token counter update (live meters)
    TEXT = "text"                # assistant prose (complete message)
    TEXT_DELTA = "text_delta"    # streaming slice of assistant prose
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    APPROVAL = "approval"        # waiting for the human
    ERROR = "error"
    MEMORY = "memory"
    DONE = "done"


@dataclass
class Event:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type.value, **self.data}


EventHandler = Callable[[Event], None]


class NullHandler:
    def __call__(self, event: Event) -> None:  # pragma: no cover - trivial
        return None


# --------------------------------------------------------------------------- #
# Provider protocol
# --------------------------------------------------------------------------- #


class ProviderError(RuntimeError):
    """Raised when a provider call fails (network, auth, model missing...)."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class Provider:
    """Minimal interface every model adapter implements."""

    name: str = "provider"
    kind: str = "unknown"
    model: str = ""
    supports_tools: bool = True
    tool_mode: str = "native"     # "native" | "text" | "none"
    fallback: str | None = None

    def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> Completion:
        raise NotImplementedError

    def health(self) -> tuple[bool, str]:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        return []

    # -- helpers ----------------------------------------------------------- #
    def label(self) -> str:
        return f"{self.name} ({self.model})"


def chunks(seq: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
