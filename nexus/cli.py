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
eval      run the offline evaluation suite (golden missions, red-team, latency benchmarks)
lessons   review the append-only lessons log from past missions
skills    skills: list/show/import/curate/stage/approve/rollback
version   version info
"""

from __future__ import annotations

import argparse
import datetime
import difflib
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
    elif t is EventType.VOICE_STATE:
        print(COLORS.dim(f"🎙 voice: {d.get('state', 'idle')}"))
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
        bits = arg.split()
        if bits and bits[0] == "--team":
            cmd_roles(argparse.Namespace(workspace=None, json=False, show=None, category=None,
                                         search=None, team=bits[1] if len(bits) > 1 else None))
        else:
            print_roles(agent.config, arg)  # return code ignored inside the REPL
    elif cmd == "teams":
        bits = arg.split()
        action = bits[0] if bits and bits[0] in {"list", "show", "role", "check"} else "list"
        target = bits[1] if action != "list" and len(bits) > 1 else None
        cmd_teams(argparse.Namespace(action=action, target=target, stage=None, workspace=None,
                                     json=False))  # return code ignored inside the REPL
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


def _roles_versions_dir(workspace: Path) -> Path:
    return workspace / ".nexus" / "roles.versions"


def _list_role_versions(workspace: Path) -> list[str]:
    vdir = _roles_versions_dir(workspace)
    if not vdir.is_dir():
        return []
    return sorted(
        p.name
        for p in vdir.iterdir()
        if p.is_dir() and (p / "roles.yaml").is_file()
    )


def _dt_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")


def cmd_roles_versions(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()
    versions = _list_role_versions(workspace)
    if getattr(args, "json", False):
        print(json.dumps(versions, indent=2, ensure_ascii=False))
        return 0
    if not versions:
        print("no role versions yet - run `python3 scripts/build_roles.py` to snapshot")
        return 0
    print(COLORS.bold("role/persona versions (newest last)"))
    for stamp in versions:
        print(f"  {stamp}")
    print()
    print(COLORS.dim("  nexus roles --rollback <stamp>        # preview the restore (diff)"))
    print(COLORS.dim("  nexus roles --rollback <stamp> --execute  # restore roles.yaml + persona.yaml"))
    return 0


def cmd_roles_rollback(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()
    vdir = _roles_versions_dir(workspace)
    snap = vdir / args.rollback
    if not snap.is_dir() or not (snap / "roles.yaml").is_file():
        print(COLORS.red(f"no role version named '{args.rollback}' - `nexus roles --versions` lists them"))
        return 1
    roles_path = workspace / "roles.yaml"
    persona_path = workspace / "persona.yaml"
    current = roles_path.read_text(encoding="utf-8") if roles_path.is_file() else ""
    snapshot = (snap / "roles.yaml").read_text(encoding="utf-8")
    diff = "\n".join(
        difflib.unified_diff(
            current.splitlines(), snapshot.splitlines(),
            fromfile=f"current {roles_path}", tofile=f"snapshot {snap / 'roles.yaml'}",
        )
    )
    print(diff or COLORS.dim("  (no changes)"))
    if not getattr(args, "execute", False):
        print()
        print(COLORS.dim("dry run - pass --execute to restore roles.yaml and persona.yaml from this snapshot"))
        return 0
    backup = vdir / f"pre-rollback-{args.rollback}-{_dt_stamp()}"
    backup.mkdir(parents=True, exist_ok=True)
    if roles_path.is_file():
        shutil.copy2(roles_path, backup / "roles.yaml")
    if persona_path.is_file():
        shutil.copy2(persona_path, backup / "persona.yaml")
    shutil.copy2(snap / "roles.yaml", roles_path)
    persona_snap = snap / "persona.yaml"
    if persona_snap.is_file():
        shutil.copy2(persona_snap, persona_path)
    elif persona_path.is_file():
        persona_path.unlink()
    try:
        load_config(workspace)
    except ConfigError as exc:
        print(COLORS.red(f"restored roles.yaml fails to load: {exc} - keeping your backup at {backup}"))
        return 1
    print(COLORS.green(f"✓ rolled back roles/persona to {args.rollback} (previous state backed up at {backup})"))
    return 0


def cmd_roles(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()

    if getattr(args, "versions", False):
        return cmd_roles_versions(args)
    if getattr(args, "rollback", None):
        return cmd_roles_rollback(args)

    config = load_config(workspace)
    team_filter = getattr(args, "team", None)
    if team_filter:
        from .teams import load_teams

        book = load_teams(workspace)
        team = book.get(team_filter)
        if team is None:
            matches = book.find(team_filter)
            if len(matches) == 1:
                team = matches[0]
            else:
                print(COLORS.red(f"no team matches '{team_filter}' - `nexus teams` lists them all"))
                return 1
        wanted = [name for name in team.roles if name in config.roles]
        if args.json:
            print(json.dumps([config.roles.get(name).to_dict() for name in wanted],
                             ensure_ascii=False, indent=2))
            return 0
        print(COLORS.bold(f"{team.label()}") + COLORS.dim(
            f"  [{team.ident}] {len(wanted)} roles · stages: {', '.join(s.name for s in team.stages)}"))
        for name in wanted:
            role = config.roles.get(name)
            stages = ", ".join(team.stage_of(name))
            print(f"  {name:<30} {role.category:<12} -> {role.agent:<16} {COLORS.dim(stages)}")
        return 0
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


def cmd_teams(args: argparse.Namespace) -> int:
    """`nexus teams` - read the generated roster (docs/teams.md -> teams.yaml)."""
    from .teams import load_teams

    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()
    config = load_config(workspace)
    book = load_teams(workspace)
    if not len(book):
        print(COLORS.red("no teams.yaml found - run `python3 scripts/build_teams.py --yaml`"))
        return 1
    action = args.action or "list"

    if action == "list":
        if args.json:
            print(json.dumps(book.to_dict(), ensure_ascii=False, indent=2))
            return 0
        top = [team for team in book if team.number.isdigit()]
        subs = len(book) - len(top)
        extra = f" + {subs} language sub-teams" if subs else ""
        print(COLORS.bold(f"{len(top)} teams{extra} · {len(book.roles())} roles") +
              COLORS.dim(f"  ({book.sources[-1]})"))
        for team in book:
            print(f"  {team.number:>3}  {team.ident:<36} {team.size:>4} roles  "
                  f"{len(team.stages):>2} stages  {team.label()}")
        print(COLORS.dim("  nexus teams show <team>   ·   nexus teams role <role>   ·   nexus teams check"))
        return 0

    if action == "show":
        target = (args.target or "").strip()
        team = book.get(target)
        if team is None:
            matches = book.find(target)
            if not matches:
                print(COLORS.red(f"no team matches '{target}' - `nexus teams` lists them all"))
                return 1
            if len(matches) > 1:
                print(COLORS.yellow(f"'{target}' is ambiguous:"))
                for candidate in matches:
                    print(f"  {candidate.number:>3}  {candidate.ident:<36} {candidate.title}")
                return 1
            team = matches[0]
        if args.json:
            print(json.dumps(team.to_dict(), ensure_ascii=False, indent=2))
            return 0
        print(COLORS.bold(f"{team.label()}") + COLORS.dim(f"  [{team.ident} · team {team.number}]"))
        print(f"  members:  {team.size} ({team.assignments} stage assignments) · stages: {len(team.stages)}")
        if team.handoff:
            print(f"  handoff:  {team.handoff}")
        if team.workflow:
            print(f"  workflow: {' -> '.join(team.workflow)}")
        if team.parent:
            print(f"  parent:   {team.parent}")
        if team.children:
            print(f"  sub-teams: {', '.join(team.children)}")
        print()
        for stage in team.stages:
            if args.stage and args.stage.strip().lower() not in stage.name.strip().lower():
                continue
            print(f"  {stage.name:<22} {', '.join(stage.roles)}")
        if args.stage and not any(args.stage.strip().lower() in stage.name.strip().lower()
                                  for stage in team.stages):
            print(COLORS.yellow(f"  no stage matching '{args.stage}' in {team.ident}"))
            return 1
        return 0

    if action == "role":
        target = (args.target or "").strip()
        if not target:
            print(COLORS.red("usage: nexus teams role <role-name>"))
            return 1
        hits = book.for_role(target)
        name = target
        if target in config.roles:
            role = config.roles.get(target)
            name = role.name
            if not args.json:
                print(COLORS.bold(role.name) + COLORS.dim(f"  [{role.category}] {role.title}"))
        if not hits:
            print(COLORS.red(f"'{name}' is not in any team of the roster"))
            return 1
        if args.json:
            print(json.dumps([{"team": team.ident, "number": team.number, "title": team.title,
                               "stages": stages} for team, stages in hits],
                             ensure_ascii=False, indent=2))
            return 0
        for team, stages in hits:
            print(f"  {team.number:>3}  {team.ident:<36} {team.label():<34} {', '.join(stages)}")
        return 0

    if action == "check":
        errors = book.check(config.roles)
        uncovered = book.uncovered(config.roles)
        covered = len(config.roles) - len(uncovered)
        for error in errors:
            print(COLORS.red(f"error: {error}"), file=sys.stderr)
        if args.json:
            print(json.dumps({"teams": len(book), "roles": len(config.roles), "covered": covered,
                              "uncovered": uncovered, "errors": errors},
                             ensure_ascii=False, indent=2))
            return 1 if errors else 0
        top = [team for team in book if team.number.isdigit()]
        subs = len(book) - len(top)
        extra = f" + {subs} language sub-teams" if subs else ""
        print(f"{len(top)} teams{extra} · {len(book.roles())} roster roles · "
              f"{covered}/{len(config.roles)} installed roles placed")
        if uncovered:
            print(COLORS.yellow(f"note: {len(uncovered)} installed role(s) are in no team "
                                f"(project-local additions are expected): {', '.join(uncovered[:8])}"
                                f"{' ...' if len(uncovered) > 8 else ''}"))
        if errors:
            print(COLORS.red(f"{len(errors)} problem(s) - the roster does not match the installed roles"))
            return 1
        print(COLORS.green("ok: every roster role exists and every handoff resolves"))
        return 0

    print(COLORS.red(f"unknown action '{action}' - use list, show, role or check"))
    return 1


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

    try:
        from .always_on import health as service_health
        from .hud.presence_policy import load as hud_policy
        hud = hud_policy(workspace)
        observed = service_health(workspace)
        add("shared presence", None, "enabled" if hud['presence']['enabled'] else "OFF in hud.yaml")
        add("always-on service", None if not hud['service']['enabled'] else observed['heartbeat_fresh'] and observed['child_running'],
            observed['state'], "nexus service -C . health; inspect the reviewed OS user-service registration")
    except (ValueError, OSError):
        add("HUD/service policy", False, "Invalid policy or unsafe private state", "inspect operator-owned hud.yaml")
    if getattr(args, 'offline', False):
        return _render_doctor(checks, args)

    add("workspace", True, str(workspace))
    add("roles", len(config.roles) > 0, f"{len(config.roles)} roles registered",
        "add roles to roles.yaml or .nexus/roles.yaml")
    try:
        from .teams import load_teams

        book = load_teams(workspace)
        team_errors = book.check(config.roles)
        add("teams", not team_errors,
            f"{len([t for t in book if t.number.isdigit()])} teams / {len(book.roles())} roster roles",
            "regenerate with `python3 scripts/build_teams.py --yaml`"
            if not len(book) else "fix `nexus teams check`")
    except ConfigError as exc:
        add("teams", False, str(exc), "fix or regenerate teams.yaml")

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

    # -- optional components (voice / chief / proactive / vision / integrations) -- #
    for comp_name, cfg_file, enabled_key in (
        ("voice", "voice.yaml", "enabled"),
        ("chief", "chief.yaml", "enabled"),
        ("proactive", "proactive.yaml", "enabled"),
        ("vision", "vision.yaml", "enabled"),
        ("integrations", "mcp.yaml", "integrations.enabled"),
    ):
        cfg_path = workspace / cfg_file
        if not cfg_path.is_file():
            add(f"opt:{comp_name}", None, f"{cfg_file} not present",
                f"copy {cfg_file} to workspace to opt in")
            continue
        try:
            data = yaml.safe_load(cfg_path.read_text()) or {}
        except yaml.YAMLError:
            add(f"opt:{comp_name}", False, f"{cfg_file} invalid YAML", f"fix {cfg_file}")
            continue
        val = data
        for part in enabled_key.split("."):
            if not isinstance(val, dict):
                val = None
                break
            val = val.get(part)
        is_enabled = val is True
        add(
            f"opt:{comp_name}",
            None,
            f"{cfg_file}: {'enabled' if is_enabled else 'OFF'}",
            f"set {enabled_key}: true in {cfg_file} to opt in (operator-owned file)",
        )

    # -- security posture ------------------------------------------------------ #
    autonomy_path = workspace / "autonomy.yaml"
    perms_path = workspace / "permissions.yaml"
    sec_path = workspace / "security.yaml"

    def _yaml_bool(path: Path, dotted_key: str) -> bool | None:
        if not path.is_file():
            return None
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            return None
        val = data
        for part in dotted_key.split("."):
            if not isinstance(val, dict):
                return None
            val = val.get(part)
        return val if isinstance(val, bool) else None

    level = None
    if autonomy_path.is_file():
        try:
            ad = yaml.safe_load(autonomy_path.read_text()) or {}
            level = ad.get("level")
        except yaml.YAMLError:
            pass
    add("security:autonomy", None, f"level={level or 'L1'} (default)",
        "edit autonomy.yaml to change admission level (operator-owned)")

    sf = _yaml_bool(perms_path, "second_factor.enabled")
    add("security:second_factor", None,
        f"second factor {'ON' if sf is True else 'OFF (default)'}",
        "set permissions.yaml second_factor.enabled: true to require PIN/Telegram")

    sb = _yaml_bool(sec_path, "enabled")
    add("security:defensive", None,
        f"defensive mode {'ON' if sb is True else 'OFF (default)'}",
        "set security.yaml enabled: true to narrow the tool gate")

    rt = _yaml_bool(autonomy_path, "runtime.enabled")
    add("security:runtime", None,
        f"local runtime {'ON' if rt is True else 'OFF (default)'}",
        "set autonomy.yaml runtime.enabled: true to allow local model execution")

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
# emergency stop / resume
# --------------------------------------------------------------------------- #


def cmd_stop(args: argparse.Namespace) -> int:
    """``nexus stop [--all]`` - latch the emergency stop, cancelling work."""
    from .computer_control import EmergencyStop

    latch = EmergencyStop()
    latch.set(True)

    if getattr(args, "all", False):
        # Also ask every known subsystem to pause/drain where safe.
        try:
            from .jarvis.daemon import Daemon
            Daemon(Path(args.workspace).resolve() if args.workspace else Path.cwd()).request("pause")
        except Exception:
            # pause is best-effort; the latch itself is the hard stop.
            pass

    if getattr(args, "json", False):
        print(json.dumps({"stopped": True, "resume": "nexus resume"}, ensure_ascii=False))
    else:
        print(COLORS.red("EMERGENCY STOP latched. All missions cancelled, pipelines paused, "
                          "processes/containers stopped. Resume requires `nexus resume`."))
    return 0


def cmd_resume(_args: argparse.Namespace) -> int:
    """``nexus resume`` - unlatch the emergency stop (operator-only)."""
    from .computer_control import EmergencyStop

    latch = EmergencyStop()
    latch.set(False)

    if getattr(_args, "json", False):
        print(json.dumps({"stopped": False}, ensure_ascii=False))
    else:
        print(COLORS.green("Emergency stop cleared. Operator approval still required for all "
                           "tool actions; nothing resumes automatically."))
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


def cmd_lessons(args: argparse.Namespace) -> int:
    """``nexus lessons`` - review the append-only lessons log (read-only)."""
    from .learning import LessonsStore

    store = LessonsStore()
    entries = store.recent(int(getattr(args, "tail", 20) or 20))
    if getattr(args, "json", False):
        print(
            json.dumps(
                [
                    {
                        "when": e.when,
                        "task": e.task,
                        "role": e.role,
                        "agent": e.agent,
                        "ok": e.ok,
                        "stopped_reason": e.stopped_reason,
                        "tool_calls": e.tool_calls,
                        "changed_files": e.changed_files,
                        "tainted": e.tainted,
                        "approval_denied": e.approval_denied,
                        "lesson": e.lesson,
                    }
                    for e in entries
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not entries:
        print("no lessons recorded yet - run a mission to start the log")
        return 0
    print(COLORS.bold("lessons (append-only, review-only - never auto-applied to prompts or permissions)"))
    for e in entries:
        mark = COLORS.green("ok") if e.ok else COLORS.red("blocked")
        print(f"  {COLORS.dim(e.when)}  {e.role:<10} {mark:>8}  {e.lesson}")
    print()
    print(COLORS.dim(f"{len(entries)} entry(ies) in {store.path}"))
    print(COLORS.dim("  lessons inform skills/memory through the normal learning review; they never change prompts or permissions silently"))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """``nexus eval run`` - offline evaluation suite; fails CI on regression."""
    from .evaluation.suite import run_suite, write_report

    workspace = Path(getattr(args, "workspace", None) or Path.cwd()).resolve()
    report = run_suite(workspace, days=int(getattr(args, "days", 7) or 7))
    path = write_report(report, workspace, fmt=getattr(args, "format", "json"))

    print(COLORS.bold(f"nexus eval ({report.ran_at})  workspace: {workspace}"))
    for c in report.checks:
        mark = COLORS.green("ok") if c.ok else COLORS.red("FAIL")
        budget = f" / budget {c.budget_seconds}s" if c.budget_seconds else ""
        print(f"  [{mark}] {c.name:<24} {c.detail}{budget}")
    print()
    if report.ok:
        print(COLORS.green("all evaluation checks passed"))
        print(COLORS.dim(f"report written to {path}"))
        return 0
    print(COLORS.red(f"REGRESSIONS: {', '.join(report.regressions)}"))
    print(COLORS.dim(f"report written to {path}"))
    return 1 if getattr(args, "fail_on_regression", True) else 0


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

    if action == "stage":
        from . import skill_staging

        name = (getattr(args, "name", "") or "").strip()
        if not name:
            print(COLORS.red("usage: nexus skills stage <name> --body-file FILE [--description ...] [--tags ...]"))
            return 2
        body = getattr(args, "body", "") or ""
        body_file = getattr(args, "body_file", None)
        if body_file:
            body = Path(body_file).read_text(encoding="utf-8")
        if not body.strip():
            print(COLORS.red("stage needs a body (--body or --body-file)"))
            return 2
        description = getattr(args, "description", "") or ""
        tags = [t.strip() for t in (getattr(args, "tags", "") or "").split(",") if t.strip()]
        diff = None
        diff_file = getattr(args, "diff_file", None)
        if diff_file:
            diff = Path(diff_file).read_text(encoding="utf-8")
        tests = None
        tests_file = getattr(args, "tests_file", None)
        if tests_file:
            tests = Path(tests_file).read_text(encoding="utf-8")
        try:
            proposal = skill_staging.stage_proposal(
                name=name, body=body, description=description, tags=tags,
                workspace=workspace, diff=diff, tests=tests,
            )
        except (ValueError, FileExistsError, OSError) as exc:
            print(COLORS.red(f"✗ {exc}"))
            return 1
        print(COLORS.green(f"✓ staged '{name}' at {proposal.path} (inert until approved)"))
        for note in proposal.notes[:5]:
            print(COLORS.dim(f"  {note}"))
        print(COLORS.dim(f"  review: nexus skills review   ·   activate: nexus skills approve {name} --execute"))
        return 0

    if action == "review":
        from . import skill_staging

        proposals = skill_staging.list_staged(workspace)
        if getattr(args, "json", False):
            print(json.dumps([p.to_dict() for p in proposals], indent=2, ensure_ascii=False))
            return 0
        if not proposals:
            print(COLORS.dim("no staged skill proposals"))
            return 0
        print(COLORS.bold(f"{len(proposals)} staged skill proposal(s)"))
        for p in proposals:
            print(f"  {COLORS.cyan(p.name):<30} {p.status:<10} {COLORS.dim(str(p.path))}")
            for note in p.notes[:3]:
                print(COLORS.dim(f"    {note}"))
        print()
        print(COLORS.dim("  nexus skills approve <name>        # dry-run preview"))
        print(COLORS.dim("  nexus skills approve <name> --execute  # run tests, ruff, permission check, then activate"))
        return 0

    if action in {"approve", "rollback"}:
        from . import skill_staging

        name = (getattr(args, "name", "") or "").strip()
        if not name:
            print(COLORS.red(f"usage: nexus skills {action} <name> [--execute]"))
            return 2
        if action == "approve":
            result = skill_staging.approve_proposal(name, workspace, execute=bool(getattr(args, "execute", False)))
        else:
            result = skill_staging.rollback_proposal(name, workspace)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result.get("ok") else 1
        for note in result.get("notes", []):
            print(COLORS.dim(f"  {note}"))
        if result.get("ok"):
            print(COLORS.green(f"✓ {result.get('message', 'done')}"))
            if result.get("activated"):
                print(COLORS.dim(f"  {result['skill_path']}"))
            return 0
        print(COLORS.red(f"✗ {result.get('error', 'refused')}"))
        return 1

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
    """Global memory (MEMORY.md / USER.md) plus opt-in long-term personal memory."""
    from .memory import LongTermMemory, MemoryPolicyError, MemoryStore, env_flag

    action = getattr(args, "action", None) or "list"
    ws = Path(getattr(args, "workspace", None) or Path.cwd()).resolve()

    if action in {"personal", "remember", "search", "confirm", "reject", "forget", "forget-topic", "compact", "export"}:
        ltm = LongTermMemory(ws)
        try:
            text = (getattr(args, "text", "") or "").strip()
            if action == "personal":
                rows = ltm.list_entries(query=text)
                if getattr(args, "json", False):
                    print(json.dumps({"enabled": ltm.enabled, "vector_count": ltm.vector_count, "entries": rows}, indent=2, ensure_ascii=False))
                else:
                    print(COLORS.bold(f"personal memory (enabled={ltm.enabled}, vectors={ltm.vector_count}, encrypted={ltm.encrypted})"))
                    for row in rows:
                        print(f"  [{row.get('status', 'confirmed')}] {row['id']} ({row.get('store', 'vector')}, {row.get('source', 'user')}, {row.get('date', '')}): {row['text']}")
                return 0
            if action == "remember":
                if not text:
                    print(COLORS.red("usage: nexus memory remember <text>"))
                    return 2
                res = ltm.handle_command(f"remember that {text}", explicit_sensitive=bool(getattr(args, "yes", False)))
                print(json.dumps(res, indent=2, ensure_ascii=False) if getattr(args, "json", False) else COLORS.green(res["reply"]))
                return 0
            if action == "search":
                hits = ltm.search(text)
                if getattr(args, "json", False):
                    print(json.dumps(hits, indent=2, ensure_ascii=False))
                else:
                    for h in hits:
                        print(f"  {h['id']} [{h['source']} | {h['date']}] ({h['score']}) {h['text']}")
                return 0
            if action == "confirm":
                fact = ltm.confirm_fact(text)
                print(json.dumps(fact, indent=2, ensure_ascii=False) if getattr(args, "json", False) else COLORS.green(f"confirmed {fact['id']}"))
                return 0
            if action == "reject":
                ok = ltm.reject_fact(text)
                print(COLORS.green(f"rejected {text}") if ok else COLORS.red(f"not found: {text}"))
                return 0 if ok else 1
            if action == "forget":
                res = ltm.forget(text)
                print(json.dumps(res, indent=2, ensure_ascii=False) if getattr(args, "json", False) else COLORS.green(f"forgot {res['total_removed']} item(s)"))
                return 0
            if action == "forget-topic":
                res = ltm.forget_topic(text, confirm=bool(getattr(args, "yes", False)))
                print(json.dumps(res, indent=2, ensure_ascii=False) if getattr(args, "json", False) else COLORS.green(res.get("message") or f"forgot {res['total_removed']} item(s)"))
                return 0
            if action == "compact":
                res = ltm.compact()
                print(json.dumps(res, indent=2, ensure_ascii=False) if getattr(args, "json", False) else COLORS.green(f"compacted {res['compacted']} entry(ies)"))
                return 0
            if action == "export":
                print(json.dumps(ltm.export_json(), indent=2, ensure_ascii=False))
                return 0
        except MemoryPolicyError as exc:
            print(COLORS.red(str(exc)))
            return 1

    store = MemoryStore(enabled=env_flag("NEXUS_MEMORY", True))
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
    if os.name == 'nt' and not getattr(args, 'check', False):
        from .updater import windows_handoff

        try:
            receipt = windows_handoff(hermes.repo_root(), pull=not getattr(args, 'no_pull', False),
                                      reinstall=not getattr(args, 'no_install', False), extras=getattr(args, 'extras', ''))
        except OSError:
            print(COLORS.red('Could not start Windows updater worker. No update completion is claimed.'))
            return 1
        print('Update scheduled, NOT completed. This launcher must exit before Windows can replace nexus.exe.')
        print(f'The worker will print the outcome and save a receipt: {receipt}')
        print('If updating from the REPL, exit it now. Keep other Nexus processes stopped.')
        return 0
    ok, messages = hermes.self_update(
        pull=not getattr(args, "no_pull", False),
        reinstall=not getattr(args, "no_install", False),
        check=getattr(args, "check", False),
        extras=getattr(args, "extras", ""),
        log=lambda msg: print(COLORS.dim(f"· {msg}")),
    )
    for message in messages:
        print(message)
    if ok:
        print(COLORS.green("✓ update checks completed"))
        return 0
    print(COLORS.red("update failed"))
    return 1


def cmd_sandbox(args: argparse.Namespace) -> int:
    """``nexus sandbox check`` — verify Docker daemon, digest-pinned image, and isolation flags."""
    from .sandbox import check_sandbox

    workspace = Path(args.workspace).resolve() if getattr(args, "workspace", None) else Path.cwd().resolve()
    action = getattr(args, "action", None) or "check"
    if action != "check":
        print(COLORS.red(f"unknown sandbox action: {action}"), file=sys.stderr)
        return 2
    report = check_sandbox(workspace)
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 1
    status_line = COLORS.green("✓ READY") if report["ok"] else COLORS.yellow("✗ NOT READY")
    print(COLORS.bold("Nexus Sandbox Check") + f"  {status_line}  " + COLORS.dim(report["workspace"]))
    if report["policy_error"]:
        print(COLORS.red(f"  policy error: {report['policy_error']}"))
    for item in report["checks"]:
        mark = COLORS.green("✓") if item["ok"] else COLORS.red("✗")
        print(f"  {mark} {item['name']:<22} {item['detail']}")
        if item.get("hint") and not item["ok"]:
            print(COLORS.dim(f"      hint: {item['hint']}"))
    return 0 if report["ok"] else 1


def cmd_audit(args: argparse.Namespace) -> int:
    """``nexus audit verify`` | ``nexus audit tail`` — inspect tamper-evident audit log."""
    from . import audit

    workspace = Path(args.workspace).resolve() if getattr(args, "workspace", None) else Path.cwd().resolve()
    action = getattr(args, "action", None) or "verify"
    if action == "tail":
        rows = audit.tail(
            workspace,
            limit=int(getattr(args, "limit", 20) or 20),
            action=str(getattr(args, "filter_action", "") or ""),
        )
        if getattr(args, "json", False):
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return 0
        if not rows:
            print(COLORS.dim("no audit events recorded yet"))
            return 0
        for row in rows:
            print(
                f"#{row.get('seq', '?'):>4}  {row.get('when', '')}  "
                f"{row.get('who', ''):<8}  {row.get('what', ''):<22}  "
                f"{row.get('result', ''):<10}  {row.get('target', '')}"
            )
        return 0

    report = audit.verify(workspace)
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report.get("ok") else 1
    if report.get("ok"):
        print(
            COLORS.green("✓ audit chain verified")
            + COLORS.dim(
                f"  ({report.get('events', report.get('checked', 0))} events · head {str(report.get('head', ''))[:16]} · {report.get('path')})"
            )
        )
        return 0
    print(COLORS.red("✗ audit chain verification FAILED") + COLORS.dim(f"  ({report.get('path')})"))
    if report.get("first_bad") is not None:
        print(COLORS.red(f"  first corrupted event seq: {report['first_bad']}"))
    if report.get("error"):
        print(COLORS.red(f"  {report['error']}"))
    return 1


def cmd_integrations(args: argparse.Namespace) -> int:
    from .personal_integrations import (
        INTEGRATION_PROVIDERS,
        grant_integration_token,
        integrations_status,
        revoke_integration_token,
    )

    workspace = Path(getattr(args, "workspace", None) or ".").resolve()
    action = getattr(args, "action", "status") or "status"
    try:
        if action == "status":
            info = integrations_status(workspace)
            if getattr(args, "json", False):
                print(json.dumps(info, indent=2, ensure_ascii=False))
                return 0
            state_lbl = COLORS.green("enabled") if info["enabled"] else COLORS.dim("disabled")
            print(f"Personal integrations ({state_lbl}) · keyring service: {info['keyring_service']}")
            for name, p in info["providers"].items():
                if name == "notes":
                    folders = len(p.get("obsidian_folders", [])) + len(p.get("notion_folders", []))
                    print(f"  notes      enabled={p['enabled']} folders={folders}")
                    continue
                scopes = ", ".join(p.get("granted_scopes") or []) or "(none)"
                print(
                    f"  {name:<10} enabled={p['enabled']} token_stored={p['token_stored']} "
                    f"granted_scopes={scopes}"
                )
            return 0

        if action == "grant":
            provider = getattr(args, "provider", "") or ""
            if provider not in INTEGRATION_PROVIDERS:
                print(COLORS.red(f"Provider must be one of: {', '.join(INTEGRATION_PROVIDERS)}"))
                return 2
            scopes_raw = getattr(args, "scopes", "") or ""
            scopes = [s.strip() for s in scopes_raw.split(",") if s.strip()]
            if not scopes:
                print(COLORS.red("Specify minimal scopes with --scopes, e.g. --scopes gmail.readonly"))
                return 2
            token = getattr(args, "token", "") or ""
            if getattr(args, "token_stdin", False):
                token = sys.stdin.read().strip()
            elif not token:
                import getpass

                token = getpass.getpass(f"OAuth access token for {provider}: ").strip()
            res = grant_integration_token(workspace, provider, token, scopes=scopes)
            if getattr(args, "json", False):
                print(json.dumps(res, indent=2, ensure_ascii=False))
                return 0
            print(COLORS.green(f"✓ Stored OAuth token for {provider} in keyring ({', '.join(res['granted_scopes'])})"))
            return 0

        if action == "revoke":
            target = getattr(args, "provider", "") or ("all" if getattr(args, "all", False) else "")
            if not target:
                print(COLORS.red("Specify a provider name or --all"))
                return 2
            res = revoke_integration_token(workspace, target)
            if getattr(args, "json", False):
                print(json.dumps(res, indent=2, ensure_ascii=False))
                return 0
            print(COLORS.green(f"✓ Revoked OAuth token(s) from keyring for {target}"))
            return 0

        if action == "search":
            from .knowledge import search_personal_sources

            q = " ".join(getattr(args, "query", None) or []).strip()
            src = getattr(args, "source", "all") or "all"
            sources = ("drive", "docs", "notes") if src == "all" else (src,)
            res = search_personal_sources(workspace, q, sources=sources)
            if getattr(args, "json", False):
                print(json.dumps(res, indent=2, ensure_ascii=False))
                return 0
            for item in res.get("results", []):
                print(f"{item['citation_tag']}  flags={item.get('injection_flags', [])}")
            return 0

    except (ValueError, ImportError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(COLORS.red(f"✗ {exc}"))
        return 2
    return 2


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
    from .assistant.cli import add_parser as add_assistant_parser
    add_assistant_parser(sub)
    from .telegram.cli import add_parser as add_telegram_parser
    add_telegram_parser(sub)
    from .context.cli import add_parser as add_context_parser
    add_context_parser(sub)
    from .vision.cli import add_parser as add_vision_parser
    add_vision_parser(sub)
    from .voice.cli import add_parser as add_voice_parser
    add_voice_parser(sub)
    from .evaluation.cli import add_parser as add_evaluation_parser
    add_evaluation_parser(sub)
    from .evaluation.cli import add_trace_parser
    add_trace_parser(sub)

    p = sub.add_parser("eval", help="run the offline evaluation suite (golden missions, red-team, latency benchmarks)")
    p.add_argument("action", nargs="?", default="run", choices=["run", "report"])
    p.add_argument("--workspace", "-C")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--format", default="json", choices=["json", "markdown"])
    p.add_argument("--fail-on-regression", action="store_true", default=True,
                   help="exit non-zero when any check fails or a latency budget regresses")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("integrations", help="inspect, store (keyring), revoke, or search scoped personal integrations")
    int_sub = p.add_subparsers(dest="action", required=True)
    ip_status = int_sub.add_parser("status", help="show integration status and granted keyring scopes")
    ip_status.add_argument("--workspace", "-C")
    ip_status.add_argument("--json", action="store_true")
    ip_grant = int_sub.add_parser("grant", help="store a minimal-scope OAuth token in OS keyring")
    ip_grant.add_argument("provider", choices=["gmail", "calendar", "contacts", "drive", "docs"])
    ip_grant.add_argument("--scopes", required=True, help="comma-separated minimal scopes")
    ip_grant.add_argument("--token", default="", help="OAuth token (prefer --token-stdin or interactive prompt)")
    ip_grant.add_argument("--token-stdin", action="store_true", help="read OAuth token from stdin")
    ip_grant.add_argument("--workspace", "-C")
    ip_grant.add_argument("--json", action="store_true")
    ip_revoke = int_sub.add_parser("revoke", help="revoke OAuth tokens and scopes from keyring")
    ip_revoke.add_argument("provider", nargs="?", default="")
    ip_revoke.add_argument("--all", action="store_true", help="revoke all integration tokens in keyring")
    ip_revoke.add_argument("--workspace", "-C")
    ip_revoke.add_argument("--json", action="store_true")
    ip_search = int_sub.add_parser("search", help="search Drive, Docs, and local Obsidian/Notion notes with citations")
    ip_search.add_argument("query", nargs="+")
    ip_search.add_argument("--source", default="all", choices=["all", "drive", "docs", "notes"])
    ip_search.add_argument("--workspace", "-C")
    ip_search.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_integrations)

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
    from .chief_cli import add_parser as add_chief_parser
    add_chief_parser(sub)
    from .github_cli import add_parser as add_github_parser
    add_github_parser(sub)
    from .computer_cli import add_parser as add_computer_parser
    add_computer_parser(sub)
    from .security_assistant.cli import add_parser as add_security_parser
    add_security_parser(sub)
    from .always_on import add_parser as add_service_parser
    add_service_parser(sub)
    from .jarvis.cli import add_parser as add_jarvis_parser
    add_jarvis_parser(sub)

    p = sub.add_parser("roles", help="list / search / inspect roles")
    p.add_argument("--workspace", "-C")
    p.add_argument("--search", "-s")
    p.add_argument("--show")
    p.add_argument("--category", "-c")
    p.add_argument("--team", help="only roles of this team (see `nexus teams`)")
    p.add_argument("--versions", action="store_true", help="list role/persona snapshot versions")
    p.add_argument("--rollback", metavar="STAMP", help="preview restoring roles.yaml/persona.yaml from this snapshot")
    p.add_argument("--execute", action="store_true", help="with --rollback: restore for real")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_roles)

    p = sub.add_parser("stop", help="latch the emergency stop and cancel all work")
    p.add_argument("--all", action="store_true",
                   help="also pause the Jarvis daemon / scheduler (best-effort)")
    p.add_argument("--workspace", "-C")
    p.add_argument("--json", action="store_true", help="machine readable output")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("resume", help="clear the emergency stop latch (operator-only)")
    p.add_argument("--workspace", "-C")
    p.add_argument("--json", action="store_true", help="machine readable output")
    p.set_defaults(func=cmd_resume)

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

    p = sub.add_parser("teams", help="the 48-team roster: list / show / role / check")
    p.add_argument("action", nargs="?", default="list", choices=["list", "show", "role", "check"])
    p.add_argument("target", nargs="?", help="team id/number (show) or role name (role)")
    p.add_argument("--stage", help="only this stage when showing a team")
    p.add_argument("--workspace", "-C")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_teams)

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
    p.add_argument("--offline", action="store_true", help="local policy/service health only; no provider/network/shell probes")
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

    p = sub.add_parser("skills", help="skills: the agent's procedural memory (list/show/import/curate/stage/approve/rollback)")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "show", "import", "curate", "delete", "stage", "review", "approve", "rollback", "path"])
    p.add_argument("name", nargs="?", help="skill name (show / delete / stage / approve / rollback)")
    p.add_argument("--workspace", "-C")
    p.add_argument("--search", "-s", help="filter by name/description/tag")
    p.add_argument("--apply", action="store_true", help="curate: archive the stale ones")
    p.add_argument("--stale-days", type=int, default=45)
    p.add_argument("--offline", action="store_true", help="import: use the local checkout, do not pull")
    p.add_argument("--no-optional", action="store_true", help="import: skip optional-skills")
    p.add_argument("--force", action="store_true", help="import: overwrite existing skills / stage: overwrite")
    p.add_argument("--description", help="stage: one-sentence skill description")
    p.add_argument("--body-file", help="stage: file with the SKILL.md body")
    p.add_argument("--body", help="stage: inline SKILL.md body")
    p.add_argument("--tags", help="stage: comma-separated tags")
    p.add_argument("--diff-file", help="stage: diff/patch file for review")
    p.add_argument("--tests-file", help="stage: pytest file exercising the skill")
    p.add_argument("--execute", action="store_true",
                   help="approve/roll back for real - without it commands are a dry-run preview")
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

    p = sub.add_parser("memory", help="long-term memory: MEMORY.md + USER.md + personal memory")
    p.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=[
            "list", "add", "remove", "import", "path",
            "personal", "remember", "search", "confirm", "reject",
            "forget", "forget-topic", "compact", "export",
        ],
    )
    p.add_argument("text", nargs="?", help="the entry, query, id, or topic")
    p.add_argument("--user", action="store_true", help="target USER.md (preferences) instead of MEMORY.md")
    p.add_argument("--source", help="import: directory holding MEMORY.md / USER.md")
    p.add_argument("--workspace", "-C")
    p.add_argument("--yes", "-y", action="store_true", help="confirm destructive topic forget or explicit sensitive store")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_memory)

    p = sub.add_parser("hermes", help="bridge to Nous Research's hermes-agent (skills, memories, CLI)")
    p.add_argument("action", nargs="?", default="status", choices=["status", "install", "sync", "run"])
    p.add_argument("argv", nargs="*", help="arguments for `nexus hermes run`")
    p.add_argument("--memories", help="sync: Hermes memories directory")
    p.add_argument("--force", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_hermes)

    p = sub.add_parser("update", help="update from trusted GitHub main and install dependencies")
    p.add_argument("--check", action="store_true", help="check trusted source without installing")
    p.add_argument("--extras", default="", help="optional dependency groups, e.g. hud,mcp,context")
    p.add_argument("--no-pull", action="store_true")
    p.add_argument("--no-install", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("sandbox", help="check Docker sandbox configuration and invariants")
    p.add_argument("action", nargs="?", default="check", choices=["check"])
    p.add_argument("--workspace", "-C")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sandbox)

    p = sub.add_parser("audit", help="verify or inspect the tamper-evident hash-chained audit log")
    p.add_argument("action", nargs="?", default="verify", choices=["verify", "tail"])
    p.add_argument("--limit", "-n", type=int, default=20)
    p.add_argument("--action", dest="filter_action", default="", help="filter by action prefix (e.g. tool.)")
    p.add_argument("--workspace", "-C")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_audit)

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
    from .redaction import install_log_redaction
    install_log_redaction(workspace)
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
