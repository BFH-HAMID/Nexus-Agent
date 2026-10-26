"""The agent loop: route -> build persona -> call model -> run tools -> repeat.

One class (:class:`NexusAgent`) drives everything the CLI, the pipeline runner
and the VS Code sidebar server need.
"""

from __future__ import annotations

import os
import platform
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import NexusConfig, Pipeline, load_config
from .learning import LearningLoop, nudge_interval, skills_index_for
from .memory import MemoryStore, env_flag
from .providers import ProviderRegistry, build_providers
from .router import RouteDecision, Router
from .sessions import SessionStore
from .tools import (
    READ_ONLY_TOOLS,
    ApprovalCallback,
    ApprovalGate,
    PathGuard,
    RoleMemory,
    ToolContext,
    build_tools,
)
from .types import (
    Event,
    EventHandler,
    EventType,
    Message,
    NullHandler,
    Role,
    TextBlock,
    ToolResultBlock,
    Usage,
)

BASE_SYSTEM = """You are Nexus, a multi-specialist software agent working inside a real repository.
You act as the role you are given below and you get things DONE with the provided tools.

Ground rules
- Work inside the workspace directory given below. Read before you write.
- Prefer small, surgical `edit_file` changes over rewriting whole files.
- Verify your work: run the tests, the linter or the build when they exist.
- Never invent file contents, command output or API responses.
- Never print API keys or secrets from the environment.
- If the task is ambiguous in a way that changes the outcome, ask ONE focused question
  and stop; otherwise pick the sensible option and state your assumption.
- Finish by calling the `finish` tool with a short summary (or by replying with plain prose
  when no tools are needed).
"""


def render_system_prompt(
    *,
    role: Role,
    agent_label: str,
    workspace: Path,
    memory_text: str = "",
    tools_hint: str = "",
    plan_mode: bool = False,
    extra: str = "",
) -> str:
    parts = [BASE_SYSTEM.strip()]
    parts.append(f"# Active role: {role.name}" + (f" ({role.title})" if role.title else ""))
    if role.persona:
        parts.append(role.persona.strip())
    if role.focus:
        parts.append("Focus areas: " + ", ".join(role.focus))
    if role.extra_instructions:
        parts.append(role.extra_instructions.strip())
    parts.append(f"# Runtime\n- workspace: {workspace}\n- model/agent: {agent_label}\n- os: {platform.system()}")
    if tools_hint:
        parts.append(f"# Tools available\n{tools_hint}")
    if memory_text:
        parts.append(
            f"# Your role memory ({role.name}) - decisions from earlier sessions in this repo\n{memory_text}"
        )
    if plan_mode:
        parts.append(
            "# PLAN MODE\nYou may only inspect the repository. Do not attempt to write files or run "
            "mutating commands - they will be denied. Produce a concrete, ordered plan with exact "
            "file paths and the change you would make in each."
        )
    if extra:
        parts.append(extra.strip())
    return "\n\n".join(parts)


@dataclass
class RunResult:
    text: str = ""
    role: str = "general"
    agent: str = ""
    method: str = ""
    turns: int = 0
    usage: Usage = field(default_factory=Usage)
    tool_calls: int = 0
    changed_files: list[str] = field(default_factory=list)
    summary: str = ""
    followups: list[str] = field(default_factory=list)
    stopped_reason: str = ""
    duration_s: float = 0.0
    messages: list[Message] = field(default_factory=list)
    ok: bool = True
    learned: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""


class NexusAgent:
    def __init__(
        self,
        config: NexusConfig | None = None,
        *,
        workspace: Path | str | None = None,
        providers: ProviderRegistry | None = None,
        emit: EventHandler | None = None,
        approval_callback: ApprovalCallback | None = None,
        auto_yes: bool | None = None,
        allow_outside: bool = False,
        allow_dangerous: bool | None = None,
    ):
        self.ws = Path(workspace or os.environ.get("NEXUS_WORKSPACE") or Path.cwd()).resolve()
        self.config = config or load_config(self.ws)
        self.config.settings.workspace = self.ws
        self.providers = providers or build_providers(self.config.agents)
        self.emit: EventHandler = emit or NullHandler()
        self.router = Router(self.config, self.providers, emit=self.emit)
        self.guard = PathGuard(self.ws, allow_outside=allow_outside)

        mode = "auto" if (auto_yes if auto_yes is not None else self.config.settings.auto_yes) else "ask"
        self.gate = ApprovalGate(
            mode=mode,
            allow_dangerous=(
                allow_dangerous if allow_dangerous is not None else self.config.settings.allow_dangerous
            ),
            always_allow=self.config.settings.always_allow_tools,
            never_allow=self.config.settings.never_allow_tools,
            extra_blocked_patterns=self.config.settings.blocked_command_patterns,
            callback=approval_callback,
            emit=self.emit,
        )
        self.memory = RoleMemory(
            self.ws,
            self.config.settings.memory_dir,
            max_chars=self.config.settings.memory_max_chars,
            enabled=self.config.settings.memory_enabled,
        )

        # chat session state
        self.sticky_role: str | None = None
        self.auto_route: bool = self.config.settings.auto_route
        self.history: list[Message] = []
        self.session_usage = Usage()
        self.active_role: Role = self.config.role(self.config.default_role_name())
        self.plan_mode = False

        # learning surfaces (Hermes-style): skills, long-term memory, recall
        settings = self.config.settings
        self.skills_enabled = bool(getattr(settings, "skills_enabled", True))
        self.memory_store = MemoryStore(enabled=env_flag("NEXUS_MEMORY", bool(settings.memory_enabled)))
        self.sessions = SessionStore()
        self.learning = LearningLoop(self)
        self._turns_since_nudge = 0
        self.pending_skill: str | None = None

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def event(self, type_: EventType, **data: Any) -> None:
        self.emit(Event(type_, data))

    def status(self, message: str, **data: Any) -> None:
        self.event(EventType.STATUS, message=message, **data)

    def set_role(self, name: str) -> Role:
        role = self.config.role(name)
        self.sticky_role = role.name
        self.active_role = role
        self.event(EventType.ROLE, role=role.name, agent=role.agent, method="manual")
        return role

    def set_agent(self, name: str) -> None:
        """Point the active role at a different agent (session-scoped)."""
        self.config.agent(name)  # raises if unknown
        self.active_role.agent = name
        if self.sticky_role:
            self.config.roles.roles[self.sticky_role].agent = name
        self.event(EventType.AGENT, agent=name, message=f"active agent -> {name}")

    def provider_for(self, role: Role):
        return self.providers.get(self.config.agent_for_role(role).name)

    def reload_config(self) -> int:
        """Re-read config from disk (new roles.d packs, edited roles.yaml) and
        rebuild the router, keeping the live session intact.

        Returns the new role count. Used by ``/addagent`` so freshly imported
        agents are routable immediately without restarting the REPL.
        """
        preserved_role = self.sticky_role
        self.config = load_config(self.ws)
        self.config.settings.workspace = self.ws
        self.router = Router(self.config, self.providers, emit=self.emit)
        self.gate.always_allow = set(self.config.settings.always_allow_tools)
        self.gate.never_allow = set(self.config.settings.never_allow_tools)
        self.memory = RoleMemory(
            self.ws,
            self.config.settings.memory_dir,
            max_chars=self.config.settings.memory_max_chars,
            enabled=self.config.settings.memory_enabled,
        )
        if preserved_role and preserved_role in self.config.roles:
            self.active_role = self.config.role(preserved_role)
            self.sticky_role = preserved_role
        else:
            self.sticky_role = None
            self.active_role = self.config.role(self.config.default_role_name())
        self.status(
            f"config reloaded - {len(self.config.roles)} roles available",
            roles=len(self.config.roles),
        )
        return len(self.config.roles)

    # ------------------------------------------------------------------ #
    def build_context(self, role: Role, *, read_only: bool = False, depth: int = 0) -> ToolContext:
        return ToolContext(
            workspace=self.ws,
            guard=self.guard,
            gate=self.gate,
            config=self.config,
            providers=self.providers,
            emit=self.emit,  # tool_call / tool_result events must reach the CLI + sidebar
            max_output_chars=self.config.settings.max_output_chars,
            max_read_chars=self.config.settings.max_read_chars,
            command_timeout=self.config.settings.command_timeout,
            ask_agent_depth=depth,
            memory_store=self.memory_store,
            sessions=self.sessions,
        )

    def tools_for(self, role: Role, *, read_only: bool = False, depth: int = 0):
        """Build the tool surface for a role.

        ``read_only`` is enforced twice: mutating tools are removed from the
        registry entirely, and the gate runs in plan mode.
        """
        ctx = self.build_context(role, read_only=read_only, depth=depth)
        registry = build_tools(ctx, role=role.name, include_ask_agent=depth == 0)
        if role.tools:
            registry.keep_only(set(role.tools))
        if read_only:
            registry.keep_only(READ_ONLY_TOOLS)
        return ctx, registry, registry.specs()

    # ------------------------------------------------------------------ #
    # main task loop
    # ------------------------------------------------------------------ #
    def run(
        self,
        task: str,
        *,
        role: str | None = None,
        read_only: bool = False,
        max_turns: int | None = None,
        history: Sequence[Message] | None = None,
        extra_system: str = "",
        learn: bool = True,
    ) -> RunResult:
        started = time.time()
        decision: RouteDecision
        if role:
            decision = self.router.route(task, forced=role)
        elif self.sticky_role and not self.auto_route:
            decision = self.router.route(task, forced=self.sticky_role)
        elif self.sticky_role and self.auto_route:
            decision = self.router.route(task)
            if decision.role.name == "general" and self.sticky_role:
                # keep the human's explicit choice when routing is unsure
                decision = self.router.route(task, forced=self.sticky_role)
        else:
            decision = self.router.route(task)

        active_role = decision.role
        self.active_role = active_role
        agent_cfg = self.config.agent_for_role(active_role)
        if decision.note:
            self.status(decision.note)
        self.event(
            EventType.AGENT,
            agent=agent_cfg.name,
            model=agent_cfg.model,
            provider=agent_cfg.provider,
            role=active_role.name,
            tool_mode=agent_cfg.tool_mode,
        )

        ctx, registry, specs = self.tools_for(active_role, read_only=read_only)
        memory_text = self.memory.read(active_role.name)
        tools_hint = "\n".join(f"- {s.name}: {s.description.splitlines()[0]}" for s in specs) or "(none)"

        extras: list[str] = []
        skills_section, selected_skills = "", []
        if self.skills_enabled:
            skills_section, selected_skills = skills_index_for(self, task)
            if self.pending_skill:
                pinned = next((s for s in selected_skills if s.name == self.pending_skill), None)
                from . import skills as skilllib

                pinned = pinned or skilllib.get(self.pending_skill, self.ws)
                if pinned is not None:
                    pinned.record_use()
                    skills_section = (
                        f"# Active skill: {pinned.name}\n{pinned.body}\n\n"
                        "Follow this procedure unless the task contradicts it.\n\n" + skills_section
                    )
                self.pending_skill = None
            if skills_section:
                extras.append(skills_section)
        global_memory = self.memory_store.prompt_sections()
        if global_memory:
            extras.append(global_memory)
        interval = nudge_interval(self.config.settings)
        if interval and self._turns_since_nudge >= interval:
            extras.append(self.learning.nudge())
            self._turns_since_nudge = 0
        if extra_system:
            extras.append(extra_system)
        system = render_system_prompt(
            role=active_role,
            agent_label=f"{agent_cfg.name} / {agent_cfg.model}",
            workspace=self.ws,
            memory_text=memory_text,
            tools_hint=tools_hint,
            plan_mode=read_only or self.plan_mode,
            extra="\n\n".join(extras),
        )

        messages: list[Message] = list(history or [])
        messages.append(Message(role="user", content=task))

        limit = max_turns or self.config.settings.max_turns
        usage = Usage()
        tool_calls = 0
        final_text = ""
        stop_reason = ""
        consecutive_errors = 0
        turn = 0

        while turn < limit:
            turn += 1
            self._maybe_compact(messages)
            # let the GUI/CLI show "who is thinking right now"
            self.event(
                EventType.THINK,
                phase="thinking",
                turn=turn,
                max_turns=limit,
                role=active_role.name,
                agent=agent_cfg.name,
                model=agent_cfg.model,
                provider=agent_cfg.provider,
                pending_tools=len(specs),
            )
            streamed_parts: list[str] = []
            use_stream = bool(self.config.settings.stream and getattr(agent_cfg, "stream", True))

            def _on_text(piece: str, _role=active_role.name, _sink=streamed_parts) -> None:
                if not piece:
                    return
                _sink.append(piece)
                self.event(EventType.TEXT_DELTA, text=piece, role=_role)

            try:
                provider, completion = self.providers.call_with_fallback(
                    agent_cfg.name,
                    system,
                    messages,
                    specs,
                    max_tokens=agent_cfg.max_tokens,
                    temperature=active_role.temperature,
                    model=active_role.model,
                    on_event=self.emit,
                    on_text=_on_text if use_stream else None,
                )
            except Exception as exc:  # noqa: BLE001
                self.event(EventType.ERROR, message=str(exc))
                failed = RunResult(
                    text=final_text,
                    role=active_role.name,
                    agent=agent_cfg.name,
                    method=decision.method,
                    turns=turn,
                    usage=usage,
                    tool_calls=tool_calls,
                    changed_files=sorted(ctx.state.get("changed_files", set())),
                    summary=ctx.state.get("summary", ""),
                    followups=ctx.state.get("followups", []),
                    stopped_reason=f"provider error: {exc}",
                    duration_s=time.time() - started,
                    messages=messages,
                    ok=False,
                )
                return self._after_run(failed, task, learn=False)

            usage.add(Usage(completion.input_tokens, completion.output_tokens, 1))
            self.session_usage.add(Usage(completion.input_tokens, completion.output_tokens, 1))
            self.event(
                EventType.USAGE,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                call_input_tokens=completion.input_tokens,
                call_output_tokens=completion.output_tokens,
                calls=usage.calls,
                model=completion.model or agent_cfg.model,
            )
            agent_cfg = self.config.agents.get(provider.name, agent_cfg)

            if completion.text or streamed_parts:
                if completion.text:
                    final_text = completion.text
                self.event(
                    EventType.TEXT,
                    text=completion.text,
                    role=active_role.name,
                    streamed=bool(streamed_parts),
                )

            assistant_blocks = list(completion.content)
            if not assistant_blocks:
                assistant_blocks = [TextBlock(text="(empty response)")]
            messages.append(Message(role="assistant", content=assistant_blocks))

            calls = completion.tool_calls
            if not calls:
                stop_reason = completion.stop_reason or "end_turn"
                break

            results: list[Any] = []
            for call in calls:
                # A `finish` call ends the run, but every tool_use block still
                # needs its own tool_result or the next request is rejected by
                # strict APIs (Anthropic, Gemini). Answer the leftovers.
                if ctx.state.get("finished"):
                    results.append(ToolResultBlock(call.id, "skipped: the run already finished", False))
                    continue
                text, is_error = registry.call(call.name, call.input)
                results.append(ToolResultBlock(call.id, text, is_error))
                tool_calls += 1
                consecutive_errors = consecutive_errors + 1 if is_error else 0
            if consecutive_errors == 3:
                # appended as plain text so no phantom tool_use_id is invented
                results.append(
                    TextBlock(
                        text="SYSTEM: three tool errors in a row. Re-read the relevant file, or stop "
                        "and explain the blocker to the user instead of retrying the same call."
                    )
                )
            messages.append(Message(role="user", content=results))

            if ctx.state.get("finished"):
                stop_reason = "finish"
                final_text = ctx.state.get("summary") or final_text
                break
            if consecutive_errors >= 6:
                stop_reason = "too many tool errors"
                break
        else:
            stop_reason = f"max_turns ({limit}) reached"
            self.status(f"stopping: {stop_reason}")

        result = RunResult(
            text=final_text,
            role=active_role.name,
            agent=agent_cfg.name,
            method=decision.method,
            turns=turn,
            usage=usage,
            tool_calls=tool_calls,
            changed_files=sorted(ctx.state.get("changed_files", set())),
            summary=ctx.state.get("summary", ""),
            followups=ctx.state.get("followups", []),
            stopped_reason=stop_reason,
            duration_s=time.time() - started,
            messages=messages,
        )
        return self._after_run(result, task, learn=learn)

    # ------------------------------------------------------------------ #
    # learning loop + session history
    # ------------------------------------------------------------------ #
    def _after_run(self, result: RunResult, task: str, *, learn: bool = True) -> RunResult:
        """Record the run, then learn from it (skills/memory) when it paid off."""
        try:
            result.session_id = self.sessions.record(result, task, workspace=self.ws)
        except Exception as exc:  # noqa: BLE001 - history must never break a run
            self.event(EventType.STATUS, message=f"session history skipped: {exc}")
        if learn and self.learning.should_review(result):
            try:
                outcome = self.learning.review(result, task)
                result.learned = outcome.to_dict()
                if outcome.changed:
                    self.status(f"learned: {outcome.summary()}")
                for note in outcome.notes[:3]:
                    self.event(EventType.STATUS, message=f"learning: {note}")
            except Exception as exc:  # noqa: BLE001
                self.event(EventType.STATUS, message=f"learning skipped: {exc}")
        self._turns_since_nudge += 1
        return result

    def inject_skill(self, name: str) -> Any:
        """Pin a skill to the next run (`/skill <name>`, `<skill-name> <task>`)."""
        from . import skills as skilllib

        skill = skilllib.get(name, self.ws)
        if skill is None:
            raise KeyError(name)
        self.pending_skill = skill.name
        return skill

    # ------------------------------------------------------------------ #
    def chat_turn(self, text: str, *, role: str | None = None, read_only: bool = False) -> RunResult:
        """One REPL turn - keeps conversation history between calls."""
        result = self.run(text, role=role, read_only=read_only, history=self.history)
        self.history = result.messages
        return result

    def reset(self) -> None:
        self.history = []
        self.sticky_role = None
        self.session_usage = Usage()
        self.gate.session_allowed.clear()

    # ------------------------------------------------------------------ #
    # context management
    # ------------------------------------------------------------------ #
    def _maybe_compact(self, messages: list[Message]) -> None:
        budget = self.config.settings.context_char_budget
        total = sum(m.char_len() for m in messages)
        if total <= budget:
            return
        keep_tail = min(6, max(1, len(messages) // 3))
        compacted = 0
        for index, message in enumerate(messages[: len(messages) - keep_tail]):
            if isinstance(message.content, str) or message.role == "assistant":
                continue
            blocks: list[Any] = []
            for block in message.content:
                if isinstance(block, ToolResultBlock) and len(block.content) > 400:
                    blocks.append(
                        ToolResultBlock(
                            block.tool_use_id,
                            block.content[:200] + f"\n… [compacted {len(block.content):,} chars]",
                            block.is_error,
                        )
                    )
                    compacted += 1
                else:
                    blocks.append(block)
            messages[index] = Message(role=message.role, content=blocks)
        new_total = sum(m.char_len() for m in messages)
        self.status(
            f"context compacted: {compacted} tool result(s) trimmed "
            f"({total:,} -> {new_total:,} chars, budget {budget:,})"
        )

    # ------------------------------------------------------------------ #
    # pipelines (Phase 6)
    # ------------------------------------------------------------------ #
    def run_pipeline(self, name: str, task: str, **kwargs: Any) -> list[RunResult]:
        pipeline = self.config.pipeline(name)
        if pipeline is None:
            known = ", ".join(sorted(self.config.pipelines)) or "none"
            raise KeyError(f"unknown pipeline '{name}' (defined: {known})")
        return self.run_steps(pipeline.steps, task, pipeline=pipeline, **kwargs)

    def run_steps(
        self,
        steps: Sequence[dict[str, Any]],
        task: str,
        *,
        pipeline: Pipeline | None = None,
        max_turns: int | None = None,
        read_only: bool = False,
    ) -> list[RunResult]:
        results: list[RunResult] = []
        handoff = ""
        label = pipeline.name if pipeline else "pipeline"
        for index, step in enumerate(steps, 1):
            role_name = str(step.get("role") or "general")
            instruction = str(step.get("instruction") or step.get("task") or "")
            step_read_only = read_only or bool(step.get("read_only", False))
            prompt = self._compose_step_prompt(task, instruction, handoff, index, len(steps), label)
            self.status(f"▶ [{label}] step {index}/{len(steps)}: {role_name}")
            result = self.run(
                prompt,
                role=role_name,
                read_only=step_read_only,
                max_turns=int(step.get("max_turns") or max_turns or 0) or None,
            )
            results.append(result)
            handoff = self._handoff_note(result)
            if not result.ok:
                self.status(f"pipeline stopped: step {index} ({role_name}) failed")
                break
            # reset stickiness so the next step routes to its own role
            self.sticky_role = None
            self.history = []
        return results

    @staticmethod
    def _compose_step_prompt(
        task: str, instruction: str, handoff: str, index: int, total: int, label: str
    ) -> str:
        parts = [f"Overall goal: {task}", f"You are step {index}/{total} of the '{label}' pipeline."]
        if instruction:
            parts.append(f"Your job in this step:\n{instruction}")
        if handoff:
            parts.append(f"Handoff notes from the previous specialist:\n{handoff}")
        parts.append(
            "When done, call `finish` with a summary that the NEXT specialist can act on "
            "(what you changed, what you verified, what they must check)."
        )
        return "\n\n".join(parts)

    @staticmethod
    def _handoff_note(result: RunResult) -> str:
        bits = [f"[{result.role}] {result.summary or result.text}".strip()]
        if result.changed_files:
            bits.append("files touched: " + ", ".join(result.changed_files[:12]))
        if result.followups:
            bits.append("follow-ups: " + "; ".join(result.followups[:6]))
        return "\n".join(b for b in bits if b)[:4000]


# --------------------------------------------------------------------------- #
# convenience constructor
# --------------------------------------------------------------------------- #


def make_agent(**kwargs: Any) -> NexusAgent:
    return NexusAgent(**kwargs)
