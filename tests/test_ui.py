"""Terminal UI: banner, streaming renderer, footer, local-model view."""

from __future__ import annotations

import argparse

import nexus.cli as cli
import nexus.ui as ui
from nexus.types import Event, EventType

from .conftest import FakeProvider, fake_registry, text_completion


def _colorless() -> cli.C:
    return cli.C(enabled=False)


def test_renderer_accepts_every_event_type(capsys):
    renderer = ui.TerminalUI(_colorless(), spinner=False)
    for kind in EventType:
        renderer(Event(kind, {
            "text": "hi", "message": "m", "tool": "read_file", "preview": "p", "risk": "read",
            "role": "backend", "agent": "gemini", "method": "keyword", "model": "m",
            "is_error": False, "level": "info", "error": "", "hits": ["api"], "note": "",
            "input_tokens": 10, "output_tokens": 5, "turn": 1, "max_turns": 8,
        }))
    assert "hi" in capsys.readouterr().out


def test_streaming_deltas_are_not_printed_twice(capsys):
    renderer = ui.TerminalUI(_colorless(), spinner=False)
    renderer(Event(EventType.TEXT_DELTA, {"text": "Hello "}))
    renderer(Event(EventType.TEXT_DELTA, {"text": "world"}))
    renderer(Event(EventType.TEXT, {"text": "Hello world", "streamed": True}))
    out = capsys.readouterr().out
    assert out.count("Hello world") == 1
    assert "┌" in out and "└" in out


def test_banner_rows_share_one_width(repo, capsys):
    from nexus.agent import NexusAgent
    from nexus.config import load_config

    agent = NexusAgent(load_config(repo), workspace=repo, emit=lambda e: None,
                       providers=fake_registry(FakeProvider("gemini", [])), auto_yes=True)
    ui.banner(_colorless(), agent, version="9.9.9", workspace=str(repo))
    out = capsys.readouterr().out
    rows = [line for line in out.splitlines() if line.startswith(("│", "╭", "├", "╰"))]
    assert rows and len({len(row) for row in rows}) == 1
    assert "9.9.9" in out


def test_footer_reports_time_tools_and_tokens(capsys):
    from nexus.agent import RunResult
    from nexus.types import Usage

    result = RunResult(turns=3, tool_calls=2, duration_s=1.25, role="backend", agent="gemini",
                       usage=Usage(input_tokens=900, output_tokens=120, calls=3),
                       changed_files=["a.py"])
    ui.print_footer(_colorless(), result)
    out = capsys.readouterr().out
    assert "1.2s" in out and "3 turns" in out and "2 tools" in out and "1.0k tok" in out


def test_help_text_lists_the_new_commands():
    text = ui.help_text(_colorless())
    for command in ("/gui", "/local", "/addagent", "/pipeline", "/exit"):
        assert command in text
    assert "Nexus REPL commands" in text


class _StreamingProvider(FakeProvider):
    """A provider that honours the on_text callback (like the real adapters)."""

    def complete(self, system, messages, tools=None, *, max_tokens=None, temperature=None,
                 model=None, on_text=None):
        completion = super().complete(system, messages, tools, max_tokens=max_tokens,
                                      temperature=temperature, model=model)
        if on_text is not None and completion.text:
            for piece in completion.text.split(" "):
                on_text(piece + " ")
        return completion


def test_agent_emits_text_delta_events(repo):
    from nexus.agent import NexusAgent
    from nexus.config import load_config

    provider = _StreamingProvider("gemini", [text_completion("streamed answer")])
    agent = NexusAgent(load_config(repo), workspace=repo, providers=fake_registry(provider),
                       emit=lambda e: None, auto_yes=True)
    events: list[Event] = []
    agent.emit = events.append
    result = agent.run("hello there")
    assert result.ok
    deltas = [e for e in events if e.type is EventType.TEXT_DELTA]
    assert deltas and "".join(e.data["text"] for e in deltas) == "streamed answer "
    final = [e for e in events if e.type is EventType.TEXT][-1]
    assert final.data["streamed"] is True


def test_agent_does_not_stream_when_settings_disable_it(repo, monkeypatch):
    from nexus.agent import NexusAgent
    from nexus.config import load_config

    monkeypatch.setenv("NEXUS_STREAM", "0")
    provider = _StreamingProvider("gemini", [text_completion("quiet answer")])
    agent = NexusAgent(load_config(repo), workspace=repo, providers=fake_registry(provider),
                       emit=lambda e: None, auto_yes=True)
    events: list[Event] = []
    agent.emit = events.append
    agent.run("hello there")
    assert not [e for e in events if e.type is EventType.TEXT_DELTA]


def test_local_command_is_offline_safe(capsys):
    code = cli.cmd_local(argparse.Namespace(host="http://127.0.0.1:9", pull=None, json=False))
    out = capsys.readouterr().out
    assert code == 1
    assert "hermes3:8b" in out and "ollama-uncensored" in out
    assert "not running" in out


def test_slash_gui_starts_a_threaded_dashboard(repo, monkeypatch, capsys):
    from nexus.agent import NexusAgent
    from nexus.config import load_config

    started: dict = {}

    def fake_serve(**kwargs):
        started.update(kwargs)
        return 0

    import nexus.server as server_module

    monkeypatch.setattr(server_module, "serve", fake_serve)
    agent = NexusAgent(load_config(repo), workspace=repo, providers=fake_registry(FakeProvider("gemini", [])),
                       emit=lambda e: None, auto_yes=True)
    url = cli._start_dashboard(agent, port=8791)
    assert url.endswith(":8791/dashboard")
    assert "GUI dashboard" in capsys.readouterr().out
    assert started.get("port") == 8791
