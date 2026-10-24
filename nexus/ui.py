"""The terminal UI.

The CLI is not a bare ``print()`` loop: while a model is thinking you get a live
spinner, tokens appear as they stream in, every tool call is a one-line card,
and each turn ends with a compact footer (time, turns, tools, tokens).

Everything here degrades gracefully: when stdout is not a TTY (pipes, CI, the
test suite) the spinner and the cursor tricks switch themselves off, but the
content is identical.
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
from typing import Any

from .types import Event, Risk

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
TOOL_ICON = {Risk.READ: "·", Risk.WRITE: "✎", Risk.DANGEROUS: "⚠"}


def _tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:  # noqa: BLE001 - closed stdout in tests
        return False


def _width(default: int = 78) -> int:
    try:
        return max(40, min(shutil.get_terminal_size((default, 24)).columns, 110))
    except Exception:  # noqa: BLE001
        return default


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fmt_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


class TerminalUI:
    """Event renderer used by ``nexus`` / ``nexus chat`` / ``nexus task``."""

    def __init__(self, colors: Any, *, spinner: bool | None = None, stream: bool = True):
        self.c = colors
        self.spinner_enabled = _tty() if spinner is None else spinner
        self.stream_enabled = stream
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._label = "thinking"
        self._started = 0.0
        self._streaming = False
        self._usage: tuple[int, int] = (0, 0)
        self._model = ""

    # -- spinner ----------------------------------------------------------- #
    def start_spinner(self, label: str = "thinking") -> None:
        if not self.spinner_enabled:
            return
        self.stop_spinner()
        self._label = label or "thinking"
        self._started = time.time()
        self._stop = threading.Event()

        def run() -> None:
            index = 0
            while not self._stop.is_set():
                elapsed = time.time() - self._started
                frame = SPINNER_FRAMES[index % len(SPINNER_FRAMES)]
                line = f"\r{self.c.cyan(frame)} {self.c.dim(self._label)}  {self.c.dim(f'{elapsed:4.1f}s')}"
                try:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                except Exception:  # noqa: BLE001 - stdout closed
                    return
                index += 1
                time.sleep(0.12 if elapsed < 2 else 0.25)

        self._thread = threading.Thread(target=run, name="nexus-spinner", daemon=True)
        self._thread.start()

    def stop_spinner(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=0.5)
        self._thread = None
        if self.spinner_enabled:
            try:
                sys.stdout.write("\r" + " " * (_width() - 2) + "\r")
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                pass

    # -- event handler ----------------------------------------------------- #
    def __call__(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.type.value}", None)
        if handler is not None:
            handler(event.data)
        else:  # unknown/new event types must never crash a run
            self._on_status(event.data)

    # individual events
    def _on_status(self, d: dict[str, Any]) -> None:
        self.stop_spinner()
        message = d.get("message")
        if message:
            print(self.c.dim(f"· {message}"))

    def _on_role(self, d: dict[str, Any]) -> None:
        self.stop_spinner()
        method = d.get("method") or ""
        bits = [
            self.c.cyan("⇄ role: ") + self.c.bold(str(d.get("role") or "?")),
            self.c.dim("→ agent: " + str(d.get("agent") or "?")),
            self.c.dim(f"[{method}]" if method else ""),
        ]
        hits = d.get("hits") or []
        if hits:
            bits.append(self.c.dim("hits: " + ", ".join(hits[:5])))
        if d.get("note"):
            bits.append(self.c.dim(f"({d['note']})"))
        print(" ".join(b for b in bits if b))

    def _on_agent(self, d: dict[str, Any]) -> None:
        self.stop_spinner()
        if d.get("error"):
            print(self.c.yellow(f"⚠ {d.get('agent')} failed: {_short(d['error'], 160)}"))
            return
        if d.get("message"):
            print(self.c.blue(f"⇢ {d['message']}"))
            return
        if d.get("model"):
            self._model = str(d.get("model"))
            mode = d.get("tool_mode")
            line = (
                f"{self.c.blue('◆')} {self.c.bold(str(d.get('agent')))}"
                f" {self.c.dim('·')} {self.c.cyan(self._model)}"
                f" {self.c.dim('·')} {self.c.dim(str(d.get('provider') or ''))}"
            )
            if mode and mode != "native":
                line += self.c.dim(f" · tools:{mode}")
            print(line)

    def _on_think(self, d: dict[str, Any]) -> None:
        if d.get("agent"):
            self._model = str(d.get("model") or self._model)
        turn = d.get("turn")
        total = d.get("max_turns")
        label = f"{d.get('agent') or 'agent'} · {_short(str(d.get('model') or ''), 28)}"
        if turn and total:
            label += f" · turn {turn}/{total}"
        self.start_spinner(label)

    def _on_text_delta(self, d: dict[str, Any]) -> None:
        piece = d.get("text") or ""
        if not piece:
            return
        self.stop_spinner()
        if not self._streaming:
            self._streaming = True
            print(self.c.dim("┌─ response " + "─" * max(0, _width() - 13)))
        sys.stdout.write(self.c.green(piece))
        sys.stdout.flush()

    def _on_text(self, d: dict[str, Any]) -> None:
        self.stop_spinner()
        text = (d.get("text") or "").strip()
        if self._streaming:
            # the deltas are already on screen - just close the box
            self._streaming = False
            print()
            print(self.c.dim("└" + "─" * max(0, _width() - 1)))
            if not text:
                return
            return
        if text:
            print(self.c.green(text))
            print()

    def _on_tool_call(self, d: dict[str, Any]) -> None:
        self.stop_spinner()
        risk = str(d.get("risk") or "write")
        try:
            icon = TOOL_ICON.get(Risk(risk), "•")
        except ValueError:
            icon = "•"
        preview = _short(d.get("preview") or "", _width() - len(str(d.get("tool"))) - 6)
        print()
        print(f"{self.c.magenta(icon + ' ' + str(d.get('tool')))}  {self.c.dim(preview)}")

    def _on_tool_result(self, d: dict[str, Any]) -> None:
        self.stop_spinner()
        flag = self.c.red("✗") if d.get("is_error") else self.c.green("✓")
        preview = _short(d.get("preview") or "", _width() - 6)
        print(f"  {flag} {self.c.dim(preview)}")

    def _on_usage(self, d: dict[str, Any]) -> None:
        self._usage = (int(d.get("input_tokens") or 0), int(d.get("output_tokens") or 0))
        if d.get("model"):
            self._model = str(d["model"])

    def _on_error(self, d: dict[str, Any]) -> None:
        self.stop_spinner()
        print(self.c.red(f"✗ {d.get('message') or 'error'}"))

    def _on_memory(self, d: dict[str, Any]) -> None:
        self.stop_spinner()
        print(self.c.dim(f"🧠 memory saved · {d.get('role')} → {d.get('path')}"))

    def _on_approval(self, d: dict[str, Any]) -> None:  # handled by the prompt
        self.stop_spinner()

    def _on_done(self, d: dict[str, Any]) -> None:
        self.stop_spinner()

    # -- chrome ------------------------------------------------------------ #
    def prompt(self, hint: str) -> str:
        self.stop_spinner()
        self._streaming = False
        return self.c.bold(f"nexus[{hint}]") + self.c.cyan(" ❯ ")

    @property
    def usage(self) -> tuple[int, int]:
        return self._usage


# --------------------------------------------------------------------------- #
# banner / footer / help
# --------------------------------------------------------------------------- #

LOGO = (
    " _  _ _____ _   _ _   _ ____  ",
    "|\\ | | ____| | | | | | / ___| ",
    "| \\| |  _| | | | | | | \\___ \\ ",
    "| |\\  | |___| |_| | |_| |___) |",
    "|_| \\_|_____|\\___/ \\___/|____/ ",
)


def banner(colors: Any, agent: Any, *, version: str, workspace: str) -> None:
    """The screen you get when you type just ``nexus``."""
    cfg = agent.config
    width = min(_width(), 92)
    inner = width - 4
    lines: list[tuple[str, str, str]] = []

    def add(label: str, value: str, color: str = "") -> None:
        lines.append((label, str(value), color))

    add("workspace", workspace)
    add("agent", f"{cfg.default_agent} · {cfg.agents[cfg.default_agent].model}"
        if cfg.default_agent in cfg.agents else cfg.default_agent, "cyan")
    add("team", f"{len(cfg.roles)} roles · {len(cfg.agents)} model endpoints", "cyan")
    add("routing", "auto" if cfg.settings.auto_route else "manual")
    configured = [fam for fam in cfg.key_families() if _has_key(fam)]
    add("keys", ", ".join(configured) if configured else "none yet - /keys set gemini", 
        "green" if configured else "yellow")
    add("approval", agent.gate.mode)
    add("mode", "local-first" if cfg.settings.prefer_local else "hosted")

    top = "╭" + "─" * (width - 2) + "╮"
    bottom = "╰" + "─" * (width - 2) + "╯"
    print()
    print(colors.cyan(top))
    for art in LOGO:
        print(colors.cyan("│ ") + colors.bold(art.ljust(width - 4)) + colors.cyan(" │"))
    print(colors.cyan("│ ") + colors.dim("one CLI · a whole team of routed specialists".ljust(width - 4)) + colors.cyan(" │"))
    print(colors.cyan("├" + "─" * (width - 2) + "┤"))
    for label, value, color in lines:
        label_str = f"{label:<10}"
        room = max(8, inner - len(label_str) - 1)
        if len(value) > room:   # long workspace paths must not break the box
            value = "…" + value[-(room - 1):] if room > 1 else value[:room]
        pad = max(0, room - len(value))
        shown = getattr(colors, color)(value) if color else value
        print(colors.cyan("│ ") + colors.dim(label_str) + " " + shown + " " * pad + colors.cyan(" │"))
    print(colors.cyan("├" + "─" * (width - 2) + "┤"))
    hints = "type a task · /help commands · /gui dashboard · /exit quit"
    print(colors.cyan("│ ") + colors.dim(hints[:width - 4].ljust(width - 4)) + colors.cyan(" │"))
    print(colors.cyan(bottom))
    print(colors.dim(f"  Nexus v{version}"))


def _has_key(family: str) -> bool:
    from . import keys as keyring

    try:
        return bool(keyring.get_key(family))
    except Exception:  # noqa: BLE001
        return False


def _strip_ansi(text: str) -> str:
    out = []
    skip = False
    for ch in text:
        if ch == "\033":
            skip = True
            continue
        if skip:
            if ch.isalpha():
                skip = False
            continue
        out.append(ch)
    return "".join(out)


def print_footer(colors: Any, result: Any) -> None:
    """One compact line under every turn: what it cost and how it went."""
    if not getattr(result, "turns", 0):
        return
    bits = [
        f"⏱ {result.duration_s:.1f}s",
        f"{result.turns} turn{'s' if result.turns != 1 else ''}",
        f"{result.tool_calls} tool{'s' if result.tool_calls != 1 else ''}",
    ]
    total = result.usage.input_tokens + result.usage.output_tokens
    if total:
        bits.append(f"{fmt_tokens(total)} tok")
    if result.changed_files:
        bits.append(f"{len(result.changed_files)} file{'s' if len(result.changed_files) != 1 else ''}")
    bits.append(f"{result.role} → {result.agent}")
    if not result.ok:
        bits.append(result.stopped_reason or "failed")
    print(colors.dim("  " + " · ".join(bits)))


HELP_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("session", (
        ("/help", "this help"),
        ("/exit", "quit (also /quit, Ctrl-D)"),
        ("/new", "fresh session: history, role pin, approvals"),
        ("/clear", "drop the conversation, keep the settings"),
        ("/save [file]", "write the transcript to a markdown file"),
        ("/stats", "turns, tokens, tools for this session"),
        ("/compact", "trim old tool output from the context"),
    )),
    ("team & models", (
        ("/roles [needle]", "list or search the team"),
        ("/role <name>", "pin a role (auto-routing off)"),
        ("/auto [on|off]", "toggle automatic routing"),
        ("/agents", "configured model endpoints"),
        ("/agent <name>", "point the active role at another agent"),
        ("/model <id>", "override the model for this session"),
        ("/models [agent]", "model ids the provider actually serves"),
        ("/addagent <url>", "import agents from a GitHub repo"),
    )),
    ("run", (
        ("/ask <question>", "read-only answer (no writes)"),
        ("/plan <task>", "same, framed as an implementation plan"),
        ("/task <task>", "full autonomous run"),
        ("/pipeline <name> <task>", "multi-role handoff"),
    )),
    ("local & UI", (
        ("/local [--pull]", "Ollama status: hermes / uncensored models"),
        ("/gui [port]", "start the GUI dashboard and print its URL"),
        ("/keys [set|test]", "API keys (no .env file)"),
        ("/doctor", "health check"),
        ("/system", "the exact system prompt in use"),
        ("/tools", "tools available to the active role"),
        ("/memory [role]", "saved role memory"),
        ("/remember <text>", "save a note for the active role"),
    )),
)


def help_text(colors: Any) -> str:
    lines = [colors.bold("Nexus REPL commands"), ""]
    for title, rows in HELP_SECTIONS:
        lines.append(colors.cyan(f"  {title}"))
        for cmd, desc in rows:
            lines.append(f"    {colors.bold(cmd.ljust(24))} {colors.dim(desc)}")
        lines.append("")
    lines.append(colors.dim("  Anything that does not start with a / is sent to the agent."))
    return "\n".join(lines)
