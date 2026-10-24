"""Shared test fixtures - everything here runs offline with scripted providers."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nexus.providers import ProviderRegistry  # noqa: E402
from nexus.types import Completion, Provider, TextBlock, ToolCall, ToolSpec  # noqa: E402

GIT = shutil.which("git")


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Keep provider retry backoff from slowing the suite down."""
    import nexus.providers as providers

    monkeypatch.setattr(providers.time, "sleep", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _isolate_committed_role_packs(monkeypatch):
    """Tests run against the curated core roster, not the committed imports.

    The repo ships ~850 imported agents under ``roles.d/`` (see ``nexus
    addagent``). Those are content, not behaviour, and they change whenever a
    pack is regenerated - so routing/agent tests must not inherit them. We drop
    only the *committed* pack dirs (repo checkout + bundled wheel); workspace
    local ``.nexus/roles.d`` still loads, which keeps the drop-in mechanism
    itself testable with a throwaway pack.
    """
    import nexus.config as config

    real = config.candidate_role_dirs
    blocked = {config.REPO_ROOT / config.DEFAULT_ROLES_DIR, config.BUNDLED_CONFIG / config.DEFAULT_ROLES_DIR}

    def filtered(workspace):
        return [p for p in real(workspace) if Path(p) not in blocked]

    monkeypatch.setattr(config, "candidate_role_dirs", filtered)


@pytest.fixture(autouse=True)
def _isolated_nexus_home(tmp_path_factory, monkeypatch):
    """Never touch the developer's real ~/.config/nexus (keys, memory, sessions)."""
    home = tmp_path_factory.mktemp("nexus-home")
    monkeypatch.setenv("NEXUS_HOME", str(home))
    monkeypatch.setenv("NEXUS_LEARNING", "0")     # no extra model calls in unit tests
    return home


# --------------------------------------------------------------------------- #
# scripted provider
# --------------------------------------------------------------------------- #


class FakeProvider(Provider):
    """A provider that replays a script instead of calling a model."""

    kind = "fake"

    def __init__(self, name: str = "fake", script: Sequence[Any] = (), *, model: str = "fake-model",
                 tool_mode: str = "native", supports_tools: bool = True, healthy: bool = True,
                 health_detail: str = "ok", fallback: str | None = None):
        self.name = name
        self.model = model
        self.script: list[Any] = list(script)
        self.tool_mode = tool_mode
        self.supports_tools = supports_tools
        self.fallback = fallback
        self._healthy = healthy
        self._health_detail = health_detail
        self.calls: list[dict[str, Any]] = []

    def complete(self, system, messages, tools=None, *, max_tokens=None, temperature=None, model=None):
        self.calls.append(
            {
                "system": system,
                "messages": [m for m in messages],
                "tools": [t.name for t in (tools or [])],
                "max_tokens": max_tokens,
                "temperature": temperature,
                "model": model,
            }
        )
        if not self.script:
            return text_completion("(script exhausted)")
        item = self.script.pop(0)
        if callable(item):
            return item(self.calls[-1])
        if isinstance(item, Exception):
            from nexus.types import ProviderError

            raise item if isinstance(item, ProviderError) else ProviderError(str(item))
        if isinstance(item, Completion):
            return item
        return text_completion(str(item))

    def health(self):
        return (self._healthy, self._health_detail)


def text_completion(text: str, *, model: str = "fake-model", stop_reason: str = "end_turn") -> Completion:
    return Completion(content=[TextBlock(text=text)], stop_reason=stop_reason, model=model,
                      input_tokens=10, output_tokens=5)


def tool_completion(calls: Sequence[tuple[str, dict[str, Any]]], *, text: str = "") -> Completion:
    content: list[Any] = []
    if text:
        content.append(TextBlock(text=text))
    for index, (name, args) in enumerate(calls):
        content.append(ToolCall(id=f"toolu_{index}_{name}", name=name, input=args))
    return Completion(content=content, stop_reason="tool_use", model="fake-model",
                      input_tokens=20, output_tokens=15)


def finish_completion(summary: str = "done", followups: Sequence[str] = ()) -> Completion:
    return tool_completion([("finish", {"summary": summary, "followups": list(followups)})])


ROUTER_AGENT = "gemini-flash"


def fake_registry(*providers: FakeProvider, config=None, router_script: Sequence[Any] = ()) -> ProviderRegistry:
    """A provider registry for tests.

    * every agent named in ``config`` (or the bundled agents.yaml) gets a
      provider, so routing to deepseek/openrouter never explodes mid-test;
    * ``gemini-flash`` - the agent agents.yaml assigns to the routing
      classifier - answers "general" by default so it never eats a scripted
      item meant for the worker provider.
    """
    from nexus.config import AgentConfig, load_config

    cfg = config or load_config()
    agents: dict[str, AgentConfig] = dict(cfg.agents)
    factories: dict[str, FakeProvider] = {}
    for provider in providers:
        agents[provider.name] = AgentConfig(
            name=provider.name, provider="anthropic", model=provider.model,
            api_key="test", tool_mode=provider.tool_mode,
            supports_tools=provider.supports_tools, fallback=provider.fallback,
        )
        factories[provider.name] = provider

    for name, agent_cfg in agents.items():
        if name in factories:
            continue
        if name == ROUTER_AGENT:
            script = list(router_script) or [text_completion("general")] * 8
        else:
            # side agents answer "nothing to do" and never touch the script
            script = [text_completion(f"({name} has no scripted answer)")] * 8
        factories[name] = FakeProvider(name, script, model=agent_cfg.model,
                                       tool_mode=agent_cfg.tool_mode,
                                       supports_tools=agent_cfg.supports_tools)
    return ProviderRegistry(agents, factories=factories)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A tiny throwaway git repo with a couple of source files."""
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "app").mkdir()
    (ws / "app" / "main.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (ws / "app" / "api.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get('/users')\ndef users():\n    return []\n",
        encoding="utf-8",
    )
    (ws / "README.md").write_text("# demo project\n", encoding="utf-8")
    if GIT:
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
        subprocess.run([GIT, "init", "-q", "-b", "main"], cwd=ws, check=False, env={**env})
        subprocess.run([GIT, "add", "-A"], cwd=ws, check=False, env=env)
        subprocess.run([GIT, "commit", "-q", "-m", "init"], cwd=ws, check=False, env=env)
    return ws


@pytest.fixture()
def config(repo: Path):
    from nexus.config import load_config

    return load_config(repo)


@pytest.fixture()
def auto_approve():
    """Approval callback that says yes to everything (tests only)."""

    def cb(spec: ToolSpec, args: dict[str, Any], risk, preview: str):
        from nexus.tools import ApprovalDecision

        return ApprovalDecision(True, scope="once")

    return cb


@pytest.fixture()
def deny_all():
    def cb(spec: ToolSpec, args: dict[str, Any], risk, preview: str):
        from nexus.tools import ApprovalDecision

        return ApprovalDecision(False, reason="test denied")

    return cb


@pytest.fixture()
def events() -> list[dict[str, Any]]:
    """Collects emitted events for assertions."""
    seen: list[dict[str, Any]] = []

    def handler(event):
        seen.append(event.to_json())

    seen_handler = handler
    seen_handler.log = seen  # type: ignore[attr-defined]
    return seen  # usage: events list; use `make_agent` fixtures to attach


def emit_to(bucket: list[dict[str, Any]]) -> Callable[[Any], None]:
    def handler(event) -> None:
        bucket.append(event.to_json())

    return handler
