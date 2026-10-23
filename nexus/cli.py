"""``nexus`` command line interface.

Subcommands
-----------
chat      interactive REPL with /commands            (default)
task      one-shot autonomous run (route -> tools -> done)
ask       one-shot read-only run (plan mode, no writes)
roles     list / search / show the team
agents    list configured model agents
pipeline  run a multi-role handoff pipeline
doctor    health check: keys, endpoints, models, tools, repo
serve     local HTTP + SSE server for the VS Code sidebar
panel     open the built-in browser panel
init      write .env.example / .nexus config into a project
prompt    print the system prompt a role would get
version   version info
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

from . import __version__
from .env import load_dotenv_files
from .agent import NexusAgent, RunResult, render_system_prompt
from .config import ConfigError, load_config
from .router import explain
from .tools import (
    ApprovalDecision,
    PathGuard,
    RoleMemory,
    find_blocked_pattern,
    gh_available,
    memory_for,
    preview_args,
    run_git,
)
from .types import Event, EventType, Risk, ToolSpec

# --------------------------------------------------------------------------- #
# tiny ansi renderer
# --------------------------------------------------------------------------- #


class C:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, t: str) -> str: return self._wrap("1", t)
    def dim(self, t: str) -> str: return self._wrap("2", t)
    def red(self, t: str) -> str: return self._wrap("31", t)
    def green(self, t: str) -> str: return self._wrap("32", t)
    def yellow(self, t: str) -> str: return self._wrap("33", t)
    def blue(self, t: str) -> str: return self._wrap("34", t)
    def magenta(self, t: str) -> str: return self._wrap("35", t)
    def cyan(self, t: str) -> str: return self._wrap("36", t)


def make_colors() -> C:
    disabled = (
        os.environ.get("NO_COLOR")
        or os.environ.get("NEXUS_NO_COLOR")
        or not sys.stdout.isatty()
    )
    return C(enabled=not disabled)


COLORS = make_colors()


def emit_console(event: Event) -> None:
    """Default CLI event renderer."""
    t = event.type
    d = event.data
    if t is EventType.TEXT:
        text = (d.get("text") or "").strip()
        if text:
            print(COLORS.green(text))
            print()
    elif t is EventType.STATUS:
        print(COLORS.dim(f"· {d.get('message', '')}"))
    elif t is EventType.ROLE:
        print(
            COLORS.cyan(
                f"⇄ role: {d.get('role')} -> agent: {d.get('agent') or '?'} "
                f"[{d.get('method')}]"
                + (f" hits: {', '.join(d.get('hits') or [])}" if d.get("hits") else "")
                + (f" ({d.get('note')})" if d.get("note") else "")
            )
        )
    elif t is EventType.AGENT:
        level = d.get("level", "info")
        color = COLORS.yellow if level == "warn" else COLORS.blue
        if d.get("error"):
            print(color(f"⚠ agent {d.get('agent')} failed: {d['error']}"))
        elif d.get("message"):
            print(color(f"⇢ {d['message']}"))
        else:
            mode = d.get("tool_mode")
            print(color(
                f"→ agent: {d.get('agent')}  model: {d.get('model')}  provider: {d.get('provider')}"
                + (f"  tools: {mode}" if mode and mode != "native" else "")
            ))
    elif t is EventType.TOOL_CALL:
        risk = d.get("risk", "write")
        mark = {"read": "·", "write": "✎", "danger": "⚠"}.get(risk, "•")
        print(COLORS.magenta(f"{mark} {d.get('tool')}  {COLORS.dim(str(d.get('preview', ''))[:300])}"))
    elif t is EventType.TOOL_RESULT:
        flag = COLORS.red("✗") if d.get("is_error") else COLORS.green("✓")
        preview = str(d.get("preview", "")).replace("\n", " ")[:220]
        print(f"  {flag} {COLORS.dim(preview)}")
    elif t is EventType.ERROR:
        print(COLORS.red(f"✗ {d.get('message', '')}"))
    elif t is EventType.MEMORY:
        print(COLORS.dim(f"🧠 saved memory for {d.get('role')} -> {d.get('path')}"))
    elif t is EventType.APPROVAL:
        pass  # handled by the interactive callback
    elif t is EventType.DONE:
        pass


# --------------------------------------------------------------------------- #
# approval prompt
# --------------------------------------------------------------------------- #


def interactive_approval(spec: ToolSpec, args: dict[str, Any], risk: Risk, preview: str) -> ApprovalDecision:
    banner = {"read": COLORS.dim("read"), "write": COLORS.yellow("write"),
              "danger": COLORS.red("DANGEROUS")}.get(risk.value, risk.value)
    print()
    print(COLORS.bold(f"⚠ approve {spec.name}?") + f"  [{banner}]")
    if preview:
        print(COLORS.dim("  " + preview.replace("\n", "\n  ")[:1500]))
    blocked = find_blocked_pattern(preview)
    if blocked and risk is Risk.DANGEROUS:
        print(COLORS.red(f"  matches deny-list pattern: {blocked}"))
    try:
        answer = input("  [y]es / [n]o / [a]lways-this-tool / [s]kip-tool-for-session > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return ApprovalDecision(False, reason="interrupted")
    if answer in {"y", "yes", ""}:
        return ApprovalDecision(True, scope="once")
    if answer in {"a", "always"}:
        return ApprovalDecision(True, scope="session")
    if answer in {"s", "session"}:
        return ApprovalDecision(False, scope="session", reason="user skipped this tool for the session")
    return ApprovalDecision(False, reason="user denied")


# --------------------------------------------------------------------------- #
# result rendering
# --------------------------------------------------------------------------- #


def print_result(result: RunResult, *, verbose: bool = True, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps({
            "ok": result.ok,
            "role": result.role,
            "agent": result.agent,
            "route_method": result.method,
            "turns": result.turns,
            "tool_calls": result.tool_calls,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "changed_files": result.changed_files,
            "summary": result.summary,
            "followups": result.followups,
            "stopped_reason": result.stopped_reason,
            "duration_s": round(result.duration_s, 2),
            "text": result.text,
        }, ensure_ascii=False, indent=2))
        return 0 if result.ok else 1

    if verbose:
        print()
        print(COLORS.dim("─" * 62))
        summary = result.summary or (result.text[:2000] if result.text else "(no summary)")
        print(COLORS.bold("summary: ") + summary)
        if result.followups:
            print(COLORS.bold("follow-ups:"))
            for item in result.followups:
                print(f"  - {item}")
        if result.changed_files:
            print(COLORS.bold("files touched:"))
            for path in result.changed_files:
                print(f"  {path}")
        stats = (
            f"role={result.role} agent={result.agent} route={result.method} turns={result.turns} "
            f"tools={result.tool_calls} tokens={result.usage.input_tokens}in/"
            f"{result.usage.output_tokens}out time={result.duration_s:.1f}s stop={result.stopped_reason or '-'}"
        )
        print(COLORS.dim(stats))
    return 0 if result.ok else 1


# --------------------------------------------------------------------------- #
# shared agent factory
# --------------------------------------------------------------------------- #


def build_agent(args: argparse.Namespace) -> NexusAgent:
    workspace = Path(args.workspace).resolve() if getattr(args, "workspace", None) else Path.cwd().resolve()
    try:
        config = load_config(workspace)
    except ConfigError as exc:
        print(COLORS.red(f"config error: {exc}"), file=sys.stderr)
        raise SystemExit(2)
    factory = getattr(args, "providers_factory", None)  # test/extension hook
    quiet = bool(getattr(args, "json", False))           # --json prints JSON only
    agent = NexusAgent(
        config,
        workspace=workspace,
        providers=factory(config) if factory else None,
        emit=(lambda _event: None) if quiet else (getattr(args, "emit", None) or emit_console),
        approval_callback=None if getattr(args, "yes", False) else interactive_approval,
        auto_yes=bool(getattr(args, "yes", False)),
        allow_outside=bool(getattr(args, "allow_outside", False)),
        allow_dangerous=bool(getattr(args, "allow_dangerous", False)),
    )
    if getattr(args, "plan", False) or getattr(args, "read_only", False):
        agent.plan_mode = True
        agent.gate.mode = "plan"
    if getattr(args, "demo", False):
        from .demo import patch_agent_for_demo

        patch_agent_for_demo(agent)
        if not quiet:
            print(COLORS.yellow("demo mode: scripted run, no model is called"))
    if getattr(args, "no_route", False):
        agent.auto_route = False
    if getattr(args, "role", None):
        agent.set_role(args.role)
        agent.auto_route = False
    return agent


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


HELP_TEXT = """\
Nexus REPL commands
  /help                     this help
  /role <name>              pin the active role (turns auto-routing off)
  /auto [on|off]            toggle automatic role routing
  /agent <name>             point the active role at another agent
  /model <id>               override the model for this session
  /ask <question>           read-only answer (no writes, no shell mutations)
  /plan <task>              same as /ask but framed as an implementation plan
  /task <task>              full autonomous run (tools enabled)
  /pipeline <name> <task>   run a multi-role handoff pipeline
  /roles [needle]           list / search the team
  /addagent <github-url>    import agents from a GitHub repo (then auto-reloads)
  /agents                   list configured agents + models
  /memory [role]            show saved role memory
  /remember <text>          save a note for the active role
  /system [role]            print the exact system prompt in use
  /tools                    list tools available to the active role
  /doctor                   health check
  /stats                    session stats (turns, tokens, tools)
  /compact                  trim old tool output from the context
  /clear                    clear conversation history
  /new                      clear history + role pin (fresh session)
  /save [file]              dump the transcript to a file
  /exit                     quit (also /quit, Ctrl-D)

Anything that does not start with '/' is sent to the agent as a chat turn.
"""


def cmd_chat(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    print(COLORS.bold("Nexus Agent") + COLORS.dim(f" v{__version__}"))
    print(COLORS.dim(f"workspace: {agent.ws}"))
    print(
        COLORS.dim(
            f"default agent: {agent.config.default_agent} | roles: {len(agent.config.roles)} | "
            f"auto-route: {'on' if agent.auto_route else 'off'} | approval: {agent.gate.mode}"
        )
    )
    print(COLORS.dim("type /help for commands, /exit to quit\n"))

    if args.message:
        result = agent.chat_turn(args.message, read_only=args.read_only)
        print_result(result, verbose=False)
        if not args.keep_open:
            return 0 if result.ok else 1

    while True:
        try:
            role_hint = agent.sticky_role or (f"auto:{agent.active_role.name}" if agent.auto_route else agent.active_role.name)
            line = input(COLORS.bold(f"nexus[{role_hint}] ❯ ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in {"/exit", "/quit", "/q"}:
            break
        if line.startswith("/"):
            if not handle_slash(agent, line):
                continue
            continue
        result = agent.chat_turn(line, read_only=args.read_only)
        print_result(result, verbose=False)
        if not result.ok:
            print(COLORS.red(f"run failed: {result.stopped_reason}"))
    return 0


def handle_slash(agent: NexusAgent, line: str) -> bool:
    """Returns True when the REPL should continue normally."""
    parts = line[1:].split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "help":
        print(HELP_TEXT)
    elif cmd == "role":
        if not arg:
            print(COLORS.dim(f"active role: {agent.active_role.name} (sticky={agent.sticky_role})"))
            print(COLORS.dim("usage: /role backend"))
        else:
            role = agent.set_role(arg)
            agent.auto_route = False
            print(COLORS.cyan(f"pinned role: {role.name} -> agent: {role.agent} (auto-routing off)"))
    elif cmd == "auto":
        wanted = arg.lower() not in {"off", "false", "0", "no"} if arg else not agent.auto_route
        agent.auto_route = wanted
        agent.sticky_role = None if wanted else agent.sticky_role
        print(COLORS.cyan(f"auto-routing: {'on' if wanted else 'off'}"))
    elif cmd == "agent":
        if not arg:
            for name, cfg in sorted(agent.config.agents.items()):
                marker = "*" if name == agent.config.default_agent else " "
                print(f" {marker} {name:<20} {cfg.provider:<18} {cfg.model}")
        else:
            try:
                agent.set_agent(arg)
                print(COLORS.cyan(f"active agent -> {arg}"))
            except Exception as exc:  # noqa: BLE001
                print(COLORS.red(str(exc)))
    elif cmd == "model":
        if not arg:
            print(COLORS.dim(f"model override: {agent.active_role.model or '(none)'}"))
        else:
            agent.active_role.model = arg
            print(COLORS.cyan(f"model for '{agent.active_role.name}' -> {arg}"))
    elif cmd in {"ask", "plan"}:
        if not arg:
            print(COLORS.red("usage: /ask <question>"))
            return True
        result = agent.chat_turn(arg, read_only=True)
        print_result(result, verbose=False)
    elif cmd == "task":
        if not arg:
            print(COLORS.red("usage: /task <description>"))
            return True
        result = agent.chat_turn(arg)
        print_result(result)
    elif cmd == "pipeline":
        bits = arg.split(maxsplit=1)
        if len(bits) < 2:
            known = ", ".join(sorted(agent.config.pipelines)) or "none"
            print(COLORS.red(f"usage: /pipeline <name> <task>   (defined: {known})"))
            return True
        results = agent.run_pipeline(bits[0], bits[1])
        for res in results:
            print_result(res, verbose=False)
            print(COLORS.dim(f"  ↳ step done: {res.role} ({res.tool_calls} tool calls)"))
        print_result(results[-1])
    elif cmd == "roles":
        print_roles(agent.config, arg)  # return code ignored inside the REPL
    elif cmd == "addagent":
        _slash_addagent(agent, arg)
    elif cmd == "agents":
        for name, cfg in sorted(agent.config.agents.items()):
            marker = "*" if name == agent.config.default_agent else " "
            print(
                f" {marker} {COLORS.bold(name):<28} {cfg.provider:<18} {cfg.model:<28} "
                f"tools={'native' if cfg.tool_mode == 'native' else cfg.tool_mode}"
                + (f" fallback={cfg.fallback}" if cfg.fallback else "")
            )
    elif cmd == "memory":
        target = arg or agent.active_role.name
        text = agent.memory.read(target)
        print(COLORS.bold(f"memory: {target}") + COLORS.dim(f" ({agent.memory.path_for(target)})"))
        print(text or COLORS.dim("(empty)"))
    elif cmd == "remember":
        if not arg:
            print(COLORS.red("usage: /remember <note>"))
            return True
        path = agent.memory.append(agent.active_role.name, arg)
        print(COLORS.dim(f"🧠 saved -> {path}"))
    elif cmd == "system":
        role = agent.config.role(arg) if arg else agent.active_role
        cfg = agent.config.agent_for_role(role)
        mem = agent.memory.read(role.name)
        print(
            render_system_prompt(
                role=role,
                agent_label=f"{cfg.name} / {cfg.model}",
                workspace=agent.ws,
                memory_text=mem,
                tools_hint="(tools listed at call time)",
                plan_mode=agent.plan_mode,
            )
        )
    elif cmd == "tools":
        _ctx, registry, specs = agent.tools_for(agent.active_role, read_only=agent.plan_mode)
        for spec in specs:
            print(f"  {spec.name:<16} [{spec.risk.value}] {spec.description.splitlines()[0]}")
    elif cmd == "doctor":
        cmd_doctor(_doctor_args(agent))
    elif cmd == "stats":
        usage = agent.session_usage
        print(
            COLORS.dim(
                f"history: {len(agent.history)} messages | tokens: {usage.input_tokens} in / "
                f"{usage.output_tokens} out over {usage.calls} calls | role: {agent.active_role.name}"
            )
        )
    elif cmd == "compact":
        agent._maybe_compact(agent.history)
        print(COLORS.dim(f"context now ~{sum(m.char_len() for m in agent.history):,} chars"))
    elif cmd == "clear":
        agent.history = []
        print(COLORS.dim("history cleared"))
    elif cmd == "new":
        agent.reset()
        print(COLORS.dim("fresh session (history, role pin and tool approvals reset)"))
    elif cmd == "save":
        target = Path(arg or f".nexus/transcript-{int(__import__('time').time())}.md")
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Nexus transcript", ""]
        for message in agent.history:
            if isinstance(message.content, str):
                lines += [f"## {message.role}", "", message.content, ""]
            else:
                lines.append(f"## {message.role}")
                lines.append("")
                for block in message.content:
                    lines.append(f"```json\n{json.dumps(block.__dict__, default=str, ensure_ascii=False)[:2000]}\n```")
                lines.append("")
        target.write_text("\n".join(lines), encoding="utf-8")
        print(COLORS.dim(f"transcript saved -> {target}"))
    else:
        print(COLORS.red(f"unknown command '/{cmd}' - try /help"))
    return True


def _doctor_args(agent: NexusAgent) -> argparse.Namespace:
    return argparse.Namespace(workspace=str(agent.ws), json=False, config=agent.config)


def _slash_addagent(agent: NexusAgent, arg: str) -> None:
    """/addagent <github-url> [--dest project|global|repo] [--agent NAME] [--limit N] [--force]"""
    from .importers import import_repo

    tokens = arg.split()
    url: str | None = None
    dest = "project"
    forced_agent: str | None = None
    limit: int | None = None
    force = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--dest" and i + 1 < len(tokens):
            dest = tokens[i + 1]
            i += 2
            continue
        if tok in {"--global", "-g"}:
            dest = "global"
            i += 1
            continue
        if tok == "--repo":
            dest = "repo"
            i += 1
            continue
        if tok in {"--force", "-f"}:
            force = True
            i += 1
            continue
        if tok == "--agent" and i + 1 < len(tokens):
            forced_agent = tokens[i + 1]
            i += 2
            continue
        if tok == "--limit" and i + 1 < len(tokens):
            try:
                limit = int(tokens[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if url is None and not tok.startswith("-"):
            url = tok
        i += 1

    if not url:
        print(COLORS.red("usage: /addagent <github-url> [--dest project|global|repo] [--agent NAME] [--force]"))
        return

    taken = set(agent.config.roles.names())
    for role in agent.config.roles.roles.values():
        taken.update(role.aliases)

    def log(msg: str) -> None:
        print(COLORS.dim(f"  {msg}"))

    try:
        result = import_repo(
            url, workspace=agent.ws, dest_kind=dest, forced_agent=forced_agent,
            limit=limit, force=force, taken=taken, log=log,
        )
    except Exception as exc:  # noqa: BLE001
        print(COLORS.red(f"✗ import failed: {exc}"))
        return

    if not result.imported:
        for note in result.notes:
            print(COLORS.yellow(f"  {note}"))
        print(COLORS.red(f"no agents imported from {url}"))
        return

    print(COLORS.green(f"✓ imported {result.imported} agent(s) from {result.repo} -> {result.dest}"))
    total = agent.reload_config()
    print(COLORS.cyan(f"  {total} roles now available - just describe a task, Nexus routes to the right one"))


def print_roles(config, needle: str = "") -> int:
    registry = config.roles
    if needle:
        matches = registry.search(needle)
        if not matches:
            print(COLORS.red(f"no role matches '{needle}'"))
            return 1
        for role in matches:
            print(f"  {role.name:<24} {role.category:<12} -> {role.agent:<20} {role.title}")
        return 0
    files = registry.sources or []
    where = ", ".join(str(Path(f).name) for f in files) or "built-in"
    print(COLORS.bold(f"{len(registry)} roles  ({where})"))
    for category, roles in registry.by_category().items():
        print(COLORS.cyan(f"\n[{category}] ({len(roles)})"))
        for role in roles:
            kws = ", ".join(k.text for k in role.keywords[:6])
            print(f"  {role.name:<26} -> {role.agent:<20} {COLORS.dim(kws)}")
    return 0


# --------------------------------------------------------------------------- #
# one-shot commands
# --------------------------------------------------------------------------- #


def cmd_task(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    task = args.task or _read_stdin_task()
    if not task:
        print(COLORS.red("no task given (pass it as an argument or via stdin)"), file=sys.stderr)
        return 2
    result = agent.run(task, role=args.role, read_only=args.read_only, max_turns=args.max_turns)
    return print_result(result, as_json=args.json)


def cmd_ask(args: argparse.Namespace) -> int:
    args.read_only = True
    return cmd_task(args)


def _read_stdin_task() -> str:
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read().strip()


def cmd_roles(args: argparse.Namespace) -> int:
    config = load_config(Path(args.workspace).resolve() if args.workspace else Path.cwd())
    if args.json:
        payload = [r.to_dict() for r in config.roles.roles.values()]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.show:
        role = config.role(args.show)
        cfg = config.agent_for_role(role)
        print(COLORS.bold(f"{role.name}") + COLORS.dim(f"  [{role.category}]"))
        print(f"  title:    {role.title or '-'}")
        print(f"  agent:    {role.agent or cfg.name} ({cfg.model})")
        print(f"  keywords: {', '.join(k.text for k in role.keywords) or '-'}")
        print(f"  aliases:  {', '.join(role.aliases) or '-'}")
        if role.focus:
            print(f"  focus:    {', '.join(role.focus)}")
        print("  persona:")
        for line in (role.persona or "(none)").splitlines():
            print(f"    {line}")
        return 0
    if args.category:
        wanted = args.category.lower()
        for category, roles in config.roles.by_category().items():
            if category == wanted:
                for role in roles:
                    print(f"  {role.name:<26} -> {role.agent}")
                return 0
        print(COLORS.red(f"no category '{args.category}'"))
        return 1
    return print_roles(config, args.search or "")


def cmd_agents(args: argparse.Namespace) -> int:
    config = load_config(Path(args.workspace).resolve() if args.workspace else Path.cwd())
    for name, cfg in sorted(config.agents.items()):
        marker = "*" if name == config.default_agent else " "
        roles = [r.name for r in config.roles.roles.values() if (r.agent or config.default_agent) == name]
        print(f"{marker} {COLORS.bold(name)}")
        print(f"    provider : {cfg.provider}")
        print(f"    model    : {cfg.model}")
        print(f"    base_url : {cfg.base_url or '(sdk default)'}")
        print(f"    api key  : {cfg.api_key_env or '(inline)'} {'✓ set' if cfg.resolved_api_key else '✗ missing'}")
        print(f"    tools    : {cfg.tool_mode}{' (supports_tools=false)' if not cfg.supports_tools else ''}")
        print(f"    fallback : {cfg.fallback or '-'}")
        print(f"    roles    : {len(roles)} ({', '.join(roles[:6])}{'…' if len(roles) > 6 else ''})")
    return 0


def _list_packs(workspace: Path) -> int:
    """Show every installed roles.d pack and how many roles it contributes."""
    from .config import candidate_role_dirs

    found = False
    for directory in candidate_role_dirs(workspace):
        if not directory.is_dir():
            continue
        packs = sorted(p for p in directory.iterdir() if p.suffix.lower() in {".yaml", ".yml"} and p.is_file())
        if not packs:
            continue
        print(COLORS.bold(f"{directory}"))
        for pack in packs:
            count: object = "?"
            try:
                data = yaml.safe_load(pack.read_text(encoding="utf-8")) or {}
                count = len(data.get("roles") or {})
            except Exception:  # noqa: BLE001
                pass
            print(f"  {pack.name:<34} {count} roles")
            found = True
    if not found:
        print(COLORS.dim("no imported packs yet - run: nexus addagent <github-url>"))
    return 0


def cmd_addagent(args: argparse.Namespace) -> int:
    """Import agents/skills from a GitHub repo as Nexus roles (no code change)."""
    from .importers import import_repo

    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()
    if args.list:
        return _list_packs(workspace)
    if not args.url:
        print(COLORS.red("usage: nexus addagent <github-url> [--dest project|global|repo] [--agent NAME] [--force]"))
        print(COLORS.dim("       nexus addagent --list"))
        return 2

    config = load_config(workspace)
    taken = set(config.roles.names())
    for role in config.roles.roles.values():
        taken.update(role.aliases)

    def log(msg: str) -> None:
        if not args.json:
            print(COLORS.dim(f"  {msg}"))

    try:
        result = import_repo(
            args.url,
            workspace=workspace,
            dest_kind=args.dest,
            forced_agent=args.agent,
            category_prefix=args.category,
            limit=args.limit or None,
            dry_run=args.dry_run,
            force=args.force,
            taken=taken,
            log=log,
        )
    except Exception as exc:  # noqa: BLE001
        print(COLORS.red(f"✗ import failed: {exc}"))
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "repo": result.repo,
                    "url": result.url,
                    "discovered": result.discovered,
                    "imported": result.imported,
                    "skipped": result.skipped,
                    "dest": str(result.dest) if result.dest else None,
                    "dry_run": result.dry_run,
                    "notes": result.notes,
                    "roles": [r["name"] for r in result.roles],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if result.imported else 1

    verb = "would import" if result.dry_run else "imported"
    print(COLORS.bold(f"\n{result.repo}: discovered {result.discovered}, {verb} {result.imported}"))
    if result.skipped:
        print(COLORS.dim(f"  skipped {result.skipped} (limit or name collision)"))
    for note in result.notes:
        print(COLORS.dim(f"  {note}"))
    if result.imported:
        sample = ", ".join(r["name"] for r in result.roles[:8])
        print(COLORS.dim(f"  e.g. {sample}{' …' if result.imported > 8 else ''}"))
        if result.dry_run:
            print(COLORS.yellow("  dry run - nothing written (drop --dry-run to save)"))
        elif result.dest and result.dest.exists():
            print(COLORS.green(f"✓ wrote {result.dest}"))
            print(COLORS.dim("  live now: nexus roles -s <name>  ·  in the REPL, /addagent reloads automatically"))
    return 0 if result.imported else 1


def cmd_pipeline(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    if args.list or not args.name:
        for name, pipe in sorted(agent.config.pipelines.items()):
            steps = " -> ".join(str(s.get("role", "?")) for s in pipe.steps)
            print(f"  {COLORS.bold(name):<22} {steps}")
            if pipe.description:
                print(COLORS.dim(f"      {pipe.description}"))
        return 0 if args.list else 2
    task = args.task or _read_stdin_task()
    if not task:
        print(COLORS.red("pipeline needs a task"), file=sys.stderr)
        return 2
    results = agent.run_pipeline(args.name, task, max_turns=args.max_turns)
    for res in results:
        print_result(res, verbose=False)
        print(COLORS.dim(f"  ↳ {res.role}: {res.tool_calls} tool calls, {res.turns} turns"))
    if results:
        return print_result(results[-1], as_json=args.json)
    return 1


def cmd_prompt(args: argparse.Namespace) -> int:
    config = load_config(Path(args.workspace).resolve() if args.workspace else Path.cwd())
    role = config.role(args.role)
    cfg = config.agent_for_role(role)
    memory = RoleMemory(Path(args.workspace or ".").resolve(), config.settings.memory_dir).read(role.name)
    print(
        render_system_prompt(
            role=role,
            agent_label=f"{cfg.name} / {cfg.model}",
            workspace=Path(args.workspace or ".").resolve(),
            memory_text=memory,
            tools_hint="(injected at call time)",
        )
    )
    return 0


# --------------------------------------------------------------------------- #
# doctor (Phase 8)
# --------------------------------------------------------------------------- #


def cmd_doctor(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool | None, detail: str, hint: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "hint": hint})

    try:
        config = load_config(workspace)
        add("config", True, f"loaded from {', '.join(str(p) for p in config.sources['agents']) or 'built-in defaults'}")
    except ConfigError as exc:
        add("config", False, str(exc), "fix the YAML and re-run `nexus doctor`")
        _render_doctor(checks, args)
        return 1

    add("workspace", True, str(workspace))
    add("roles", len(config.roles) > 0, f"{len(config.roles)} roles registered",
        "add roles to roles.yaml or .nexus/roles.yaml")
    add("git repo", (workspace / ".git").exists(),
        "inside a git repository" if (workspace / ".git").exists() else "not a git repository",
        "git tools need a repository: `git init`")
    add("gh cli", gh_available(), shutil.which("gh") or "not installed",
        "install from https://cli.github.com - needed for PR tools")

    from .providers import build_providers

    providers = build_providers(config.agents)
    for name in sorted(config.agents):
        cfg = config.agents[name]
        try:
            provider = providers.get(name)
            ok, detail = provider.health()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, str(exc)
        hint = ""
        if not ok:
            if cfg.provider == "anthropic":
                hint = f"export {cfg.api_key_env or 'ANTHROPIC_API_KEY'}=sk-ant-..."
            elif (cfg.base_url or "").startswith(("http://localhost", "http://127.0.0.1")):
                hint = f"start the local server (e.g. `ollama serve`) and pull the model: `ollama pull {cfg.model}`"
            else:
                hint = f"endpoint {cfg.base_url} unreachable - check the URL, the key ({cfg.api_key_env or 'inline'}) and your network"
        add(f"agent:{name}", ok, f"{cfg.provider} {cfg.model} - {detail}", hint)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    add("github token", bool(token), "GITHUB_TOKEN set" if token else "GITHUB_TOKEN not set",
        "only needed for the REST fallback when `gh` is missing")

    guard = PathGuard(workspace)
    add("memory dir", True, str(workspace / config.settings.memory_dir),
        "created on first `remember` call")

    _, code = run_git(["rev-parse", "--abbrev-ref", "HEAD"], workspace)
    branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], workspace)[0].strip()
    add("git branch", code == 0, branch or "unknown")

    exit_code = _render_doctor(checks, args)
    return exit_code


def _render_doctor(checks: list[dict[str, Any]], args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    else:
        print(COLORS.bold("nexus doctor"))
        failures = 0
        for check in checks:
            if check["ok"] is None:
                mark = COLORS.dim("?")
            elif check["ok"]:
                mark = COLORS.green("✓")
            else:
                mark = COLORS.red("✗")
                failures += 1
            print(f"  {mark} {check['name']:<16} {check['detail']}")
            if not check["ok"] and check.get("hint"):
                print(COLORS.dim(f"      ↳ {check['hint']}"))
        print()
        if failures:
            print(COLORS.yellow(f"{failures} check(s) failed - the agent will fall back where it can."))
        else:
            print(COLORS.green("all good."))
    return 1 if any(c["ok"] is False for c in checks) else 0


# --------------------------------------------------------------------------- #
# serve / panel (Phase 5)
# --------------------------------------------------------------------------- #


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()
    return serve(host=args.host, port=args.port, workspace=workspace, token=args.token,
                 auto_yes=args.yes, quiet=args.quiet, demo=getattr(args, "demo", False))


def cmd_panel(args: argparse.Namespace) -> int:
    """Serve the built-in web panel (same UI the VS Code sidebar loads)."""
    from .server import serve

    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()
    print(COLORS.dim(f"panel: http://{args.host}:{args.port}/panel  (workspace {workspace})"))
    return serve(host=args.host, port=args.port, workspace=workspace, token=args.token,
                 auto_yes=args.yes, quiet=args.quiet, demo=getattr(args, "demo", False),
                 open_panel=True)


def cmd_init(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()
    created: list[str] = []
    env_example = workspace / ".env.example"
    if not env_example.exists() or args.force:
        env_example.write_text(
            "# Nexus Agent configuration\n"
            "ANTHROPIC_API_KEY=\n"
            "GITHUB_TOKEN=\n"
            "# optional overrides\n"
            "# NEXUS_DEFAULT_AGENT=claude\n"
            "# NEXUS_ROUTER_AGENT=claude-cheap\n"
            "# NEXUS_AUTO_ROUTE=true\n"
            "# NEXUS_YES=false\n"
            "# OLLAMA_HOST=http://localhost:11434\n",
            encoding="utf-8",
        )
        created.append(".env.example")
    env_file = workspace / ".env"
    if not env_file.exists():
        env_file.write_text("ANTHROPIC_API_KEY=\nGITHUB_TOKEN=\n", encoding="utf-8")
        created.append(".env")
    nexus_dir = workspace / ".nexus"
    (nexus_dir / "memory").mkdir(parents=True, exist_ok=True)
    roles_local = nexus_dir / "roles.yaml"
    if not roles_local.exists() or args.force:
        roles_local.write_text(
            "# Project-local specialists. These MERGE with the bundled roles.yaml.\n"
            "roles:\n"
            "  repo-historian:\n"
            "    title: Repository Historian\n"
            "    category: docs\n"
            "    agent: hermes\n"
            "    persona: >-\n"
            "      You know this repository's conventions by heart. Answer from the actual\n"
            "      files and git history, never from generic advice.\n"
            "    keywords:\n"
            "      - convention\n"
            "      - history\n"
            "      - why was\n",
            encoding="utf-8",
        )
        created.append(".nexus/roles.yaml")
    vscode = workspace / ".vscode"
    vscode.mkdir(exist_ok=True)
    bundled_dir = Path(__file__).resolve().parent / "bundled"
    for filename, source in (
        ("tasks.json", "vscode-tasks.json"),
        ("launch.json", "vscode-launch.json"),
        ("settings.json", "vscode-settings.json"),
    ):
        target = vscode / filename
        bundled = bundled_dir / source
        if (not target.exists() or args.force) and bundled.is_file():
            target.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")
            created.append(f".vscode/{filename}")
    print(COLORS.green(f"initialized nexus in {workspace}") if created else COLORS.dim("nothing to do"))
    for item in created:
        print(f"  + {item}")
    return 0


def cmd_version(_args: argparse.Namespace) -> int:
    print(f"nexus {__version__} (python {sys.version.split()[0]})")
    return 0


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #


def _workspace_from_argv(argv: Sequence[str]) -> Path:
    """Best-effort peek at --workspace/-C before argparse runs (for .env loading)."""
    for i, token in enumerate(argv):
        if token in {"--workspace", "-C"} and i + 1 < len(argv):
            return Path(argv[i + 1]).expanduser().resolve()
        if token.startswith("--workspace="):
            return Path(token.split("=", 1)[1]).expanduser().resolve()
    return Path.cwd()


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", "-C", help="project directory (default: cwd)")
    parser.add_argument("--role", "-r", help="force a role (skips routing)")
    parser.add_argument("--yes", "-y", action="store_true", help="auto-approve non-dangerous tool calls")
    parser.add_argument("--plan", action="store_true", help="read-only plan mode (no writes, no mutations)")
    parser.add_argument("--allow-outside", action="store_true", help="allow file paths outside the workspace")
    parser.add_argument("--allow-dangerous", action="store_true",
                        help="allow commands that match the deny-list (still asks)")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument("--demo", action="store_true",
                        help="scripted offline run (no API key, no local model needed)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus",
        description="Nexus Agent - one CLI, a whole team of routed specialist agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  nexus chat\n"
            "  nexus task 'add pagination to the /users API endpoint and write tests'\n"
            "  nexus ask 'explain how auth works in this repo' --role backend\n"
            "  nexus pipeline ship-feature 'add a rate limiter to the public API'\n"
            "  nexus roles --search security\n"
            "  nexus doctor\n"
        ),
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("chat", help="interactive REPL (default)")
    add_common(p)
    p.add_argument("message", nargs="*", help="optional first message")
    p.add_argument("--read-only", action="store_true", help="start the REPL in plan mode")
    p.add_argument("--no-route", action="store_true", help="disable auto-routing")
    p.add_argument("--keep-open", action="store_true", help="stay in the REPL after the first message")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("task", help="one-shot autonomous run")
    add_common(p)
    p.add_argument("task", nargs="*", help="the task (or pass it on stdin)")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--no-route", action="store_true")
    p.add_argument("--max-turns", type=int, default=0)
    p.set_defaults(func=cmd_task)

    p = sub.add_parser("ask", help="one-shot read-only question (plan mode)")
    add_common(p)
    p.add_argument("task", nargs="*")
    p.add_argument("--no-route", action="store_true")
    p.add_argument("--max-turns", type=int, default=0)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("roles", help="list / search / inspect roles")
    p.add_argument("--workspace", "-C")
    p.add_argument("--search", "-s")
    p.add_argument("--show")
    p.add_argument("--category", "-c")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_roles)

    p = sub.add_parser("agents", help="list configured model agents")
    p.add_argument("--workspace", "-C")
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("addagent", help="import agents/skills from a GitHub repo as roles")
    p.add_argument("url", nargs="?", help="github repo, .../tree/<ref>/<dir> or .../blob/<ref>/<file>.md")
    p.add_argument("--workspace", "-C")
    p.add_argument("--dest", default="project", choices=["project", "global", "repo"],
                   help="where to write the pack: project (.nexus/roles.d), global (~/.config/nexus), repo (committed)")
    p.add_argument("--agent", help="force every imported role onto this agent (default: auto by category)")
    p.add_argument("--category", help="prefix imported categories with this slug")
    p.add_argument("--limit", type=int, default=0, help="import at most N agents")
    p.add_argument("--dry-run", action="store_true", help="discover + convert, write nothing")
    p.add_argument("--force", action="store_true", help="overwrite an existing pack for this repo")
    p.add_argument("--list", action="store_true", help="show installed packs and role counts")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_addagent)

    p = sub.add_parser("pipeline", help="run a multi-role pipeline")
    add_common(p)
    p.add_argument("name", nargs="?")
    p.add_argument("task", nargs="*")
    p.add_argument("--list", action="store_true")
    p.add_argument("--max-turns", type=int, default=0)
    p.add_argument("--no-route", action="store_true")
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("doctor", help="health check")
    p.add_argument("--workspace", "-C")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("serve", help="HTTP+SSE server for the VS Code sidebar")
    p.add_argument("--workspace", "-C")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--token", default=os.environ.get("NEXUS_SERVE_TOKEN", ""))
    p.add_argument("--yes", "-y", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--demo", action="store_true", help="scripted runs (no API key needed)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("panel", help="serve the built-in browser panel")
    p.add_argument("--workspace", "-C")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--token", default=os.environ.get("NEXUS_SERVE_TOKEN", ""))
    p.add_argument("--yes", "-y", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--demo", action="store_true", help="scripted runs (no API key needed)")
    p.set_defaults(func=cmd_panel)

    p = sub.add_parser("init", help="write .env / .nexus / .vscode scaffolding into a project")
    p.add_argument("--workspace", "-C")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("prompt", help="print the system prompt for a role")
    p.add_argument("role", nargs="?", default="general")
    p.add_argument("--workspace", "-C")
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("version", help="print version")
    p.set_defaults(func=cmd_version)

    return parser


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    """argparse gives us lists for nargs='*'; the commands want strings."""
    for attr in ("task", "message"):
        value = getattr(args, attr, None)
        if isinstance(value, list):
            setattr(args, attr, " ".join(str(v) for v in value).strip())
    if getattr(args, "max_turns", None) in (None, ""):
        args.max_turns = 0
    return args


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    workspace = _workspace_from_argv(argv)
    load_dotenv_files(workspace)
    parser = build_parser()
    if not argv or argv[0].startswith("-") and argv[0] not in {"--version"}:
        argv = ["chat", *argv]
    if "--version" in argv and len(argv) == 1:
        print(f"nexus {__version__}")
        return 0
    args = normalize_args(parser.parse_args(argv))
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    try:
        return int(func(args) or 0)
    except KeyboardInterrupt:
        print(COLORS.yellow("\ninterrupted"))
        return 130
    except ConfigError as exc:
        print(COLORS.red(f"config error: {exc}"), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
