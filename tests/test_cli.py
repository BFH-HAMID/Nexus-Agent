"""CLI surface: argument handling, offline commands and the approval prompt."""

from __future__ import annotations

import argparse
import io
import json
import sys

import pytest

import nexus.cli as cli
from nexus.tools import ApprovalDecision

from .conftest import FakeProvider, fake_registry, finish_completion, text_completion, tool_completion


def ns(**kwargs) -> argparse.Namespace:
    base = dict(
        workspace=None, role=None, yes=False, plan=False, allow_outside=False,
        allow_dangerous=False, json=False, providers_factory=None, emit=None,
        max_turns=0, read_only=False, no_route=False, message="", keep_open=False,
        list=False, search=None, show=None, category=None, force=False, name=None,
    )
    base.update(kwargs)
    return cli.normalize_args(argparse.Namespace(**base))


@pytest.fixture(autouse=True)
def _no_color(monkeypatch):
    monkeypatch.setattr(cli, "COLORS", cli.C(enabled=False))


def run(argv, **kwargs) -> tuple[int, str]:
    buffer = io.StringIO()
    old = sys.stdout
    sys.stdout = buffer
    try:
        code = cli.main(argv)
    finally:
        sys.stdout = old
    return code, buffer.getvalue()


# --------------------------------------------------------------------------- #
# offline, provider-free commands
# --------------------------------------------------------------------------- #


def test_version():
    code, out = run(["version"])
    assert code == 0 and out.startswith("nexus ")


def test_roles_list(repo):
    code, out = run(["roles", "-C", str(repo)])
    assert code == 0
    assert "[development]" in out and "[security]" in out
    assert "pentesting" in out


def test_roles_search(repo):
    code, out = run(["roles", "-C", str(repo), "--search", "security"])
    assert code == 0
    assert "security-audit" in out


def test_roles_search_misses(repo):
    code, out = run(["roles", "-C", str(repo), "--search", "zzzzz"])
    assert code == 1 and "no role matches" in out


def test_roles_show(repo):
    code, out = run(["roles", "-C", str(repo), "--show", "pentesting"])
    assert code == 0
    assert "openrouter-uncensored" in out
    assert "offensive-security specialist" in out


def test_roles_json_is_machine_readable(repo):
    code, out = run(["roles", "-C", str(repo), "--json"])
    assert code == 0
    payload = json.loads(out)
    assert len(payload) >= 100
    assert {"name", "persona", "agent", "keywords"} <= set(payload[0])


def test_roles_category_filter(repo):
    code, out = run(["roles", "-C", str(repo), "--category", "trading"])
    assert code == 0
    assert "trading" in out and "quant-research" in out
    code, out = run(["roles", "-C", str(repo), "--category", "nope"])
    assert code == 1


def test_agents_lists_the_team(repo):
    code, out = run(["agents", "-C", str(repo)])
    assert code == 0
    assert "gemini-3.8-flash" in out
    assert "dolphin-mistral-24b-venice-edition" in out
    assert "deepseek-flash" in out


def test_prompt_command_prints_the_persona(repo):
    code, out = run(["prompt", "backend", "-C", str(repo)])
    assert code == 0
    assert "senior Backend Engineer" in out
    assert "# Active role: backend" in out
    assert str(repo) in out


def test_doctor_runs_offline(repo):
    code, out = run(["doctor", "-C", str(repo)])
    assert "nexus doctor" in out
    assert "✓ roles" in out
    assert "agent:gemini" in out
    assert "key:gemini" in out
    assert code == 1  # no GEMINI_API_KEY / DEEPSEEK_API_KEY / OPENROUTER_API_KEY in the sandbox
    assert "check(s) failed" in out


def test_doctor_json(repo):
    code, out = run(["doctor", "-C", str(repo), "--json"])
    payload = json.loads(out)
    names = {check["name"] for check in payload}
    assert {"config", "roles", "agent:gemini", "key:gemini", "gh cli", "git repo"} <= names


def test_pipeline_list(repo):
    code, out = run(["pipeline", "--list", "-C", str(repo)])
    assert code == 0
    assert "ship-feature" in out and "backend -> qa" in out


def test_init_writes_scaffolding(tmp_path):
    target = tmp_path / "fresh"
    target.mkdir()
    code, out = run(["init", "-C", str(target)])
    assert code == 0
    # keys live in the terminal store, so no .env is ever written
    assert not (target / ".env").exists()
    assert not (target / ".env.example").exists()
    assert (target / ".nexus" / "roles.yaml").is_file()
    assert (target / ".vscode" / "tasks.json").is_file()
    assert "nexus keys set" in out
    # idempotent
    code2, out2 = run(["init", "-C", str(target)])
    assert code2 == 0 and "nothing to do" in out2


def test_help_and_no_args(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "usage: nexus" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# commands that need a provider (injected)
# --------------------------------------------------------------------------- #


def factory_for(*providers):
    registry = fake_registry(*providers)
    return lambda config: registry


def test_task_runs_and_prints_a_summary(repo, capsys):
    provider = FakeProvider("deepseek", [           # docs is routed to the cheap deepseek agent
        tool_completion([("read_file", {"path": "README.md"})], text="reading"),
        finish_completion("documented the project", ["publish it"]),
    ])
    args = ns(workspace=str(repo), yes=True, task=["update the readme and the api reference"],
              providers_factory=factory_for(provider))
    code = cli.cmd_task(args)
    out = capsys.readouterr().out
    assert code == 0
    assert "role: docs" in out
    assert "documented the project" in out
    assert "publish it" in out
    assert "files touched" not in out


def test_task_with_injected_provider(repo, monkeypatch):
    provider = FakeProvider("gemini", [finish_completion("wrote the endpoint")])
    monkeypatch.setattr(cli, "build_parser", cli.build_parser)  # keep parser real
    args = ns(workspace=str(repo), yes=True, task=["add an api endpoint for profiles"],
              providers_factory=factory_for(provider))
    code = cli.cmd_task(args)
    assert code == 0
    assert provider.calls
    assert "senior Backend Engineer" in provider.calls[0]["system"]


def test_task_json_output(repo):
    provider = FakeProvider("gemini", [finish_completion("done and dusted")])
    args = ns(workspace=str(repo), yes=True, json=True, task=["polish the query builder module"],
              providers_factory=factory_for(provider))
    buffer = io.StringIO()
    old, sys.stdout = sys.stdout, buffer
    try:
        code = cli.cmd_task(args)
    finally:
        sys.stdout = old
    assert code == 0
    payload = json.loads(buffer.getvalue())
    assert payload["summary"] == "done and dusted"
    assert payload["route_method"] == "keyword"
    assert payload["ok"] is True


def test_ask_is_read_only(repo):
    provider = FakeProvider("gemini", [
        tool_completion([("write_file", {"path": "sneaky.py", "content": "x"})]),
        finish_completion("answered"),
    ])
    args = ns(workspace=str(repo), yes=True, task=["explain the auth flow"],
              providers_factory=factory_for(provider))
    buffer = io.StringIO()
    old, sys.stdout = sys.stdout, buffer
    try:
        code = cli.cmd_ask(args)
    finally:
        sys.stdout = old
    assert code == 0
    assert not (repo / "sneaky.py").exists()
    assert "write_file" not in provider.calls[0]["tools"]


def test_task_failure_returns_nonzero(repo):
    from nexus.types import ProviderError

    provider = FakeProvider("gemini", [ProviderError("boom", retryable=False)])
    args = ns(workspace=str(repo), yes=True, task=["do something"],
              providers_factory=factory_for(provider))
    buffer = io.StringIO()
    old, sys.stdout = sys.stdout, buffer
    try:
        code = cli.cmd_task(args)
    finally:
        sys.stdout = old
    assert code == 1


def test_forced_role_flag(repo):
    provider = FakeProvider("gemini", [finish_completion("ok")])
    args = ns(workspace=str(repo), yes=True, role="database", task=["whatever"],
              providers_factory=factory_for(provider))
    buffer = io.StringIO()
    old, sys.stdout = sys.stdout, buffer
    try:
        cli.cmd_task(args)
    finally:
        sys.stdout = old
    assert "Database Engineer/DBA" in provider.calls[0]["system"]


def test_pipeline_command(repo):
    provider = FakeProvider("gemini", [finish_completion("step done")] * 6)
    args = ns(workspace=str(repo), yes=True, name="security-review",
              task=["review the auth layer"], providers_factory=factory_for(provider))
    buffer = io.StringIO()
    old, sys.stdout = sys.stdout, buffer
    try:
        code = cli.cmd_pipeline(args)
    finally:
        sys.stdout = old
    assert code == 0
    assert "step done" in buffer.getvalue()


# --------------------------------------------------------------------------- #
# REPL slash commands + approval prompt
# --------------------------------------------------------------------------- #


def agent_for(repo, *providers, **kwargs):
    from nexus.agent import NexusAgent
    from nexus.config import load_config

    return NexusAgent(load_config(repo), workspace=repo, providers=fake_registry(*providers),
                      emit=lambda e: None, auto_yes=kwargs.get("auto_yes", True))


def test_slash_help_and_roles(repo, capsys):
    agent = agent_for(repo, FakeProvider("gemini", []))
    assert cli.handle_slash(agent, "/help")
    assert "Nexus REPL commands" in capsys.readouterr().out
    cli.handle_slash(agent, "/roles security")
    assert "security-audit" in capsys.readouterr().out


def test_slash_role_pins_and_disables_auto_routing(repo, capsys):
    agent = agent_for(repo, FakeProvider("gemini", []))
    cli.handle_slash(agent, "/role backend")
    out = capsys.readouterr().out
    assert "pinned role: backend" in out
    assert agent.sticky_role == "backend"
    assert agent.auto_route is False


def test_slash_unknown_role_falls_back(repo, capsys):
    agent = agent_for(repo, FakeProvider("gemini", []))
    cli.handle_slash(agent, "/role not-a-role")
    assert agent.active_role.name == "general"


def test_slash_agents_and_tools_and_system(repo, capsys):
    agent = agent_for(repo, FakeProvider("gemini", []))
    cli.handle_slash(agent, "/agents")
    out = capsys.readouterr().out
    assert "gemini-3.8-flash" in out
    cli.handle_slash(agent, "/tools")
    out = capsys.readouterr().out
    assert "read_file" in out and "write_file" in out
    cli.handle_slash(agent, "/system backend")
    assert "senior Backend Engineer" in capsys.readouterr().out


def test_slash_memory_roundtrip(repo, capsys):
    agent = agent_for(repo, FakeProvider("gemini", []))
    cli.handle_slash(agent, "/remember always use httpx here")
    cli.handle_slash(agent, "/memory general")
    out = capsys.readouterr().out
    assert "always use httpx here" in out


def test_slash_model_override(repo):
    agent = agent_for(repo, FakeProvider("gemini", []))
    cli.handle_slash(agent, "/model gemini-4-argon")
    assert agent.active_role.model == "gemini-4-argon"


def test_slash_clear_new_compact_stats(repo, capsys):
    provider = FakeProvider("gemini", [text_completion("hi")])
    agent = agent_for(repo, provider)
    agent.chat_turn("please continue")
    assert agent.history
    cli.handle_slash(agent, "/stats")
    assert "history: 2 messages" in capsys.readouterr().out
    cli.handle_slash(agent, "/compact")
    cli.handle_slash(agent, "/clear")
    assert agent.history == []
    cli.handle_slash(agent, "/new")
    assert agent.sticky_role is None


def test_slash_save_transcript(repo, tmp_path, capsys):
    provider = FakeProvider("gemini", [text_completion("hi")])
    agent = agent_for(repo, provider)
    agent.chat_turn("please continue")
    target = tmp_path / "t.md"
    cli.handle_slash(agent, f"/save {target}")
    assert target.is_file()
    assert "please continue" in target.read_text()


def test_slash_ask_uses_read_only(repo):
    provider = FakeProvider("gemini", [
        tool_completion([("write_file", {"path": "nope.py", "content": "x"})]),
        text_completion("answer"),
    ])
    agent = agent_for(repo, provider)
    cli.handle_slash(agent, "/ask what does main.py do?")
    assert not (repo / "nope.py").exists()
    assert "write_file" not in provider.calls[0]["tools"]


def test_slash_task_runs_with_tools(repo):
    provider = FakeProvider("gemini", [
        tool_completion([("write_file", {"path": "yes.py", "content": "x = 1\n"})]),
        finish_completion("created yes.py"),
    ])
    agent = agent_for(repo, provider)
    cli.handle_slash(agent, "/task create yes.py")
    assert (repo / "yes.py").is_file()


def test_slash_unknown_command(repo, capsys):
    agent = agent_for(repo, FakeProvider("gemini", []))
    cli.handle_slash(agent, "/wat")
    assert "unknown command" in capsys.readouterr().out


@pytest.mark.parametrize(
    "answer,approved,scope",
    [("y", True, "once"), ("", True, "once"), ("yes", True, "once"),
     ("n", False, "once"), ("no", False, "once"),
     ("a", True, "session"), ("always", True, "session"),
     ("s", False, "session")],
)
def test_interactive_approval(monkeypatch, answer, approved, scope):
    from nexus.types import Risk, ToolSpec

    monkeypatch.setattr("builtins.input", lambda *_a: answer)
    spec = ToolSpec(name="write_file", description="d", input_schema={"type": "object"}, risk=Risk.WRITE)
    decision = cli.interactive_approval(spec, {"path": "a.py", "content": "x"}, Risk.WRITE, "a.py (1 chars)")
    assert decision.approved is approved
    assert decision.scope == scope


def test_interactive_approval_handles_eof(monkeypatch):
    from nexus.types import Risk, ToolSpec

    def boom(*_a):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    spec = ToolSpec(name="run_command", description="d", input_schema={"type": "object"}, risk=Risk.WRITE)
    decision = cli.interactive_approval(spec, {"command": "ls"}, Risk.WRITE, "ls")
    assert not decision.approved


def test_denied_tool_call_message_reaches_the_model(repo):
    def deny(spec, args, risk, preview):
        return ApprovalDecision(False, reason="not today")

    from nexus.agent import NexusAgent
    from nexus.config import load_config

    provider = FakeProvider("gemini", [
        tool_completion([("write_file", {"path": "blocked.py", "content": "x"})]),
        finish_completion("respecting the denial"),
    ])
    agent = NexusAgent(load_config(repo), workspace=repo, providers=fake_registry(provider),
                       emit=lambda e: None, approval_callback=deny)
    result = agent.run("create blocked.py")
    assert not (repo / "blocked.py").exists()
    denied = [m for m in result.messages if isinstance(m.content, list)
              and any("DENIED" in getattr(b, "content", "") for b in m.content)]
    assert denied
    assert result.summary == "respecting the denial"


def test_emit_console_does_not_crash_on_any_event_type(capsys):
    from nexus.types import Event, EventType

    for kind in EventType:
        cli.emit_console(Event(kind, {"text": "t", "message": "m", "tool": "x", "preview": "p",
                                      "role": "backend", "agent": "gemini", "method": "keyword",
                                      "model": "m", "is_error": False, "risk": "write", "level": "warn",
                                      "error": "e", "hits": ["a"], "note": "n", "path": "p"}))
    assert capsys.readouterr().out


def test_main_defaults_to_chat_when_no_subcommand(monkeypatch):
    """`nexus` and `nexus -r backend "do x"` both land in the chat REPL."""
    seen = {}

    def fake_chat(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli, "cmd_chat", fake_chat)
    real_parser = cli.build_parser

    def patched_parser():
        parser = real_parser()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                action.choices["chat"].set_defaults(func=fake_chat)
        return parser

    monkeypatch.setattr(cli, "build_parser", patched_parser)
    assert cli.main([]) == 0
    assert seen["args"].command == "chat"

    seen.clear()
    assert cli.main(["-r", "backend", "add an endpoint"]) == 0
    args = seen["args"]
    assert args.command == "chat"
    assert args.role == "backend"
    assert args.message == "add an endpoint"


def test_main_reports_config_errors(tmp_path, capsys):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "agents.yaml").write_text("agents: [not, a, mapping]\n", encoding="utf-8")
    code = cli.main(["roles", "-C", str(broken)])
    assert code == 2
    assert "config error" in capsys.readouterr().err


def test_dotenv_loading(tmp_path, monkeypatch):
    from nexus.env import load_dotenv_files, parse_env_text

    (tmp_path / ".env").write_text(
        "# comment\nANTHROPIC_API_KEY=sk-ant-test\nexport GITHUB_TOKEN='ghp_x'\nEMPTY=\n", encoding="utf-8"
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    loaded = load_dotenv_files(tmp_path)
    assert "ANTHROPIC_API_KEY" in loaded
    assert json.dumps(parse_env_text((tmp_path / ".env").read_text()))
    import os

    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert os.environ["GITHUB_TOKEN"] == "ghp_x"
    assert "EMPTY" not in os.environ
