"""The learning loop: turn finished work into skills and memory.

This is the Hermes idea (`hermes-agent <https://github.com/NousResearch/hermes-agent>`_)
implemented natively for Nexus:

* after a non-trivial run, a cheap model reviews what actually happened and may
  **create a skill** (procedural memory), **patch a skill that was wrong**, or
  **persist memory / user-profile facts**;
* nothing is invented: the review only sees the task, the tool calls, the
  changed files and the final answer;
* skills follow the agentskills.io / Hermes authoring standards (``SKILL.md``,
  <=60 char description, When to Use → Procedure → Verification), so they are
  portable both ways;
* ``/learn <what you just did>`` runs the same loop on demand, and a periodic
  *nudge* reminds the agent to save what it learned.

Everything is capped (1 skill + 1 patch + 3 memory entries per review) so a
run cannot flood the library.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import skills as skilllib
from .types import EventType, Message

LEARN_SYSTEM = """You are the learning loop of a coding agent. You read a FINISHED run and decide what \
is worth keeping. Return ONLY a JSON object, no prose, no code fences.

Schema:
{
  "skill":  {"name": "lowercase-hyphen-name", "description": "<=60 char sentence.", \
"body": "full SKILL.md body", "tags": ["Tag"]} | null,
  "patch":  {"name": "existing-skill-name", "description": "<=60 chars" | null, "body": "full new body" | null} | null,
  "memory": ["durable fact, decision or gotcha worth remembering next session"],
  "profile": ["stable preference of the human, not task detail"]
}

Rules:
- A skill is a REUSABLE procedure (how to do X in this repo/environment), not a summary of the task.
  Create one only if the run involved 3+ tool calls and the steps will repeat.
- Body starts with "# <Title> Skill" and uses: ## When to Use, ## Procedure, ## Verification,
  optionally ## Pitfalls. Keep it under 120 lines, exact commands and paths only.
- description: ONE sentence, <= 60 characters, ends with a period, no marketing words.
- patch: only when an existing skill was incomplete or wrong. Never invent a name.
- memory: max 3 entries, each one line, no secrets, no code dumps.
- profile: only stable preferences ("prefers pytest over unittest"), never task facts.
- It is correct to return all nulls/empty lists when nothing generalizes."""


@dataclass
class LearningOutcome:
    skill: str = ""
    skill_path: str = ""
    patched: str = ""
    memory: list[str] = field(default_factory=list)
    profile: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.skill or self.patched or self.memory or self.profile)

    def summary(self) -> str:
        bits: list[str] = []
        if self.skill:
            bits.append(f"new skill {self.skill}")
        if self.patched:
            bits.append(f"updated skill {self.patched}")
        if self.memory:
            bits.append(f"{len(self.memory)} memory entr{'y' if len(self.memory) == 1 else 'ies'}")
        if self.profile:
            bits.append(f"{len(self.profile)} profile entr{'y' if len(self.profile) == 1 else 'ies'}")
        return ", ".join(bits) or "nothing to save"

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill, "skill_path": self.skill_path, "patched": self.patched,
            "memory": self.memory, "profile": self.profile, "notes": self.notes,
            "changed": self.changed,
        }


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def extract_json(text: str) -> dict[str, Any] | None:
    """Find the first JSON object in a model reply (tolerates fences/prose)."""
    if not text:
        return None
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates.append(text)
    for blob in candidates:
        start = blob.find("{")
        while start != -1:
            depth = 0
            for index in range(start, len(blob)):
                char = blob[index]
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(blob[start : index + 1])
                        except json.JSONDecodeError:
                            break
                        if isinstance(data, dict):
                            return data
                        break
            start = blob.find("{", start + 1)
    return None


def _as_list(value: Any, limit: int = 3) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        text = " ".join(str(item).split()).strip()
        if text:
            out.append(text[:400])
    return out


def _clean_skill_name(value: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", str(value or "").strip().lower()).strip("-")
    return name[:64]


# --------------------------------------------------------------------------- #
# context for the reviewer
# --------------------------------------------------------------------------- #


def run_digest(result: Any, task: str, *, max_chars: int = 9000) -> str:
    lines = [f"TASK: {task.strip()[:1500]}", f"ROLE: {getattr(result, 'role', '')}",
             f"TOOLS CALLED: {getattr(result, 'tool_calls', 0)}",
             f"TURNS: {getattr(result, 'turns', 0)}"]
    changed = getattr(result, "changed_files", []) or []
    if changed:
        lines.append("FILES CHANGED: " + ", ".join(changed[:30]))
    if getattr(result, "summary", ""):
        lines.append(f"SUMMARY: {result.summary[:1200]}")
    tool_notes: list[str] = []
    for message in getattr(result, "messages", []) or []:
        if isinstance(message.content, str):
            continue
        for block in message.content:
            from .types import ToolCall, ToolResultBlock

            if isinstance(block, ToolCall):
                args = json.dumps(block.input, ensure_ascii=False)[:300]
                tool_notes.append(f"- {block.name}({args})")
            elif isinstance(block, ToolResultBlock) and block.is_error:
                tool_notes.append(f"  error: {' '.join(block.content.split())[:200]}")
    if tool_notes:
        lines.append("TOOL TRAIL:\n" + "\n".join(tool_notes[-40:]))
    if getattr(result, "text", ""):
        lines.append(f"FINAL ANSWER: {result.text[:2000]}")
    digest = "\n".join(lines)
    return digest[:max_chars]


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #


class LearningLoop:
    def __init__(self, agent: Any):
        self.agent = agent

    # -- policy ---------------------------------------------------------- #
    @property
    def settings(self) -> Any:
        return self.agent.config.settings

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "learning", True)) and getattr(self.agent, "skills_enabled", True)

    def should_review(self, result: Any) -> bool:
        if not self.enabled or not getattr(result, "ok", False):
            return False
        minimum = int(getattr(self.settings, "learning_min_tools", 4) or 0)
        return int(getattr(result, "tool_calls", 0) or 0) >= minimum

    # -- review ---------------------------------------------------------- #
    def review(self, result: Any, task: str, *, apply: bool = True,
               agent_name: str | None = None) -> LearningOutcome:
        outcome = LearningOutcome()
        if not self.enabled:
            outcome.notes.append("learning is off (settings.learning / NEXUS_LEARNING=0)")
            return outcome

        provider_name = agent_name or getattr(self.settings, "learning_agent", "") \
            or getattr(self.settings, "router_agent", "") or self.agent.config.default_agent
        agent_cfg = self.agent.config.agents.get(provider_name) or self.agent.config.agent_for_role(
            self.agent.active_role
        )
        system = LEARN_SYSTEM
        library = skilllib.discover(self.agent.ws)
        if library:
            system += "\n\nExisting skills (patch these instead of duplicating):\n" + "\n".join(
                f"- {s.name}: {s.prompt_description}" for s in library[:80]
            )
        messages = [Message(role="user", content=run_digest(result, task))]
        self.agent.status(f"learning from this run ({agent_cfg.name})…")
        try:
            _provider, completion = self.agent.providers.call_with_fallback(
                agent_cfg.name, system, messages, None,
                max_tokens=1800, temperature=0.2, on_event=self.agent.emit,
            )
        except Exception as exc:  # noqa: BLE001 - learning must never break a run
            outcome.notes.append(f"learning call failed: {exc}")
            return outcome

        outcome.raw = completion.text or ""
        data = extract_json(outcome.raw)
        if not data:
            outcome.notes.append("review returned no JSON")
            return outcome
        if apply:
            self.apply(data, outcome)
        else:
            outcome.notes.append("dry run - nothing written")
        return outcome

    # -- apply ----------------------------------------------------------- #
    def apply(self, data: dict[str, Any], outcome: LearningOutcome) -> LearningOutcome:
        memory = getattr(self.agent, "memory_store", None)

        patch = data.get("patch")
        if isinstance(patch, dict) and patch.get("name"):
            name = _clean_skill_name(patch["name"])
            existing = skilllib.get(name, self.agent.ws)
            if existing is None:
                outcome.notes.append(f"patch skipped: no skill named '{name}'")
            elif existing.source == "hermes":
                outcome.notes.append(f"patch skipped: '{name}' is imported (read-only)")
            else:
                body = patch.get("body") if isinstance(patch.get("body"), str) and patch["body"].strip() else None
                description = patch.get("description") if isinstance(patch.get("description"), str) else None
                skilllib.patch_skill(existing.path, body=body, description=description)
                outcome.patched = name
                self.agent.event(EventType.SKILL, action="patch", name=name, path=str(existing.path))

        skill = data.get("skill")
        if isinstance(skill, dict) and skill.get("name"):
            name = _clean_skill_name(skill["name"])
            description = str(skill.get("description") or "").strip()
            body = str(skill.get("body") or "").strip()
            tags = skill.get("tags") if isinstance(skill.get("tags"), list) else []
            existing = skilllib.get(name, self.agent.ws)
            warnings = skilllib.lint(name, description, body, tags=tags)
            if existing is not None:
                if existing.source != "hermes":
                    skilllib.patch_skill(existing.path, body=body or None,
                                         description=description or None)
                    outcome.patched = outcome.patched or name
                    self.agent.event(EventType.SKILL, action="patch", name=name, path=str(existing.path))
                else:
                    outcome.notes.append(f"skill '{name}' exists (imported) - not overwritten")
            elif not description or not body:
                outcome.notes.append("skill skipped: description/body missing")
            else:
                try:
                    path = skilllib.create_skill(
                        name=name, description=description, body=body, tags=tags,
                        category="learned", workspace=self.agent.ws,
                        provenance={"created_by": "learning-loop", "task": data.get("_task", "")[:200]},
                    )
                    outcome.skill = name
                    outcome.skill_path = str(path)
                    outcome.notes.extend(warnings)
                    self.agent.event(EventType.SKILL, action="create", name=name, path=str(path))
                except (ValueError, OSError, FileExistsError) as exc:
                    outcome.notes.append(f"skill write failed: {exc}")

        if memory is not None:
            for text in _as_list(data.get("memory"), 3):
                ok, note = memory.add(text, "memory")
                if ok:
                    outcome.memory.append(text)
                    self.agent.event(EventType.MEMORY, role="memory", path=str(memory.memory.path),
                                     text=text)
                else:
                    outcome.notes.append(f"memory: {note}")
            for text in _as_list(data.get("profile"), 3):
                ok, note = memory.add(text, "user")
                if ok:
                    outcome.profile.append(text)
                else:
                    outcome.notes.append(f"profile: {note}")
        return outcome

    # -- on demand ------------------------------------------------------- #
    def learn_from_brief(self, brief: str, *, name: str = "", apply: bool = True,
                         history: Any = None) -> LearningOutcome:
        """``/learn <description>``: author a skill from what the user described."""
        outcome = LearningOutcome()
        agent_cfg = self.agent.config.agents.get(
            getattr(self.settings, "learning_agent", "") or getattr(self.settings, "router_agent", "")
        ) or self.agent.config.agent_for_role(self.agent.active_role)
        system = LEARN_SYSTEM
        context = ""
        if history:
            from .sessions import transcript_text

            context = "\n\nWHAT WE JUST DID (transcript tail):\n" + transcript_text(history, 6000)
        messages = [Message(role="user", content=(
            "The user wants this reusable procedure captured as a skill. Sources may be below; "
            "write the skill now, plus any memory worth keeping.\n\n" + brief[:8000] + context
        ))]
        try:
            _provider, completion = self.agent.providers.call_with_fallback(
                agent_cfg.name, system, messages, None, max_tokens=1800, temperature=0.2,
                on_event=self.agent.emit,
            )
        except Exception as exc:  # noqa: BLE001
            outcome.notes.append(f"learning call failed: {exc}")
            return outcome
        data = extract_json(completion.text or "") or {}
        if name and isinstance(data.get("skill"), dict):
            data["skill"]["name"] = name
        if apply:
            self.apply(data, outcome)
        return outcome

    # -- nudges ---------------------------------------------------------- #
    def nudge(self) -> str:
        return (
            "SYSTEM: if this session produced a reusable procedure, save it now with the "
            "`skill_manage` tool (action=create); if a fact or preference should survive this "
            "session, use the `memory` tool. Skip it if nothing generalizes."
        )


def nudge_interval(settings: Any) -> int:
    return int(getattr(settings, "skill_nudge_interval", 8) or 0)


def skills_index_for(agent: Any, task: str) -> tuple[str, list[Any]]:
    """(prompt section, selected skills) for the current task."""
    library = skilllib.discover(agent.ws)
    if not library:
        return "", []
    selected = skilllib.select(library, task, limit=8)
    return skilllib.render_prompt_section(selected, library), selected
