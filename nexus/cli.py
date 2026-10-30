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
gui       open the GUI dashboard (same as `panel`)
panel     open the GUI dashboard in a browser
keys      set / list / test API keys (no .env file needed)
models    list the model ids a provider actually offers
init      write .nexus / .vscode scaffolding into a project
prompt    print the system prompt a role would get
version   version info
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from . import keys as keyring
from .agent import NexusAgent, RunResult, render_system_prompt
from .config import AgentConfig, ConfigError, load_config
from .env import load_dotenv_files
from .tools import (
    ApprovalDecision,
    RoleMemory,
    find_blocked_pattern,
    gh_available,
    run_git,
)
from .types import Event, EventType, Risk, ToolSpec
from .ui import TerminalUI, help_text, print_footer
from .ui import banner as render_banner

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
    elif t is EventType.THINK:
        turn = d.get("turn")
        total = d.get("max_turns")
        print(COLORS.dim(
            f"◉ thinking [{d.get('agent')}/{d.get('model')}]"
            + (f"  turn {turn}/{total}" if turn else "")
        ))
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
# API keys - entered in the terminal, no .env file required
# --------------------------------------------------------------------------- #

KEY_FAMILIES: tuple[str, ...] = tuple(keyring.PROVIDERS)
_CHOICE_INDEX = {str(i): fam for i, fam in enumerate(KEY_FAMILIES, 1)}
_CHOICE_INDEX.update({fam: fam for fam in KEY_FAMILIES})
_CHOICE_INDEX.update({"g": "gemini", "google": "gemini", "d": "deepseek", "ds": "deepseek",
                      "or": "openrouter", "o": "openrouter"})


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _resolve_family(raw: str | None) -> str | None:
    """Accept a name or a menu number; None when it cannot be resolved."""
    if not raw:
        return None
    token = str(raw).strip().lower()
    if token in _CHOICE_INDEX:
        return _CHOICE_INDEX[token]
    return keyring.canonical(token)


def _key_row(family: str) -> dict[str, Any]:
    spec = keyring.PROVIDERS[family]
    value, origin = keyring.key_from_env(family)
    if not value:
        stored = keyring.stored_key(family)
        if stored:
            value, origin = stored, str(keyring.store_path())
    return {
        "provider": family,
        "label": spec.label,
        "configured": bool(value),
        "masked": keyring.mask(value),
        "source": origin,
        "env": spec.env[0],
        "console": spec.console_url,
        "command": spec.command,
        "default_model": spec.default_model,
    }


def _choose_provider(prompt: str = "  provider > ") -> str | None:
    print(COLORS.bold("Which provider?"))
    for index, family in enumerate(KEY_FAMILIES, 1):
        spec = keyring.PROVIDERS[family]
        print(f"  [{index}] {spec.label:<14} {COLORS.dim(spec.console_url)}")
    choice = _ask(prompt)
    if not choice or choice.lower() in {"s", "skip", "q", "n"}:
        return None
    family = _resolve_family(choice)
    if family is None:
        print(COLORS.red(f"unknown provider '{choice}' (choose {', '.join(KEY_FAMILIES)})"))
    return family


def _prompt_and_save_key(family: str, *, value: str = "", session_only: bool = False) -> str:
    """Ask for one key in the terminal and (by default) persist it."""
    spec = keyring.PROVIDERS[family]
    print(COLORS.bold(f"{spec.label} API key") + COLORS.dim(f"  - get one at {spec.console_url}"))
    if not value:
        if sys.stdin.isatty():
            value = keyring.read_secret("  paste key (hidden) > ")
        else:
            value = keyring.normalize_key(sys.stdin.readline())
    value = keyring.normalize_key(value)
    if not value:
        print(COLORS.red("  no key entered - nothing saved"))
        return ""
    warning = keyring.looks_like_key(family, value)
    if warning:
        print(COLORS.yellow(f"  ! {warning}"))
    if session_only:
        os.environ.setdefault(spec.env[0], value)
        print(COLORS.green(f"✓ {family} key {keyring.mask(value)} set for this session only"))
        return value
    path = keyring.save_key(family, value)
    print(COLORS.green(f"✓ saved {family} key {keyring.mask(value)}"))
    print(COLORS.dim(f"  store: {path} (chmod 600, outside the repo)"))
    return value


def maybe_prompt_for_keys(config, args: argparse.Namespace) -> None:
    """First-run helper: offer to paste a key instead of making a .env file."""
    if (
        getattr(args, "no_key_prompt", False)
        or getattr(args, "json", False)
        or getattr(args, "demo", False)      # demo is offline by design - never ask
    ):
        return
    if os.environ.get("NEXUS_NO_PROMPT") or not sys.stdin.isatty():
        return
    families = config.key_families()
    if not families or any(keyring.get_key(f) for f in families):
        return
    print()
    print(COLORS.yellow("No API key found.") + COLORS.dim(" Nexus runs on Gemini, DeepSeek or OpenRouter."))
    family = _choose_provider("  set a key now? [1-3, s = skip] > ")
    if family is None:
        print(COLORS.dim("  skipped - later: nexus keys set gemini   (or export GEMINI_API_KEY=...)"))
        print()
        return
    _prompt_and_save_key(family)
    default_cfg = config.agents.get(config.default_agent)
    if default_cfg is not None and default_cfg.key_family and default_cfg.key_family != family:
        print(COLORS.dim(
            f"  note: default agent '{default_cfg.name}' uses {default_cfg.key_family}; "
            f"agents without a key fall back to {family}"
        ))
    print()


def cmd_keys(args: argparse.Namespace) -> int:
    action = (args.action or "list").lower()
    if action in {"list", "ls", "show"}:
        return cmd_keys_list(args)
    if action in {"set", "add", "login"}:
        return cmd_keys_set(args)
    if action in {"remove", "rm", "delete", "unset"}:
        return cmd_keys_remove(args)
    if action in {"test", "check", "verify"}:
        return cmd_keys_test(args)
    if action == "path":
        print(keyring.store_path())
        return 0
    print(COLORS.red(f"unknown action '{action}' (list | set | remove | test | path)"))
    return 2


def cmd_keys_list(args: argparse.Namespace) -> int:
    rows = [_key_row(family) for family in KEY_FAMILIES]
    if getattr(args, "json", False):
        print(json.dumps({
            "store": str(keyring.store_path()),
            "providers": rows,
        }, ensure_ascii=False, indent=2))
        return 0

    print(COLORS.bold("Nexus API keys") + COLORS.dim("   (no .env file needed)"))
    for row in rows:
        if row["configured"]:
            mark = COLORS.green("✓")
            detail = f"{row['masked']:<18} {COLORS.dim(row['source'])}"
        else:
            mark = COLORS.dim("·")
            detail = f"{'not set':<18} {COLORS.dim('run: ' + row['command'])}"
        print(f"  {mark} {row['provider']:<11} {detail}")
        if not row["configured"]:
            print(COLORS.dim(f"      key: {row['console']}"))
    print()
    print(COLORS.dim(f"store: {keyring.store_path()}"
                     + ("" if keyring.store_is_private() else "  ⚠ readable by other users - chmod 600 it")))
    print(COLORS.dim("env:   "
                     + "  ".join(spec.env[0] for spec in keyring.PROVIDERS.values())
                     + "   (exported variables win over the store)"))
    return 0


def cmd_keys_set(args: argparse.Namespace) -> int:
    family = _resolve_family(getattr(args, "provider", None))
    if getattr(args, "provider", None) and family is None:
        print(COLORS.red(f"unknown provider '{args.provider}' (choose {', '.join(KEY_FAMILIES)})"))
        return 2
    if family is None:
        if not sys.stdin.isatty():
            print(COLORS.red("usage: nexus keys set gemini|deepseek|openrouter "
                             "[--key ... | --session]"), file=sys.stderr)
            return 2
        family = _choose_provider()
        if family is None:
            print(COLORS.dim("nothing to do"))
            return 0
    value = _prompt_and_save_key(family, value=getattr(args, "key", "") or "",
                                 session_only=bool(getattr(args, "session", False)))
    if not value:
        return 1
    print(COLORS.dim(f"next: nexus keys test {family}   ·   nexus agents   ·   nexus task '...'"))
    return 0


def cmd_keys_remove(args: argparse.Namespace) -> int:
    family = _resolve_family(getattr(args, "provider", None))
    if family is None:
        print(COLORS.red(f"usage: nexus keys remove {'|'.join(KEY_FAMILIES)}"), file=sys.stderr)
        return 2
    if keyring.delete_key(family):
        print(COLORS.green(f"✓ removed the stored {family} key"))
    else:
        print(COLORS.dim(f"no stored {family} key (an exported {keyring.PROVIDERS[family].env[0]} "
                         "still takes precedence)"))
    return 0


def cmd_keys_test(args: argparse.Namespace) -> int:
    from .providers import OpenAICompatProvider

    requested = _resolve_family(getattr(args, "provider", None))
    if getattr(args, "provider", None) and requested is None:
        print(COLORS.red(f"unknown provider '{args.provider}' (choose {', '.join(KEY_FAMILIES)})"))
        return 2
    families = [requested] if requested else [f for f in KEY_FAMILIES if keyring.get_key(f)]
    if not families:
        print(COLORS.yellow("no keys configured yet - run `nexus keys set gemini`"))
        return 1
    failures = 0
    for family in families:
        spec = keyring.PROVIDERS[family]
        cfg = AgentConfig(name=family, provider=family, model=spec.default_model,
                          api_key_env=spec.env[0], timeout=30.0)
        try:
            ok, detail = OpenAICompatProvider(cfg).health()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{exc.__class__.__name__}: {exc}"
        if ok:
            print(f"  {COLORS.green('✓')} {family:<11} {detail}")
        else:
            failures += 1
            print(f"  {COLORS.red('✗')} {family:<11} {detail}")
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# shared agent factory
# --------------------------------------------------------------------------- #


def build_agent(args: argparse.Namespace) -> NexusAgent:
    workspace = Path(args.workspace).resolve() if getattr(args, "workspace", None) else Path.cwd().resolve()
    try:
        config = load_config(workspace)
    except ConfigError as exc:
        print(COLORS.red(f"config error: {exc}"), file=sys.stderr)
        raise SystemExit(2) from None
    factory = getattr(args, "providers_factory", None)  # test/extension hook
    if factory is None and not getattr(args, "defer_key_prompt", False):
        maybe_prompt_for_keys(config, args)
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




def cmd_chat(args: argparse.Namespace) -> int:
    # one renderer for the whole session: streaming + spinner + footers
    ui = TerminalUI(COLORS, stream=True)
    if getattr(args, "emit", None) is None:
        args.emit = ui
    args.defer_key_prompt = True          # banner first, key prompt after it
    agent = build_agent(args)
    if getattr(args, "json", False):
        agent.emit = lambda _event: None

    if not getattr(args, "json", False):
        render_banner(COLORS, agent, version=__version__, workspace=str(agent.ws))
    maybe_prompt_for_keys(agent.config, args)

    if args.message:
        result = agent.chat_turn(args.message, read_only=args.read_only)
        print_footer(COLORS, result)
        if not args.keep_open:
            return 0 if result.ok else 1

    while True:
        try:
            role_hint = agent.sticky_role or (
                f"auto:{agent.active_role.name}" if agent.auto_route else agent.active_role.name
            )
            line = input(ui.prompt(role_hint)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in {"/exit", "/quit", "/q"}:
            break
        if line.startswith("/"):
            handle_slash(agent, line)
            continue
        result = agent.chat_turn(line, read_only=args.read_only)
        print_footer(COLORS, result)
        if not result.ok:
            print(COLORS.red(f"run stopped: {result.stopped_reason}"))
    print(COLORS.dim("bye · nexus"))
    return 0


def handle_slash(agent: NexusAgent, line: str) -> bool:
    """Returns True when the REPL should continue normally."""
    parts = line[1:].split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "help":
        print(help_text(COLORS))
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
    elif cmd in {"gui", "dashboard", "panel"}:
        port = int(arg.strip() or 8787) if arg.strip().isdigit() else 8787
        _start_dashboard(agent, port=port)
    elif cmd in {"local", "ollama"}:
        bits = arg.split()
        pull = None
        want = [b for b in bits if not b.startswith("-")]
        if "--pull" in bits or "-p" in bits:
            pull = want[0] if want else None
        cmd_local(argparse.Namespace(host=None, pull=pull, json=False))
    elif cmd == "roles":
        print_roles(agent.config, arg)  # return code ignored inside the REPL
    elif cmd == "addagent":
        _slash_addagent(agent, arg)
    elif cmd == "keys":
        bits = arg.split(maxsplit=1)
        if bits and bits[0].lower() in {"set", "add", "login"}:
            family = _resolve_family(bits[1] if len(bits) > 1 else None) or _choose_provider()
            if family:
                _prompt_and_save_key(family)
        elif bits and bits[0].lower() in {"remove", "rm", "delete"}:
            cmd_keys_remove(argparse.Namespace(provider=bits[1] if len(bits) > 1 else None))
        elif bits and bits[0].lower() in {"test", "check"}:
            cmd_keys_test(argparse.Namespace(provider=bits[1] if len(bits) > 1 else None))
        else:
            cmd_keys_list(argparse.Namespace(json=False))
    elif cmd == "models":
        cmd_models(argparse.Namespace(agent=arg or None, workspace=str(agent.ws), json=False))
    elif cmd == "agents":
        for name, cfg in sorted(agent.config.agents.items()):
            marker = "*" if name == agent.config.default_agent else " "
            print(
                f" {marker} {COLORS.bold(name):<28} {cfg.provider:<18} {cfg.model:<28} "
                f"tools={'native' if cfg.tool_mode == 'native' else cfg.tool_mode}"
                + (f" fallback={cfg.fallback}" if cfg.fallback else "")
            )
    elif cmd == "memory":
        if arg.split()[0:1] and arg.split()[0].lower() in {"global", "brain", "long"}:
            store = agent.memory_store
            for block in (store.memory, store.user):
                print(COLORS.bold(f"{block.name} ({block.chars}/{block.limit})")
                      + COLORS.dim(f"  {block.path}"))
                print(block.render() or COLORS.dim("  (empty)"))
            return True
        target = arg or agent.active_role.name
        text = agent.memory.read(target)
        print(COLORS.bold(f"memory: {target}") + COLORS.dim(f" ({agent.memory.path_for(target)})"))
        print(text or COLORS.dim("(empty)"))
    elif cmd in {"skills", "skill"}:
        from . import skills as skilllib

        library = skilllib.discover(agent.ws)
        if cmd == "skill" and arg:
            bits = arg.split(maxsplit=1)
            skill = skilllib.get(bits[0], agent.ws)
            if skill is None:
                print(COLORS.red(f"no skill named '{bits[0]}' - try /skills"))
            elif len(bits) > 1:
                agent.inject_skill(skill.name)
                print(COLORS.cyan(f"skill '{skill.name}' active for this task"))
                result = agent.chat_turn(bits[1])
                print_result(result)
            else:
                print(COLORS.bold(skill.name) + COLORS.dim(f"  {skill.path}"))
                print(skill.body)
        elif not library:
            print(COLORS.yellow("no skills yet - /learn <what we just did> or `nexus skills import`"))
        else:
            print(COLORS.bold(f"{len(library)} skills") + COLORS.dim(" (load: /<skill-name>, or /skill <name> <task>)"))
            for skill in library[:60]:
                uses = skill.usage().get("uses", 0)
                print(f"  {COLORS.cyan(skill.name):<34} {COLORS.dim(skill.prompt_description)}"
                      + (COLORS.dim(f"  ({uses}x)") if uses else ""))
    elif cmd == "learn":
        brief = arg or "the work we just did in this session"
        print(COLORS.dim("· reviewing the session and writing a skill…"))
        outcome = agent.learning.learn_from_brief(brief, history=agent.history)
        if outcome.changed:
            print(COLORS.green(f"✓ learned: {outcome.summary()}"))
            if outcome.skill:
                print(COLORS.dim(f"  {outcome.skill_path}"))
        else:
            print(COLORS.yellow(f"nothing saved: {outcome.summary()}"))
        for note in outcome.notes[:4]:
            print(COLORS.dim(f"  {note}"))
    elif cmd == "curate":
        from . import skills as skilllib

        report = skilllib.curator_report(agent.ws)
        print(COLORS.bold(f"curator: {report['total']} skills")
              + COLORS.dim(f" · stale {len(report['stale'])} · never used {len(report['unused'])}"))
        for row in report["stale"][:12]:
            print(f"  {row['name']:<32} {row['last_used'] or 'never'}")
        if arg in {"apply", "archive", "yes"}:
            archived = skilllib.archive_stale(agent.ws)
            print(COLORS.green(f"archived {len(archived)} skills (nothing deleted)"))
        elif report["stale"]:
            print(COLORS.dim("  /curate apply to archive them"))
    elif cmd == "recall":
        rows = agent.sessions.search(arg, limit=6) if arg else agent.sessions.recent(6)
        if not rows:
            print(COLORS.dim("no past sessions recorded yet"))
        for row in rows:
            from .sessions import format_hit

            print(format_hit(row, width=96))
    elif cmd == "hermes":
        cmd_hermes(argparse.Namespace(action=(arg.split()[0] if arg else "status"), argv=[], json=False))
    elif cmd == "update":
        cmd_update(argparse.Namespace(no_pull=False, no_install=False))
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
        # Hermes-style shortcut: /<skill-name> [task] runs that skill
        from . import skills as skilllib

        skill = skilllib.get(cmd, agent.ws)
        if skill is None:
            print(COLORS.red(f"unknown command '/{cmd}' - try /help"))
        else:
            agent.inject_skill(skill.name)
            print(COLORS.cyan(f"skill '{skill.name}' active"))
            if arg:
                print_result(agent.chat_turn(arg))
            else:
                print(skill.body)
    return True


def _start_dashboard(agent: NexusAgent, *, port: int = 8787, host: str = "127.0.0.1") -> str:
    """Serve the GUI dashboard from this process, without blocking the REPL.

    ``/gui`` is the shortcut for people who live in the terminal: one command,
    the URL appears, the browser opens if this machine has one.
    """
    import socket
    import threading
    import webbrowser

    from .server import serve

    existing = getattr(agent, "_nexus_gui_port", None)
    if existing:
        url = f"http://{host}:{existing}/dashboard"
        print(COLORS.cyan(f"dashboard already running → {url}"))
        return url

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        if probe.connect_ex((host, port)) == 0:
            url = f"http://{host}:{port}/dashboard"
            print(COLORS.yellow(f"port {port} is already serving something - opening {url}"))
            print(COLORS.dim("  if that is not Nexus, run /gui 8790 with a free port"))
            agent._nexus_gui_port = port
            return url

    def run() -> None:
        try:
            serve(host=host, port=port, workspace=agent.ws, quiet=True)
        except Exception as exc:  # noqa: BLE001 - a bad port must not kill the REPL
            print(COLORS.red(f"dashboard failed to start: {exc}"))

    threading.Thread(target=run, name="nexus-dashboard", daemon=True).start()
    url = f"http://{host}:{port}/dashboard"
    for _ in range(40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.15)
            if probe.connect_ex((host, port)) == 0:
                break
        time.sleep(0.05)
    agent._nexus_gui_port = port
    print()
    print(COLORS.bold("GUI dashboard") + COLORS.green(f"  {url}"))
    print(COLORS.dim(f"  live routing · streaming output · approvals · run history  (port {port})"))
    try:
        if webbrowser.open(url):
            print(COLORS.dim("  opened in your browser"))
        else:
            print(COLORS.dim("  open the URL above in a browser (the server keeps running)"))
    except Exception:  # noqa: BLE001 - headless box
        print(COLORS.dim("  no browser on this machine - open the URL above from any device"))
    print()
    return url


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


def _local_status(base_url: str) -> str:
    """Cheap TCP probe - `nexus agents` must not hang on a dead local box."""
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return COLORS.green(f"reachable at {base_url}")
    except OSError:
        return COLORS.yellow(f"not running at {base_url} - `nexus local` to set it up")


def cmd_agents(args: argparse.Namespace) -> int:
    config = load_config(Path(args.workspace).resolve() if args.workspace else Path.cwd())
    for name, cfg in sorted(config.agents.items()):
        marker = "*" if name == config.default_agent else " "
        roles = [r.name for r in config.roles.roles.values() if (r.agent or config.default_agent) == name]
        print(f"{marker} {COLORS.bold(name)}")
        print(f"    provider : {cfg.provider}")
        print(f"    model    : {cfg.model}")
        print(f"    base_url : {cfg.effective_base_url or '(sdk default)'}")
        if cfg.key_family:
            if cfg.resolved_api_key:
                source = cfg.key_source or ("inline" if cfg.api_key else "?")
                print(f"    api key  : ✓ {keyring.mask(cfg.resolved_api_key)}  ({source})")
            else:
                print(f"    api key  : ✗ missing - {cfg.missing_key_hint()}")
        else:
            print("    api key  : (not needed - keyless local/self-hosted endpoint)")
        print(f"    tools    : {cfg.tool_mode}{' (supports_tools=false)' if not cfg.supports_tools else ''}")
        if cfg.optional:
            status = _local_status(cfg.effective_base_url or "")
            print(f"    local    : {status}")
            print("    optional : yes - offline is fine, roles fall back to a hosted agent")
        print(f"    fallback : {cfg.fallback or '-'}")
        print(f"    roles    : {len(roles)} ({', '.join(roles[:6])}{'…' if len(roles) > 6 else ''})")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    """Ask the provider which model ids it actually serves right now."""
    from .providers import build_providers

    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()
    config = load_config(workspace)
    try:
        cfg = config.agent(args.agent)
    except ConfigError as exc:
        print(COLORS.red(f"config error: {exc}"), file=sys.stderr)
        return 2
    if cfg.key_family and not cfg.resolved_api_key:
        print(COLORS.red(f"agent '{cfg.name}' has no API key - {cfg.missing_key_hint()}"))
        return 1
    provider = build_providers(config.agents).get(cfg.name)
    try:
        models = provider.list_models()
    except Exception as exc:  # noqa: BLE001
        print(COLORS.red(f"✗ {cfg.name} ({cfg.effective_base_url}): {exc}"))
        return 1
    if args.json:
        print(json.dumps({"agent": cfg.name, "current": cfg.model, "models": models}, indent=2))
        return 0
    if not models:
        if cfg.optional:
            print(COLORS.yellow(f"no models listed by {cfg.effective_base_url}"))
            print(COLORS.dim(f"  optional endpoint - start it (`ollama serve`) and pull one: "
                             f"`ollama pull {cfg.model}`"))
        else:
            print(COLORS.yellow(f"no models listed by {cfg.effective_base_url}"))
        return 1
    print(COLORS.bold(f"{cfg.name} ({cfg.provider}) - {len(models)} models, current: {cfg.model}"))
    for name in models:
        mark = COLORS.green("*") if name == cfg.model else " "
        print(f" {mark} {name}")
    print(COLORS.dim(f"switch: NEXUS model override in agents.yaml, or {cfg.name}.model"))
    return 0


# --------------------------------------------------------------------------- #
# local models (Ollama) - Hermes, uncensored, everything free and offline
# --------------------------------------------------------------------------- #

LOCAL_MODELS: tuple[tuple[str, str, str], ...] = (
    ("hermes3:8b", "hermes", "local Hermes worker (docs, QA, bulk edits)"),
    ("hermes3:70b", "hermes-big", "bigger Hermes when you have the VRAM"),
    ("dolphin-mixtral:latest", "ollama-uncensored", "uncensored local model (authorized research)"),
    ("llama3.1:8b", "ollama", "generic local fallback"),
)


def ollama_base_url() -> str:
    raw = os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
    raw = raw.rstrip("/")
    return raw[: -len("/v1")] if raw.endswith("/v1") else raw


def _ollama_models(base: str, timeout: float = 2.0) -> tuple[bool, list[str], str]:
    """(running, model names, detail) - never raises, never hangs."""
    try:
        import httpx
    except Exception:  # pragma: no cover - httpx is a hard dependency
        return False, [], "httpx is not installed"
    try:
        response = httpx.get(f"{base}/api/tags", timeout=timeout)
        if response.status_code != 200:
            return False, [], f"HTTP {response.status_code}"
        data = response.json()
        names = [str(m.get("name") or "") for m in (data.get("models") or []) if m.get("name")]
        return True, names, f"{len(names)} model(s)"
    except Exception as exc:  # noqa: BLE001
        return False, [], f"{exc.__class__.__name__}: {exc}"


def cmd_local(args: argparse.Namespace) -> int:
    base = getattr(args, "host", None) or ollama_base_url()
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]

    if getattr(args, "pull", None):
        model = args.pull
        if not shutil.which("ollama"):
            print(COLORS.red("ollama is not installed - https://ollama.com/download"))
            return 1
        print(COLORS.bold(f"ollama pull {model}") + COLORS.dim("  (this can take a while)"))
        try:
            import subprocess

            code = subprocess.call(["ollama", "pull", model])
        except Exception as exc:  # noqa: BLE001
            print(COLORS.red(f"✗ {exc}"))
            return 1
        if code == 0:
            print(COLORS.green(f"✓ {model} is ready - use it with /agent <agent> or a role that targets it"))
        return code

    running, models, detail = _ollama_models(base)
    rows: list[dict[str, Any]] = []
    for model, agent_name, purpose in LOCAL_MODELS:
        present = any(name == model or name.startswith(model.split(":")[0] + ":") for name in models)             if running else False
        rows.append({"model": model, "agent": agent_name, "purpose": purpose, "present": present})

    if getattr(args, "json", False):
        print(json.dumps({
            "host": base,
            "running": running,
            "detail": detail,
            "installed": models,
            "agents": rows,
        }, ensure_ascii=False, indent=2))
        return 0 if running else 1

    mark = COLORS.green("✓") if running else COLORS.red("✗")
    print(COLORS.bold("Nexus local models") + COLORS.dim(f"  (Ollama at {base})"))
    print(f"  {mark} {('running - ' + detail) if running else ('not running - ' + detail)}")
    print()
    print(COLORS.dim(f"  {'model':<24} {'agent':<20} status"))
    for row in rows:
        if running and row["present"]:
            state = COLORS.green("pulled")
        elif running:
            state = COLORS.dim("not pulled")
        else:
            state = COLORS.dim("-")
        print(f"  {row['model']:<24} {COLORS.cyan(row['agent']):<29} {state}  {COLORS.dim(row['purpose'])}")

    print()
    if not running:
        print(COLORS.yellow("Start Ollama, then pull the models you want:"))
        print(COLORS.dim("  ollama serve"))
        for model, _agent, _purpose in LOCAL_MODELS[:3]:
            print(COLORS.dim(f"  ollama pull {model}"))
        print(COLORS.dim("  nexus local --pull hermes3:8b      # or let Nexus do it"))
        print()
        print(COLORS.dim("No Ollama? The same roles fall back to the hosted free agents: "
                         "hermes-free, openrouter-uncensored."))
    else:
        missing = [row for row in rows if not row["present"]]
        if missing:
            print(COLORS.dim("Pull the missing ones:"))
            for row in missing:
                print(COLORS.dim(f"  ollama pull {row['model']}"))
        else:
            print(COLORS.green("All local workers are ready."))
        print(COLORS.dim("Use one now:  /agent hermes   ·   /agent ollama-uncensored   ·   /local --pull <model>"))
    return 0 if running else 1


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

    # -- learning surfaces (skills / memory / sessions) -------------------- #
    try:
        from . import skills as skilllib
        from .memory import MemoryStore
        from .sessions import SessionStore

        library = skilllib.discover(workspace)
        add("skills", None if not library else True, f"{len(library)} skills available",
            "nexus skills import  -  import the hermes-agent skill library")
        store = MemoryStore()
        add("memory", None, f"{len(store.memory.entries)} memory / {len(store.user.entries)} profile entries "
                            f"({store.memory.chars}/{store.memory.limit} chars)")
        add("sessions", None, f"{SessionStore().stats()['sessions']} past runs recorded",
            "`nexus recall` searches them")
    except Exception as exc:  # noqa: BLE001
        add("learning", False, str(exc))

    # -- API keys (terminal store, env vars; .env is optional/legacy) ------- #
    families = config.key_families()
    for family in families:
        key = keyring.get_key(family)
        spec = keyring.PROVIDERS.get(family)
        if key:
            origin = keyring.key_source(family) or str(keyring.store_path())
            add(f"key:{family}", True, f"{keyring.mask(key)} ({origin})")
        else:
            add(
                f"key:{family}",
                False,
                "not configured",
                (spec.command if spec else f"nexus keys set {family}")
                + (f"  -  {spec.console_url}" if spec else ""),
            )
    if not families:
        add("api keys", None, "no hosted provider configured (local/OpenAI-compatible endpoint)",
            "nexus keys set gemini|deepseek|openrouter")

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
            if cfg.key_family and not cfg.resolved_api_key:
                hint = ""   # the health detail already says how to set the key
            elif (cfg.effective_base_url or "").startswith(("http://localhost", "http://127.0.0.1")):
                hint = f"optional local endpoint - start it and pull the model: `ollama pull {cfg.model}`"
            else:
                hint = (f"endpoint {cfg.effective_base_url} unreachable - check the URL, "
                        f"the key ({cfg.key_env_names[0] if cfg.key_env_names else 'inline'}) and your network")
        if not ok and cfg.optional:
            # local/extra endpoints are allowed to be offline: warn, do not fail
            add(f"agent:{name}", None, f"{cfg.provider} {cfg.model} - {detail}",
                hint or "optional agent - Nexus falls back to the hosted ones when it is down")
        else:
            add(f"agent:{name}", ok, f"{cfg.provider} {cfg.model} - {detail}", hint)

    token = os.environ.get("GITHUB_TOKEN")
    add("github token", bool(token), "GITHUB_TOKEN set" if token else "GITHUB_TOKEN not set",
        "required for GitHub tools; GH_TOKEN/CLI-login fallback is disabled")

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
    from .gui_session import launch_credential
    shared_token = launch_credential(workspace, args.token)
    return serve(host=args.host, port=args.port, workspace=workspace, token=shared_token,
                 auto_yes=args.yes, quiet=args.quiet, demo=getattr(args, "demo", False))


def cmd_panel(args: argparse.Namespace) -> int:
    """Serve the GUI dashboard (`nexus gui` / `nexus panel`)."""
    from .server import serve

    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()
    from .gui_session import launch_credential
    shared_token = launch_credential(workspace, args.token, getattr(args, "remember_token", False))
    host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{host}:{args.port}/dashboard"
    print()
    print(COLORS.bold("GUI dashboard") + COLORS.green(f"  {url}"))
    print(COLORS.dim(f"  workspace {workspace} · Ctrl-C stops the server"))
    if not getattr(args, "open", True):
        print(COLORS.dim("  open that URL in a browser (--no-open was passed, so nothing was launched)"))
    if args.host == "0.0.0.0":
        print(COLORS.dim("  bound to 0.0.0.0 so a remote browser can reach it - "
                         "set --token/NEXUS_SERVE_TOKEN before exposing the port"))
    print()
    return serve(host=args.host, port=args.port, workspace=workspace, token=shared_token,
                 auto_yes=args.yes, quiet=args.quiet, demo=getattr(args, "demo", False),
                 open_panel=getattr(args, "open", True))


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold project-local config. Deliberately writes NO .env file - API
    keys live in the terminal store (``nexus keys set``) or in your shell."""
    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()
    created: list[str] = []
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
            "    agent: deepseek\n"
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
    configured = [family for family in keyring.PROVIDERS if keyring.get_key(family)]
    if configured:
        print(COLORS.dim(f"api keys already configured: {', '.join(configured)}"))
    else:
        print(COLORS.dim("next: nexus keys set gemini   (no .env file needed)"))
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
    parser.add_argument("--no-key-prompt", action="store_true",
                        help="never ask for an API key interactively (CI / scripts)")


# --------------------------------------------------------------------------- #
# skills / memory / recall / learning / hermes / self-update
# --------------------------------------------------------------------------- #


def _skills_count(config_workspace: Path) -> int:
    from . import skills as skilllib

    return len(skilllib.discover(config_workspace))


def cmd_skills(args: argparse.Namespace) -> int:
    """``nexus skills [list|show|import|curate|delete|path]``."""
    from . import skills as skilllib

    workspace = Path(args.workspace).resolve() if getattr(args, "workspace", None) else Path.cwd()
    action = getattr(args, "action", None) or "list"

    if action == "path":
        for root in skilllib.skill_roots(workspace):
            print(root)
        return 0

    if action == "import":
        from . import hermes

        count, notes = hermes.import_skills(
            refresh=not getattr(args, "offline", False),
            include_optional=not getattr(args, "no_optional", False),
            force=bool(getattr(args, "force", False)),
            log=lambda msg: print(COLORS.dim(f"· {msg}")),
        )
        print(COLORS.green(f"imported {count} skills from Nous Research's hermes-agent"))
        for note in notes[:20]:
            print(COLORS.dim(f"  {note}"))
        print(COLORS.dim("  list them with: nexus skills"))
        return 0 if count else 1

    if action == "curate":
        report = skilllib.curator_report(workspace, stale_days=int(getattr(args, "stale_days", 45) or 45))
        print(COLORS.bold("skills curator") + COLORS.dim(f" ({report['total']} skills)"))
        print(COLORS.dim(f"  stale: {len(report['stale'])} · never used: {len(report['unused'])}"))
        for row in report["stale"][:20]:
            print(f"  {row['name']:<34} {row['last_used'] or 'never':<12} {row['description']}")
        if getattr(args, "apply", False):
            archived = skilllib.archive_stale(workspace, stale_days=int(args.stale_days or 45))
            print(COLORS.green(f"archived {len(archived)} stale skills (moved to archive/, never deleted)"))
        elif report["stale"]:
            print(COLORS.dim("  run `nexus skills curate --apply` to archive them"))
        return 0

    if action == "delete":
        name = (getattr(args, "name", "") or "").strip()
        if not name:
            print(COLORS.red("usage: nexus skills delete <name>"))
            return 2
        ok = skilllib.delete_skill(name, workspace=workspace)
        print(COLORS.green(f"deleted {name}") if ok else COLORS.red(f"no skill named '{name}'"))
        return 0 if ok else 1

    library = skilllib.discover(workspace)
    if action == "show":
        name = (getattr(args, "name", "") or "").strip()
        skill = skilllib.get(name, workspace)
        if skill is None:
            print(COLORS.red(f"no skill named '{name}' (try: nexus skills)"))
            return 1
        print(COLORS.bold(skill.name) + COLORS.dim(f"  v{skill.version} · {skill.source} · {skill.path}"))
        print(COLORS.dim(f"  {skill.description}"))
        print()
        print(skill.body)
        usage = skill.usage()
        if usage:
            print(COLORS.dim(f"\n  used {usage.get('uses', 0)}x · last {usage.get('last_used', '—')}"))
        return 0

    if getattr(args, "json", False):
        print(json.dumps([skill.to_dict() for skill in library], indent=2, ensure_ascii=False))
        return 0
    if not library:
        print(COLORS.yellow("no skills installed yet."))
        print(COLORS.dim("  create one:            /learn <what you just did>   (inside the REPL)"))
        print(COLORS.dim("  import Hermes library: nexus skills import"))
        return 0
    needle = (getattr(args, "search", "") or "").lower()
    rows = [s for s in library if not needle or needle in (s.name + s.description + " ".join(s.tags)).lower()]
    print(COLORS.bold(f"{len(rows)} skills") + COLORS.dim(" · procedural memory the agent loads on demand"))
    for skill in rows:
        uses = skill.usage().get("uses", 0)
        print(f"  {COLORS.cyan(skill.name):<34} {COLORS.dim(skill.category or '-'):<16} "
              f"{skill.prompt_description}" + (COLORS.dim(f"  ({uses}x)") if uses else ""))
    print()
    print(COLORS.dim("  nexus skills show <name> · nexus skills import · nexus skills curate"))
    return 0


def _brief_from_source(source: str) -> str:
    """Turn a file, directory or URL into text the learning call can read."""
    import urllib.request
    from pathlib import Path as _P

    if source.startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"User-Agent": "nexus-agent"})
        with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310 - user-supplied URL
            raw = response.read(200_000).decode("utf-8", "replace")
        return f"SOURCE: {source}\n\n{raw}"
    path = _P(source).expanduser()
    if path.is_dir():
        parts = [f"SOURCE DIRECTORY: {path}"]
        for child in sorted(path.rglob("*")):
            if child.is_file() and child.stat().st_size < 200_000:
                try:
                    parts.append(f"\n### {child.relative_to(path)}\n{child.read_text('utf-8', 'replace')[:20000]}")
                except OSError:
                    continue
            if sum(len(p) for p in parts) > 120_000:
                parts.append("\n… [truncated]")
                break
        return "\n".join(parts)
    if path.is_file():
        return f"SOURCE FILE: {path}\n\n{path.read_text('utf-8', 'replace')[:120_000]}"
    return source


def cmd_learn(args: argparse.Namespace) -> int:
    """``nexus learn "<what to capture>" | nexus learn path/ | nexus learn <url>``."""
    raw_text = getattr(args, "text", "") or ""
    text = " ".join(raw_text).strip() if isinstance(raw_text, list) else str(raw_text).strip()
    if not text:
        print(COLORS.red("usage: nexus learn <description | file | directory | url>"))
        return 2
    brief = _brief_from_source(text)
    agent = build_agent(args)
    agent.auto_route = False
    outcome = agent.learning.learn_from_brief(brief, name=getattr(args, "name", "") or "")
    if outcome.changed:
        print(COLORS.green(f"✓ {outcome.summary()}"))
        if outcome.skill:
            print(COLORS.dim(f"  {outcome.skill_path}"))
        for note in outcome.notes[:5]:
            print(COLORS.dim(f"  {note}"))
        return 0
    print(COLORS.yellow(f"nothing saved: {outcome.summary()}"))
    for note in outcome.notes[:5]:
        print(COLORS.dim(f"  {note}"))
    return 1


def cmd_recall(args: argparse.Namespace) -> int:
    """Search this workspace's past runs."""
    from .sessions import SessionStore, format_hit, json_dumps

    store = SessionStore()
    raw_query = getattr(args, "query", "") or ""
    query = " ".join(raw_query).strip() if isinstance(raw_query, list) else str(raw_query).strip()
    rows = store.search(query, limit=args.limit) if query else store.recent(args.limit)
    if getattr(args, "json", False):
        print(json_dumps(rows))
        return 0
    if not rows:
        print(COLORS.dim(f"no sessions recorded yet ({store.stats()['path']})"))
        return 0
    if not query:
        print(COLORS.bold(f"last {len(rows)} runs") + COLORS.dim(f" · {store.path}"))
        for row in rows:
            print(f"  {row['id']}  {_ago(row['created']):>10}  {str(row.get('role') or '-'):<18} "
                  f"{' '.join(str(row.get('task') or '').split())[:90]}")
        return 0
    print(COLORS.bold(f"{len(rows)} match(es) for '{query}'"))
    for row in rows:
        print(format_hit(row, width=110))
    return 0


def _ago(timestamp: float) -> str:
    import datetime as _dt

    delta = _dt.datetime.now(_dt.timezone.utc).timestamp() - float(timestamp or 0)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= size:
            return f"{int(delta // size)}{unit} ago"
    return "just now"


def cmd_memory(args: argparse.Namespace) -> int:
    """Global memory (MEMORY.md / USER.md) - the long-term brain."""
    from .memory import MemoryStore, env_flag

    store = MemoryStore(enabled=env_flag("NEXUS_MEMORY", True))
    action = getattr(args, "action", None) or "list"
    if action == "add":
        text = (getattr(args, "text", "") or "").strip()
        if not text:
            print(COLORS.red("usage: nexus memory add <text> [--user]"))
            return 2
        ok, message = store.add(text, "user" if getattr(args, "user", False) else "memory")
        print(COLORS.green(message) if ok else COLORS.red(message))
        return 0 if ok else 1
    if action == "remove":
        ok, message = store.remove(getattr(args, "text", "") or "", "user" if getattr(args, "user", False) else "memory")
        print(COLORS.green(message) if ok else COLORS.red(message))
        return 0 if ok else 1
    if action == "import":
        count, notes = store.import_hermes(getattr(args, "source", None))
        print(COLORS.green(f"imported {count} memory entries"))
        for note in notes:
            print(COLORS.dim(f"  {note}"))
        return 0
    if getattr(args, "json", False):
        print(json.dumps(store.stats(), indent=2, ensure_ascii=False))
        return 0
    for block in (store.memory, store.user):
        print(COLORS.bold(f"{block.name} ({block.chars}/{block.limit})") + COLORS.dim(f"  {block.path}"))
        print(block.render() or COLORS.dim("  (empty)"))
        print()
    return 0


def cmd_hermes(args: argparse.Namespace) -> int:
    """Bridge to Nous Research's hermes-agent."""
    from . import hermes

    action = getattr(args, "action", None) or "status"
    if action == "run":
        return hermes.run_cli(getattr(args, "argv", []) or [])
    if action == "install":
        count, notes = hermes.import_skills(
            refresh=True, force=bool(getattr(args, "force", False)),
            log=lambda msg: print(COLORS.dim(f"· {msg}")),
        )
        print(COLORS.green(f"✓ {count} Hermes skills available to Nexus") if count
              else COLORS.red("could not import Hermes skills"))
        for note in notes[:20]:
            print(COLORS.dim(f"  {note}"))
        return 0 if count else 1
    if action == "sync":
        count, notes = hermes.import_skills(
            refresh=True, force=True, log=lambda msg: print(COLORS.dim(f"· {msg}"))
        )
        memories, mem_notes = hermes.import_memories(getattr(args, "memories", None))
        print(COLORS.green(f"✓ synced {count} skills and {memories} memory entries"))
        for note in [*notes[:10], *mem_notes[:5]]:
            print(COLORS.dim(f"  {note}"))
        return 0

    info = hermes.status()
    if getattr(args, "json", False):
        print(json.dumps(info, indent=2))
        return 0
    print(COLORS.bold("hermes-agent bridge"))
    rows = (
        ("upstream", info["repo"]),
        ("checkout", f"{info['checkout']} ({'present' if info['checkout_present'] else 'not cloned'})"),
        ("skills", f"{info['skills_available']} upstream · {info['imported_skills']} imported into Nexus"),
        ("hermes cli", info["cli"] or "not installed (only needed for the gateway/TUI)"),
        ("memories", info["memories"]),
    )
    for key, value in rows:
        print(f"  {COLORS.cyan(key):<18} {value}")
    print()
    print(COLORS.dim("  nexus hermes install   # clone upstream + import its skills"))
    print(COLORS.dim("  nexus hermes sync      # re-import skills and MEMORY.md/USER.md"))
    print(COLORS.dim("  nexus hermes run ...   # hand a task to a locally installed `hermes`"))
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """``nexus update`` - fast-forward this checkout and reinstall the package."""
    from . import __version__, hermes

    print(COLORS.dim(f"nexus {__version__} @ {hermes.repo_root()}"))
    ok, messages = hermes.self_update(
        pull=not getattr(args, "no_pull", False),
        reinstall=not getattr(args, "no_install", False),
        log=lambda msg: print(COLORS.dim(f"· {msg}")),
    )
    for message in messages:
        print(message)
    if ok:
        print(COLORS.green("✓ up to date"))
        return 0
    print(COLORS.red("update failed"))
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus",
        description="Nexus Agent - one CLI, a whole team of routed specialist agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  nexus                        # just this: opens the agent\n"
            "  nexus keys set gemini        # paste a key once, no .env file\n"
            "  nexus local                  # Ollama: Hermes / uncensored models\n"
            "  nexus task 'add pagination to the /users API endpoint and write tests'\n"
            "  nexus ask 'explain how auth works in this repo' --role backend\n"
            "  nexus pipeline ship-feature 'add a rate limiter to the public API'\n"
            "  nexus roles --search security\n"
            "  nexus doctor\n"
            "  nexus gui                    # live GUI dashboard (localhost:8787)\n"
            "  (inside the REPL: /help · /gui · /local · /roles · /exit)\n"
        ),
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")
    from .delegation.cli import add_parser as add_delegation_parser
    add_delegation_parser(sub)
    from .hud.cli import add_parser as add_hud_parser
    add_hud_parser(sub)
    from .extensions.cli import add_parser as add_extensions_parser
    add_extensions_parser(sub)
    from .telegram.cli import add_parser as add_telegram_parser
    add_telegram_parser(sub)
    from .context.cli import add_parser as add_context_parser
    add_context_parser(sub)
    from .evaluation.cli import add_parser as add_evaluation_parser
    add_evaluation_parser(sub)
    from .evaluation.cli import add_trace_parser
    add_trace_parser(sub)

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

    from .orchestration.cli import add_parser as add_orchestration_parser
    add_orchestration_parser(sub)
    from .github_cli import add_parser as add_github_parser
    add_github_parser(sub)
    from .computer_cli import add_parser as add_computer_parser
    add_computer_parser(sub)
    from .jarvis.cli import add_parser as add_jarvis_parser
    add_jarvis_parser(sub)

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

    for sub_name, sub_help in (
        ("panel", "serve + open the GUI dashboard"),
        ("gui", "serve + open the GUI dashboard (alias of panel)"),
    ):
        p = sub.add_parser(sub_name, help=sub_help)
        p.add_argument("--workspace", "-C")
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8787)
        p.add_argument("--token", default=os.environ.get("NEXUS_SERVE_TOKEN", ""))
        p.add_argument("--yes", "-y", action="store_true")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--demo", action="store_true", help="scripted runs (no API key needed)")
        p.add_argument("--remember-token", action="store_true", help="remember the existing GUI token in the native OS vault")
        p.add_argument("--no-open", dest="open", action="store_false",
                       help="serve only - do not launch a browser")
        p.set_defaults(func=cmd_panel, open=True)

    p = sub.add_parser("keys", help="set / list / test API keys (no .env file needed)")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "set", "remove", "test", "path"],
                   help="list (default) | set | remove | test | path")
    p.add_argument("provider", nargs="?", help="gemini | deepseek | openrouter")
    p.add_argument("--key", default="", help="paste the key non-interactively (it lands in shell history)")
    p.add_argument("--session", action="store_true",
                   help="use the key for this process only - do not write it to disk")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_keys)

    p = sub.add_parser("local", help="Ollama status: Hermes / uncensored / local models")
    p.add_argument("--host", help="Ollama base URL (default $OLLAMA_HOST or http://localhost:11434)")
    p.add_argument("--pull", metavar="MODEL", help="pull a model now (e.g. hermes3:8b)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_local)

    p = sub.add_parser("models", help="list the model ids a provider serves")
    p.add_argument("agent", nargs="?", help="agent name from agents.yaml (default: the default agent)")
    p.add_argument("--workspace", "-C")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("init", help="write .nexus / .vscode scaffolding into a project")
    p.add_argument("--workspace", "-C")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("prompt", help="print the system prompt for a role")
    p.add_argument("role", nargs="?", default="general")
    p.add_argument("--workspace", "-C")
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("skills", help="skills: the agent's procedural memory (list/show/import/curate)")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "show", "import", "curate", "delete", "path"])
    p.add_argument("name", nargs="?", help="skill name (show / delete)")
    p.add_argument("--workspace", "-C")
    p.add_argument("--search", "-s", help="filter by name/description/tag")
    p.add_argument("--apply", action="store_true", help="curate: archive the stale ones")
    p.add_argument("--stale-days", type=int, default=45)
    p.add_argument("--offline", action="store_true", help="import: use the local checkout, do not pull")
    p.add_argument("--no-optional", action="store_true", help="import: skip optional-skills")
    p.add_argument("--force", action="store_true", help="import: overwrite existing skills")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("learn", help="turn work/a doc/a directory into a reusable skill")
    p.add_argument("text", nargs="*", help="what to capture, a file, a directory or a URL")
    p.add_argument("--workspace", "-C")
    p.add_argument("--name", help="force the skill name")
    p.set_defaults(func=cmd_learn)

    p = sub.add_parser("recall", help="search your past sessions in this project")
    p.add_argument("query", nargs="*", help="what to look for (omit for the latest runs)")
    p.add_argument("--limit", "-n", type=int, default=8)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("memory", help="long-term memory: MEMORY.md + USER.md")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove", "import", "path"])
    p.add_argument("text", nargs="?", help="the entry to add/remove")
    p.add_argument("--user", action="store_true", help="target USER.md (preferences) instead of MEMORY.md")
    p.add_argument("--source", help="import: directory holding MEMORY.md / USER.md")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_memory)

    p = sub.add_parser("hermes", help="bridge to Nous Research's hermes-agent (skills, memories, CLI)")
    p.add_argument("action", nargs="?", default="status", choices=["status", "install", "sync", "run"])
    p.add_argument("argv", nargs="*", help="arguments for `nexus hermes run`")
    p.add_argument("--memories", help="sync: Hermes memories directory")
    p.add_argument("--force", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_hermes)

    p = sub.add_parser("update", help="update Nexus itself (git pull + reinstall)")
    p.add_argument("--no-pull", action="store_true")
    p.add_argument("--no-install", action="store_true")
    p.set_defaults(func=cmd_update)

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
    if len(argv) == 1 and argv[0] in {"-h", "--help"}:
        # top-level help: list the subcommands instead of chat's flags
        parser.print_help()
        raise SystemExit(0)
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
    except BrokenPipeError:
        # `nexus roles | head` must not print a traceback
        try:
            sys.stdout.close()
        except OSError:
            pass
        return 0
    except ConfigError as exc:
        print(COLORS.red(f"config error: {exc}"), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
