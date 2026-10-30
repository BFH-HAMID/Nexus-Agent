"""Router: turn a task string into a role.

Two stages, cheapest first:

1. **Keyword fast-match** - no LLM call, no latency. Each role has weighted
   keywords; the highest scoring role above ``router_min_score`` wins.
2. **LLM classifier fallback** - only when no keyword hits. Uses the exact
   prompt from the roadmap (chunked when the team is big), sanitizes the reply
   to ``a-z0-9-`` and validates it against the registry; anything unknown falls
   back to ``general``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .config import NexusConfig, RoleRegistry
from .types import (
    Event,
    EventHandler,
    EventType,
    Message,
    Provider,
    Role,
    chunks,
    sanitize_role_name,
)

CLASSIFIER_PROMPT = """You are a routing classifier.
Pick exactly ONE role name from this list that best fits the task.
Reply with ONLY the role name, nothing else.

Roles: {roles}

Task: {task}"""

CLASSIFIER_SYSTEM = (
    "You are a routing classifier for a multi-agent team. You output a single role slug "
    "(lowercase letters, digits and dashes). No explanations, no punctuation, no code fences."
)


@dataclass
class RouteDecision:
    role: Role
    method: str                      # "keyword" | "llm" | "manual" | "default" | "sticky"
    score: float = 0.0
    hits: list[str] = field(default_factory=list)
    candidates: list[tuple[str, float]] = field(default_factory=list)
    note: str = ""

    def describe(self) -> str:
        bits = [f"⇄ role: {self.role.name}", f"agent: {self.role.agent or '?'}", f"via {self.method}"]
        if self.hits:
            bits.append("hits: " + ", ".join(self.hits[:6]))
        if self.score:
            bits.append(f"score {self.score:g}")
        return "  ".join(f"[{b}]" if i == 0 else b for i, b in enumerate(bits))


@dataclass
class Router:
    config: NexusConfig
    providers: object | None = None          # ProviderRegistry
    emit: EventHandler = field(default=lambda _e: None)
    last: RouteDecision | None = None

    # -- registry shortcuts ------------------------------------------------ #
    @property
    def registry(self) -> RoleRegistry:
        return self.config.roles

    @property
    def settings(self):
        return self.config.settings

    # ------------------------------------------------------------------ #
    def keyword_route(self, task: str) -> RouteDecision | None:
        """Stage 1. Returns None when nothing crosses ``router_min_score``."""
        scored: list[tuple[Role, float, list[str]]] = []
        for role in self.registry.roles.values():
            score, hits = role.keyword_score(task)
            if score > 0:
                scored.append((role, score, hits))
        priorities: dict[str, int] = {}
        try:
            # Router Studio rules live in workspace runtime state. A missing or
            # damaged file must never stop a run, so every failure is ignored.
            from .routerstudio import active_rules

            rules = active_rules(self.settings.workspace)
        except Exception:  # noqa: BLE001 - routing outranks the editor
            rules = ()
        if rules:
            scored = self._apply_rules(task, scored, rules, priorities)
        if not scored:
            return None
        scored.sort(key=lambda item: (-item[1], -priorities.get(item[0].name, 0), item[0].name))
        best_role, best_score, best_hits = scored[0]
        candidates = [(r.name, s) for r, s, _ in scored[:6]]

        if best_score < float(self.settings.router_min_score):
            return None

        # Ambiguity guard: two+ roles tied at the top -> let the LLM decide.
        tied = [r for r, s, _ in scored if s == best_score]
        note = ""
        if len(tied) > 1 and self.settings.router_llm_fallback and self.providers is not None:
            tied_names = sorted(t.name for t in tied)
            shown = ", ".join(tied_names[:6]) + ("…" if len(tied_names) > 6 else "")
            note = f"tie between {len(tied_names)} roles ({shown}) - deferring to LLM classifier"
            # A small tie is classified among exactly those roles; a large tie
            # (common with a big imported roster) lets the weak-relevance
            # narrower pick the best 40 instead of an arbitrary alphabetical slice.
            prefer = tied_names if len(tied_names) <= 8 else None
            llm = self.llm_route(task, prefer=prefer)
            if llm is not None:
                llm.note = note
                llm.candidates = candidates
                return llm

        return RouteDecision(
            role=best_role,
            method="keyword",
            score=best_score,
            hits=best_hits,
            candidates=candidates,
            note=note,
        )

    def _apply_rules(self, task: str, scored: list[tuple[Role, float, list[str]]], rules,
                     priorities: dict[str, int]) -> list[tuple[Role, float, list[str]]]:
        """Add saved Router Studio weights to their target roles.

        Rules are additive keywords with a priority used only to break an exact
        score tie. A rule that names a role which no longer exists is skipped.
        """
        positions = {role.name: index for index, (role, _score, _hits) in enumerate(scored)}
        for rule in rules:
            if not rule.enabled or rule.role not in self.registry.roles or not rule.matches(task):
                continue
            position = positions.get(rule.role)
            if position is None:
                positions[rule.role] = len(scored)
                scored.append((self.registry.roles[rule.role], 0.0, []))
                position = positions[rule.role]
            role, score, hits = scored[position]
            scored[position] = (role, score + rule.weight, hits + [f"rule:{rule.id}"])
            priorities[rule.role] = max(priorities.get(rule.role, 0), rule.priority)
        return scored

    def _router_agent_name(self) -> str:
        """Prefer the configured cheap classifier agent, but never point at an
        agent that does not exist in this provider registry."""
        candidates = [self.settings.router_agent, self.config.default_agent, *sorted(self.config.agents)]
        available = set(getattr(self.providers, "agents", {})) or set(self.config.agents)
        for name in candidates:
            if name and name in available:
                return name
        return self.config.default_agent

    # ------------------------------------------------------------------ #
    def _weak_score(self, role: Role, task: str) -> float:
        """Cheap relevance used only to *narrow* the LLM classifier's options.

        Unlike ``keyword_score`` this rewards partial/substring overlap so a big
        imported roster still surfaces a sane top-N without an LLM call.
        """
        text = task.lower()
        score = 0.0
        for kw in role.keywords:
            term = kw.text.lower()
            if not term:
                continue
            if term in text:
                score += 2.0 + min(kw.weight, 3)
            else:
                # token-level partial overlap ("kubernetes" vs "k8s cluster")
                for piece in re.split(r"[\s\-]+", term):
                    if len(piece) >= 4 and piece in text:
                        score += 0.5
                        break
        for piece in re.split(r"[\s\-_]+", role.name.lower()):
            if len(piece) >= 4 and piece in text:
                score += 1.0
        for piece in re.split(r"[\s\-_]+", (role.title or "").lower()):
            if len(piece) >= 4 and piece in text:
                score += 0.5
        if role.category and role.category.lower() in text:
            score += 0.5
        return score

    def _narrow_candidates(self, task: str, limit: int = 40) -> list[str]:
        """Top-N role names for the classifier, so one prompt covers any roster.

        Falls back to one representative per category when nothing overlaps, so
        the LLM still chooses from a meaningful, bounded set instead of every
        role in chunks.
        """
        scored = [(self._weak_score(r, task), r) for r in self.registry.roles.values()]
        scored = [(s, r) for s, r in scored if s > 0]
        scored.sort(key=lambda item: (-item[0], item[1].name))
        names = [r.name for _, r in scored[:limit]]
        if len(names) >= max(5, limit // 4):
            return names
        # pad with one role per category (deterministic) for zero-overlap tasks
        seen_cat: set[str] = set()
        for role in sorted(self.registry.roles.values(), key=lambda r: (r.category, r.name)):
            if role.name in names:
                continue
            if role.category in seen_cat:
                continue
            seen_cat.add(role.category)
            names.append(role.name)
            if len(names) >= limit:
                break
        return names[:limit]

    # ------------------------------------------------------------------ #
    def llm_route(self, task: str, prefer: Sequence[str] | None = None) -> RouteDecision | None:
        """Stage 2 - classifier call against a cheap model."""
        if self.providers is None or not self.settings.router_llm_fallback:
            return None

        if prefer:
            names = list(prefer)[:40]
        else:
            names = self._narrow_candidates(task, 40)
        if not names:
            return None

        agent_name = self._router_agent_name()
        agent_cfg = self.config.agent(agent_name)

        provider: Provider | None = None
        answer = ""
        # Narrowed to <=40 names, so this is a single call even for a huge roster.
        for group in chunks(names, 40):
            prompt = CLASSIFIER_PROMPT.format(roles=", ".join(group), task=task.strip()[:4000])
            try:
                provider, completion = self.providers.call_with_fallback(  # type: ignore[union-attr]
                    agent_cfg.name,
                    CLASSIFIER_SYSTEM,
                    [Message(role="user", content=prompt)],
                    None,
                    max_tokens=32,
                    temperature=0.0,
                    model=agent_cfg.extra.get("router_model") if isinstance(agent_cfg.extra, dict) else None,
                    on_event=self.emit,
                )
            except Exception as exc:  # noqa: BLE001
                self.emit(Event(EventType.ERROR, {"message": f"router LLM fallback failed: {exc}"}))
                return None
            candidate = _extract_role(completion.text or "")
            if candidate and self.registry._resolve(candidate) is not None:
                answer = candidate
                break
            answer = answer or candidate or ""

        clean = sanitize_role_name(answer)
        role = self.registry._resolve(clean)
        if role is None:
            if prefer:
                self.emit(Event(EventType.STATUS, {"message":
                    f"router: '{answer or 'empty'}' is not a known role - retaining the best keyword match"}))
                return None
            self.emit(
                Event(
                    EventType.STATUS,
                    {"message": f"router: '{answer or 'empty'}' is not a known role - falling back to general"},
                )
            )
            role = self.registry.get("general", default=self.config.default_role_name())
            return RouteDecision(role=role, method="default", note=f"classifier said '{answer or 'nothing'}'")

        return RouteDecision(
            role=role,
            method="llm",
            note=(provider.label() if provider else ""),
        )

    # ------------------------------------------------------------------ #
    def route(self, task: str, *, forced: str | None = None) -> RouteDecision:
        """Full routing entry point."""
        if forced:
            role = self.registry.get(forced, default="general")
            decision = RouteDecision(role=role, method="manual", note=f"forced with /role {forced}")
            self.last = decision
            self.emit(Event(EventType.ROLE, {"role": role.name, "agent": role.agent, "method": "manual"}))
            return decision

        if not task.strip():
            role = self.registry.get("general")
            return RouteDecision(role=role, method="default", note="empty task")

        decision = self.keyword_route(task)
        if decision is None:
            decision = self.llm_route(task)
        if decision is None:
            role = self.registry.get("general")
            decision = RouteDecision(role=role, method="default", note="no keyword hit, no LLM router")

        self.last = decision
        self.emit(
            Event(
                EventType.ROLE,
                {
                    "role": decision.role.name,
                    "agent": decision.role.agent,
                    "method": decision.method,
                    "score": decision.score,
                    "hits": decision.hits,
                    "note": decision.note,
                },
            )
        )
        return decision


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _extract_role(text: str) -> str:
    """Models love to add prose around the slug - dig it out."""
    text = (text or "").strip()
    if not text:
        return ""
    fenced = re.search(r"```[a-z]*\s*([a-z0-9-]+)\s*```", text, re.IGNORECASE)
    if fenced:
        return sanitize_role_name(fenced.group(1))
    first = text.splitlines()[0].strip()
    quoted = re.search(r"[`\"']([a-z0-9-]+)[`\"']", first, re.IGNORECASE)
    if quoted:
        return sanitize_role_name(quoted.group(1))
    tokens = re.findall(r"[a-z0-9][a-z0-9-]*", first.lower())
    return tokens[0] if tokens else ""


def explain(decision: RouteDecision) -> str:
    """Human readable one-liner for the CLI."""
    if decision.method == "keyword":
        return (
            f"⇄ role: {decision.role.name} -> agent: {decision.role.agent} "
            f"(keyword match: {', '.join(decision.hits[:5]) or '-'}, score {decision.score:g})"
        )
    if decision.method == "llm":
        return f"⇄ role: {decision.role.name} -> agent: {decision.role.agent} (LLM classifier{': ' + decision.note if decision.note else ''})"
    if decision.method == "manual":
        return f"⇄ role: {decision.role.name} -> agent: {decision.role.agent} (manual)"
    return f"⇄ role: {decision.role.name} -> agent: {decision.role.agent} (default{': ' + decision.note if decision.note else ''})"
