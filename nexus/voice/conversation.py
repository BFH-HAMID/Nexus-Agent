"""Conversation state: short-term turns, active mission context, reference resolution, one-question rule, and acks."""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .persona import enforce_one_question

_FILE_RE = re.compile(
    r"\b(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|js|ts|tsx|jsx|yaml|yml|json|md|txt|sh|toml|cfg|csv|log|rs|go)\b"
)
_PR_ISSUE_RE = re.compile(r"\b(?:PR|pull request|issue|branch)\s*(?:#\d+|['\"]?[A-Za-z0-9_./-]+['\"]?)", re.I)
_BACKTICK_CMD_RE = re.compile(r"`([^`]{2,120})`")
_COMMAND_HINT_RE = re.compile(
    r"\b(?:pytest|ruff|git\s+\w+|npm\s+\w+|python(?:\d)?\s+\S+|nexus\s+\w+|docker\s+\w+|make\s+\w+)\b[^\n!?;]*?(?=[.!?;](?:\s|$)|$|\n)",
    re.I,
)
_PERSON_RE = re.compile(
    r"(?:(?:to|for|from|with|by|ask|tell|email|message|send(?:\s+it)?\s+to|ping)\s+([A-Z][a-z]{1,24})(?![\w./-])|@([a-zA-Z0-9_-]{2,24}))"
)
_CAP_NAME_RE = re.compile(r"\b([A-Z][a-z]{2,20})\b")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+)$", re.MULTILINE)
_PAIR_AND_RE = re.compile(r"\b([A-Z][a-z]{2,20}|[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,6})\s+(?:and|or|এবং)\s+([A-Z][a-z]{2,20}|[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,6})\b")

_REPLAY_RE = re.compile(
    r"\b(?:run\s+(?:it|that)\s+again|do\s+(?:it|that)\s+again|repeat\s+(?:that|it)|retry(?:\s+(?:it|that))?|rerun(?:\s+(?:it|that))?)\b"
    r"|(?:আবার\s*(?:চালাও|করো|দাও)|ওটা\s*আবার)",
    re.I,
)
_SEND_RECIPIENT_RE = re.compile(
    r"\b(?:send|email|message|forward|reply|give|tell|assign)\s+(?:.{1,40}?\s+)?to\s+(him|her|them)\b"
    r"|\b(?:email|message|ping|tell|ask)\s+(him|her|them)\b"
    r"|(?:ওকে|তাকে|তাদের)\s*(?:পাঠাও|দাও|বলো)",
    re.I,
)
_NAMED_RECIPIENT_RE = re.compile(
    r"\b(?:send|email|message|forward|reply)\s+(?:.{1,40}?\s+)?to\s+([A-Z][a-zA-Z.-]{1,32})(?!\S*@)\b"
    r"|\b(?:email|message|ping)\s+([A-Z][a-zA-Z.-]{1,32})(?!\S*@)\b",
    re.I,
)
_ORDINAL_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\b(?:the\s+)?first\s+one\b|প্রথমটা", re.I), 0),
    (re.compile(r"\b(?:the\s+)?second\s+one\b|দ্বিতীয়টা", re.I), 1),
    (re.compile(r"\b(?:the\s+)?third\s+one\b|তৃতীয়টা", re.I), 2),
    (re.compile(r"\b(?:the\s+)?last\s+one\b|শেষেরটা", re.I), -1),
)
_DEMO_ITEM_RE = re.compile(
    r"\b(?:that\s+one|this\s+one|that\s+file|this\s+file|that\s+pr|that\s+issue|that\s+command|"
    r"(?:delete|open|fix|check|inspect|read|run|send|show|explain)\s+(?:it|that))\b"
    r"|(?:ওটা|এইটা|এটা)",
    re.I,
)

_NON_PERSON_CAPS = frozenset({
    "The", "This", "That", "There", "Here", "What", "When", "Where", "Which", "Who",
    "Yes", "No", "Wait", "Done", "Okay", "Ok", "Nexus", "Bangla", "English", "Python",
    "Git", "GitHub", "Telegram", "HUD", "PR", "API", "CLI", "File", "Test", "Tests",
    "Found", "Running", "Check", "Checking", "Error", "Please", "Sorry", "Got",
})


@dataclass(frozen=True)
class EntityMention:
    kind: str  # file | command | person | target | item
    value: str
    turn_id: int
    source: str = "assistant"  # user | assistant | mission


@dataclass
class ConversationTurn:
    turn_id: int
    user_text: str
    assistant_text: str = ""
    language: str = "en"
    timestamp: float = field(default_factory=time.time)
    role: str = ""
    command_executed: str = ""
    entities: list[EntityMention] = field(default_factory=list)


@dataclass
class PendingClarification:
    original_utterance: str
    ambiguity_kind: str  # person | file | target | command | item
    candidates: list[str]
    question: str
    language: str = "en"
    created_at: float = field(default_factory=time.time)
    target_token: str = ""


@dataclass(frozen=True)
class ReferenceResolution:
    resolved_text: str
    needs_clarification: bool = False
    clarification_question: str = ""
    resolved_references: dict[str, str] = field(default_factory=dict)
    ambiguous_candidates: tuple[str, ...] = ()
    active_mission_note: str = ""


def extract_entities(
    text: str,
    *,
    turn_id: int,
    source: str = "assistant",
    command_executed: str = "",
) -> list[EntityMention]:
    mentions: list[EntityMention] = []
    seen: set[tuple[str, str]] = set()

    def _add(kind: str, value: str) -> None:
        cleaned = value.strip(" .,;:\"'`")
        if not cleaned:
            return
        key = (kind, cleaned.lower())
        if key not in seen:
            seen.add(key)
            mentions.append(EntityMention(kind=kind, value=cleaned, turn_id=turn_id, source=source))

    if command_executed:
        _add("command", command_executed)

    for match in _FILE_RE.finditer(text or ""):
        _add("file", match.group(0))

    for match in _BACKTICK_CMD_RE.finditer(text or ""):
        inner = match.group(1).strip()
        if _COMMAND_HINT_RE.search(inner):
            _add("command", inner)
        elif _FILE_RE.fullmatch(inner):
            _add("file", inner)
        else:
            _add("target", inner)

    non_backtick_text = _BACKTICK_CMD_RE.sub(" ", text or "")
    for match in _COMMAND_HINT_RE.finditer(non_backtick_text):
        _add("command", match.group(0))

    for match in _PR_ISSUE_RE.finditer(text or ""):
        _add("target", match.group(0))

    for match in _LIST_ITEM_RE.finditer(text or ""):
        _add("item", match.group(1).strip())

    # Detect explicit recipient / person names.
    for match in _PERSON_RE.finditer(text or ""):
        candidate = (match.group(1) or match.group(2) or "").strip()
        if candidate and candidate not in _NON_PERSON_CAPS and candidate.lower() not in {"him", "her", "them", "you", "me", "us"}:
            _add("person", candidate)

    for match in _PAIR_AND_RE.finditer(text or ""):
        left, right = match.group(1), match.group(2)
        for item in (left, right):
            if _FILE_RE.fullmatch(item):
                _add("file", item)
            elif item[:1].isupper() and item not in _NON_PERSON_CAPS:
                _add("person", item)

    # Capitalized non-sentence-start words that are not technical nouns often name people when paired with communication context.
    if re.search(r"\b(?:from|by|with|email|message|ping|owner|author|reviewer|wrote|sent|asked)\b", text or "", re.I):
        for match in _CAP_NAME_RE.finditer(text or ""):
            word = match.group(1)
            if word not in _NON_PERSON_CAPS and not _FILE_RE.search(word):
                _add("person", word)

    return mentions


def read_active_mission(workspace: Path | str | None) -> dict[str, Any]:
    """Read active mission / task snapshot from workspace state safely (never raises)."""
    if workspace is None:
        return {}
    ws = Path(workspace).resolve()
    info: dict[str, Any] = {}
    try:
        from ..hud.presence import Presence
        presence = Presence(ws)
        if presence.enabled('handoff_enabled') and presence.enabled('voice_enabled'):
            selected = presence.snapshot()['active_mission']
            if selected:
                from ..chief import load_mission
                mission = load_mission(ws, selected)
                info.update(mission_id=mission.id, task=presence.clean(mission.goal), state=mission.status)
    except (ValueError, OSError, KeyError):
        pass  # Observation only; no task resume or approval transfer.
    try:
        from ..assistant.memory import PersonalMemory

        pm = PersonalMemory(ws)
        if pm.enabled:
            active = pm.active_state()
            if isinstance(active, dict) and active:
                info = {**active, **info}
    except Exception:
        pass
    try:
        from ..studio import Studio
        from ..workbench import WorkbenchState

        studio = Studio(WorkbenchState(ws))
        jobs = studio.items("jarvis-job")
        running = [j for j in jobs if j.get("status") == "running"]
        if running:
            latest = running[-1]
            info.setdefault("mission_task", latest.get("task", ""))
            info.setdefault("role", latest.get("active_role", ""))
            info.setdefault("job_id", latest.get("id", ""))
    except Exception:
        pass
    return info


class ConversationState:
    """Maintains rolling N-turn context, active mission, and single-question clarifications."""

    def __init__(
        self,
        *,
        max_turns: int = 10,
        workspace: Path | str | None = None,
        mission_provider: Callable[[], dict[str, Any]] | None = None,
        contact_resolver: Callable[[str], dict[str, Any] | None] | None = None,
        window_provider: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self.max_turns = max(1, min(50, int(max_turns)))
        self.workspace = Path(workspace).resolve() if workspace else None
        self.mission_provider = mission_provider
        self.contact_resolver = contact_resolver
        self.window_provider = window_provider
        self.turns: deque[ConversationTurn] = deque(maxlen=self.max_turns)
        self._next_turn_id = 1
        self.pending_clarification: PendingClarification | None = None
        self.last_language: str = "en"
        self._shared_turn_id = ''
        self._shared_fingerprint = ''

    def active_mission(self) -> dict[str, Any]:
        if self.mission_provider is not None:
            try:
                return dict(self.mission_provider() or {})
            except Exception:
                return {}
        return read_active_mission(self.workspace)

    def add_turn(
        self,
        user_text: str,
        assistant_text: str = "",
        *,
        language: str = "en",
        role: str = "",
        command_executed: str = "",
        extra_entities: list[EntityMention] | None = None,
    ) -> ConversationTurn:
        tid = self._next_turn_id
        self._next_turn_id += 1
        self.last_language = language or self.last_language
        entities = extract_entities(user_text, turn_id=tid, source="user")
        extra_mentions = extract_entities(
            assistant_text,
            turn_id=tid,
            source="assistant",
            command_executed=command_executed,
        )
        seen_turn = {(e.kind, e.value.lower()) for e in entities}
        for m in extra_mentions:
            key = (m.kind, m.value.lower())
            if key not in seen_turn:
                seen_turn.add(key)
                entities.append(m)
        if extra_entities:
            for m in extra_entities:
                key = (m.kind, m.value.lower())
                if key not in seen_turn:
                    seen_turn.add(key)
                    entities.append(m)
        turn = ConversationTurn(
            turn_id=tid,
            user_text=user_text,
            assistant_text=assistant_text,
            language=language,
            role=role,
            command_executed=command_executed,
            entities=entities,
        )
        self.turns.append(turn)
        return turn

    def recent_candidates(self, kind: str | None = None) -> list[EntityMention]:
        """Collect recent entity mentions, newest turn first, preserving within-turn order."""
        items: list[EntityMention] = []
        seen: set[str] = set()
        for turn in reversed(self.turns):
            for ent in turn.entities:
                if kind is not None and ent.kind != kind:
                    continue
                key = ent.value.lower()
                if key not in seen:
                    seen.add(key)
                    items.append(ent)
        # Also include active mission entities if not already present.
        mission = self.active_mission()
        mission_blob = " ".join(
            str(mission.get(k) or "") for k in ("task", "mission_task", "target", "recipient", "file", "command")
        ).strip()
        if mission_blob:
            for ent in extract_entities(mission_blob, turn_id=0, source="mission"):
                if kind is not None and ent.kind != kind:
                    continue
                if ent.value.lower() not in seen:
                    seen.add(ent.value.lower())
                    items.append(ent)
        return items

    def _ordered_list_candidates(self) -> list[str]:
        """Return list items or pair items from the most recent turn that introduced multiple items."""
        for turn in reversed(self.turns):
            items = [e.value for e in turn.entities if e.kind == "item"]
            if len(items) >= 2:
                return items
            files = [e.value for e in turn.entities if e.kind == "file"]
            if len(files) >= 2:
                return files
            people = [e.value for e in turn.entities if e.kind == "person"]
            if len(people) >= 2:
                return people
            targets = [e.value for e in turn.entities if e.kind == "target"]
            if len(targets) >= 2:
                return targets
        return []

    def _resolve_clarification_answer(self, utterance: str) -> ReferenceResolution | None:
        pending = self.pending_clarification
        if pending is None:
            return None
        reply = utterance.strip()
        if not reply:
            return None
        low = reply.lower()
        # Allow explicit cancellation of pending question.
        if low in {"cancel", "never mind", "nevermind", "stop", "বাদ দাও", "বাতিল"}:
            self.pending_clarification = None
            return ReferenceResolution(resolved_text="", needs_clarification=False)

        chosen = ""
        # Check ordinal selection in clarification answer.
        for pattern, idx in _ORDINAL_PATTERNS:
            if pattern.search(reply):
                try:
                    chosen = pending.candidates[idx]
                except IndexError:
                    chosen = ""
                break
        if not chosen:
            for cand in pending.candidates:
                if cand.lower() in low or low in cand.lower():
                    chosen = cand
                    break
        if not chosen and len(reply.split()) <= 3:
            chosen = reply.rstrip(".!?।")
        if not chosen:
            return None

        self.pending_clarification = None
        orig = pending.original_utterance
        skip_contact_lookup = False
        if pending.ambiguity_kind == "person":
            if pending.target_token:
                merged = re.sub(
                    r"\b" + re.escape(pending.target_token) + r"\b",
                    chosen,
                    orig,
                    count=1,
                    flags=re.I,
                )
                skip_contact_lookup = True
            else:
                merged = _SEND_RECIPIENT_RE.sub(
                    lambda m: re.sub(r"\b(?:him|her|them)\b|ওকে|তাকে|তাদের", chosen, m.group(0), flags=re.I),
                    orig,
                )
            if merged == orig:
                merged = f"{orig} ({chosen})"
        else:
            phrase_pat = re.compile(
                r"\b(?:that\s+one|this\s+one|that\s+file|this\s+file|that\s+pr|that\s+issue|that\s+command)\b|ওটা|এইটা|এটা",
                re.I,
            )
            if phrase_pat.search(orig):
                merged = phrase_pat.sub(chosen, orig)
            else:
                merged = re.sub(r"\b(?:it|that)\b", chosen, orig, count=1, flags=re.I)
            if merged == orig:
                merged = f"{orig} ({chosen})"

        # Also resolve any secondary 'it' still remaining in merged text!
        res = self._resolve_core(merged, language=pending.language, skip_contact_lookup=skip_contact_lookup)
        refs = {**res.resolved_references, pending.ambiguity_kind: chosen}
        return ReferenceResolution(
            resolved_text=res.resolved_text,
            needs_clarification=res.needs_clarification,
            clarification_question=res.clarification_question,
            resolved_references=refs,
            ambiguous_candidates=res.ambiguous_candidates,
            active_mission_note=res.active_mission_note,
        )

    def _clear_shared(self):
        if self._shared_fingerprint:
            self.turns.clear()
            self.pending_clarification = None
            self._shared_turn_id = self._shared_fingerprint = ''

    def _sync_shared(self):
        if self.workspace is None:
            return
        try:
            import hashlib
            import json

            from ..hud.presence import Presence
            presence = Presence(self.workspace)
            if not (presence.enabled('voice_enabled') and presence.enabled('conversation_enabled')):
                self._clear_shared()
                return
            rows = [row for row in presence.snapshot()['turns'] if row['status'] in {'done', 'error', 'interrupted'}]
            rows = rows[-self.max_turns:]
            # ID alone misses retention of older turns and credential re-redaction.
            fingerprint = hashlib.sha256(json.dumps([(r['id'], r['user'], r['assistant'], r['status'])
                                                      for r in rows], ensure_ascii=False).encode()).hexdigest()
            if fingerprint == self._shared_fingerprint:
                return
            self.turns.clear()
            self.pending_clarification = None
            self._shared_turn_id = rows[-1]['id'] if rows else ''
            self._shared_fingerprint = fingerprint
            for row in rows:
                # Text references only, not executed command/grant/speaker authority.
                self.add_turn(row['user'], row['assistant'])
        except (ValueError, OSError, KeyError, TypeError, TimeoutError):
            self._clear_shared()  # Invalid/off shared state cannot retain imported references.

    def resolve_reference(self, utterance: str, *, language: str = "en") -> ReferenceResolution:
        self._sync_shared()
        text = " ".join((utterance or "").split()).strip()
        if not text:
            return ReferenceResolution(resolved_text="")

        if self.pending_clarification is not None:
            answered = self._resolve_clarification_answer(text)
            if answered is not None:
                return answered

        return self._resolve_core(text, language=language)

    def _resolve_core(self, text: str, *, language: str = "en", skip_contact_lookup: bool = False) -> ReferenceResolution:
        resolved = text
        refs: dict[str, str] = {}
        mission = self.active_mission()
        mission_note = ""
        if mission:
            task_label = str(mission.get("task") or mission.get("mission_task") or "").strip()
            role_label = str(mission.get("role") or "").strip()
            if task_label or role_label:
                mission_note = f"active_mission={task_label} role={role_label}".strip()

        # 0. Explicit named recipient contact resolution ('send it to Rahim')
        if not skip_contact_lookup and self.contact_resolver is not None:
            m_named = _NAMED_RECIPIENT_RE.search(resolved)
            if m_named:
                person_name = (m_named.group(1) or m_named.group(2) or "").strip()
                if person_name and person_name.lower() not in {"him", "her", "them", "me", "you", "us", "everyone"}:
                    try:
                        contact_res = self.contact_resolver(person_name)
                    except Exception:
                        contact_res = None
                    if isinstance(contact_res, dict):
                        status = contact_res.get("status")
                        if status == "resolved" and isinstance(contact_res.get("contact"), dict):
                            c = contact_res["contact"]
                            formatted = f"{c.get('name', person_name)} <{c.get('email', '')}>".strip()
                            refs["person"] = formatted
                            resolved = re.sub(
                                r"\b" + re.escape(person_name) + r"\b",
                                formatted,
                                resolved,
                                count=1,
                            )
                        elif status in {"ambiguous", "not_found"} and contact_res.get("clarification_question"):
                            matches = contact_res.get("matches") or []
                            choices = [
                                f"{m.get('name', '')} <{m.get('email', '')}>".strip()
                                for m in matches
                                if isinstance(m, dict) and m.get("email")
                            ]
                            q = enforce_one_question(str(contact_res["clarification_question"]))
                            self.pending_clarification = PendingClarification(
                                text,
                                "person",
                                choices,
                                q,
                                language=language,
                                target_token=person_name,
                            )
                            return ReferenceResolution(
                                resolved_text=text,
                                needs_clarification=True,
                                clarification_question=q,
                                ambiguous_candidates=tuple(choices),
                                active_mission_note=mission_note,
                            )

        # 1. Replay ("run it again", "do it again", etc.)
        if _REPLAY_RE.search(resolved):
            cmds = self.recent_candidates("command")
            if not cmds:
                # Fall back to last actionable user turn if it wasn't itself a replay.
                prev_tasks = [t.user_text for t in reversed(self.turns) if t.user_text and not _REPLAY_RE.search(t.user_text)]
                if prev_tasks:
                    chosen_cmd = prev_tasks[0]
                    refs["command"] = chosen_cmd
                    resolved = f"Run again: {chosen_cmd}"
                    return ReferenceResolution(resolved_text=resolved, resolved_references=refs, active_mission_note=mission_note)
                q = _ask_one(
                    "Which command would you like me to run again?",
                    "কোন কমান্ডটা আবার চালাব?",
                    language,
                )
                self.pending_clarification = PendingClarification(text, "command", [], q, language=language)
                return ReferenceResolution(resolved_text=text, needs_clarification=True, clarification_question=q, active_mission_note=mission_note)
            # Check if multiple distinct competing commands appeared in the very last turn.
            raw_turn_cmds = [e.value for e in self.turns[-1].entities if e.kind == "command"] if self.turns else []
            last_turn_cmds: list[str] = []
            for cmd in raw_turn_cmds:
                if cmd.lower() not in {c.lower() for c in last_turn_cmds}:
                    last_turn_cmds.append(cmd)
            if len(last_turn_cmds) >= 2:
                q = _ask_choice(last_turn_cmds[:2], language, kind="command")
                self.pending_clarification = PendingClarification(text, "command", last_turn_cmds[:2], q, language=language)
                return ReferenceResolution(
                    resolved_text=text,
                    needs_clarification=True,
                    clarification_question=q,
                    ambiguous_candidates=tuple(last_turn_cmds[:2]),
                    active_mission_note=mission_note,
                )
            refs["command"] = cmds[0].value
            resolved = f"Run again: {cmds[0].value}"
            return ReferenceResolution(resolved_text=resolved, resolved_references=refs, active_mission_note=mission_note)

        # 2. Recipient pronouns ("send it to him", "email her", etc.)
        if _SEND_RECIPIENT_RE.search(resolved):
            # Look at the most recent turn with person mentions first.
            latest_people: list[str] = []
            for turn in reversed(self.turns):
                people = [e.value for e in turn.entities if e.kind == "person"]
                if people:
                    latest_people = people
                    break
            if not latest_people:
                latest_people = [e.value for e in self.recent_candidates("person")]
            if len(latest_people) == 1:
                recipient = latest_people[0]
                refs["person"] = recipient
                resolved = re.sub(r"\b(?:him|her|them)\b|ওকে|তাকে|তাদের", recipient, resolved, flags=re.I)
            elif len(latest_people) >= 2:
                choices = latest_people[:2]
                q = _ask_choice(choices, language, kind="person")
                self.pending_clarification = PendingClarification(text, "person", choices, q, language=language)
                return ReferenceResolution(
                    resolved_text=text,
                    needs_clarification=True,
                    clarification_question=q,
                    ambiguous_candidates=tuple(choices),
                    active_mission_note=mission_note,
                )
            else:
                q = _ask_one(
                    "Who should I send it to?",
                    "কাকে পাঠাব?",
                    language,
                )
                self.pending_clarification = PendingClarification(text, "person", [], q, language=language)
                return ReferenceResolution(
                    resolved_text=text,
                    needs_clarification=True,
                    clarification_question=q,
                    active_mission_note=mission_note,
                )

        # 3. Ordinals ("the first one", "the second one", "the last one")
        for pattern, idx in _ORDINAL_PATTERNS:
            if pattern.search(resolved):
                items = self._ordered_list_candidates()
                if not items:
                    q = _ask_one("Which item do you mean?", "কোনটার কথা বলছেন?", language)
                    self.pending_clarification = PendingClarification(text, "item", [], q, language=language)
                    return ReferenceResolution(
                        resolved_text=text,
                        needs_clarification=True,
                        clarification_question=q,
                        active_mission_note=mission_note,
                    )
                try:
                    picked = items[idx]
                except IndexError:
                    q = _ask_choice(items[:2], language, kind="item")
                    self.pending_clarification = PendingClarification(text, "item", items[:2], q, language=language)
                    return ReferenceResolution(
                        resolved_text=text,
                        needs_clarification=True,
                        clarification_question=q,
                        ambiguous_candidates=tuple(items[:2]),
                        active_mission_note=mission_note,
                    )
                refs["item"] = picked
                resolved = pattern.sub(picked, resolved)
                break

        # 4. Demonstrative pronouns ("that one", "that file", "it", "that")
        if _DEMO_ITEM_RE.search(resolved):
            wanted_kind: str | None = None
            if re.search(r"\b(?:that|this)\s+file\b", resolved, re.I):
                wanted_kind = "file"
            elif re.search(r"\b(?:that|this)\s+command\b", resolved, re.I):
                wanted_kind = "command"
            elif re.search(r"\b(?:that|this)\s+(?:pr|issue)\b", resolved, re.I):
                wanted_kind = "target"

            # Check most recent turn with candidates of the matching kind (or general files/targets/items).
            latest_cands: list[str] = []
            for turn in reversed(self.turns):
                pool = [
                    e.value
                    for e in turn.entities
                    if (e.kind == wanted_kind if wanted_kind else e.kind in {"file", "target", "item", "command"})
                ]
                # Deduplicate preserving order
                dedup: list[str] = []
                for v in pool:
                    if v.lower() not in {d.lower() for d in dedup}:
                        dedup.append(v)
                if dedup:
                    latest_cands = dedup
                    break
            if not latest_cands and mission_note:
                mission_ents = extract_entities(mission_note, turn_id=0, source="mission")
                latest_cands = [e.value for e in mission_ents if (wanted_kind is None or e.kind == wanted_kind)]

            if len(latest_cands) == 1:
                chosen = latest_cands[0]
                refs[wanted_kind or "target"] = chosen
                if re.search(r"\b(?:that\s+one|this\s+one|that\s+file|this\s+file|that\s+pr|that\s+issue|that\s+command)\b|ওটা|এইটা|এটা", resolved, re.I):
                    resolved = re.sub(
                        r"\b(?:that\s+one|this\s+one|that\s+file|this\s+file|that\s+pr|that\s+issue|that\s+command)\b|ওটা|এইটা|এটা",
                        chosen,
                        resolved,
                        flags=re.I,
                    )
                else:
                    resolved = re.sub(r"\b(it|that)\b", chosen, resolved, count=1, flags=re.I)
            elif len(latest_cands) >= 2:
                choices = latest_cands[:2]
                q = _ask_choice(choices, language, kind=wanted_kind or "item")
                self.pending_clarification = PendingClarification(
                    text, wanted_kind or "item", choices, q, language=language
                )
                return ReferenceResolution(
                    resolved_text=text,
                    needs_clarification=True,
                    clarification_question=q,
                    ambiguous_candidates=tuple(choices),
                    active_mission_note=mission_note,
                )
            elif not refs:
                if self.workspace is not None:
                    try:
                        from ..memory import resolve_contextual_pronouns, resolve_runtime_context

                        rt = resolve_runtime_context(
                            self.workspace,
                            conversation=self,
                            window_provider=self.window_provider,
                        )
                        if rt.get("enabled"):
                            ctx_res = resolve_contextual_pronouns(resolved, rt, conversation=self)
                            if ctx_res.get("resolved"):
                                return ReferenceResolution(
                                    resolved_text=ctx_res["resolved_text"],
                                    needs_clarification=False,
                                    resolved_references=dict(ctx_res["references"]),
                                    active_mission_note=mission_note,
                                )
                    except Exception:
                        pass
                q = _ask_one("Which one do you mean?", "কোনটার কথা বলছেন?", language)
                self.pending_clarification = PendingClarification(text, wanted_kind or "item", [], q, language=language)
                return ReferenceResolution(
                    resolved_text=text,
                    needs_clarification=True,
                    clarification_question=q,
                    active_mission_note=mission_note,
                )

        return ReferenceResolution(
            resolved_text=resolved,
            needs_clarification=False,
            resolved_references=refs,
            active_mission_note=mission_note,
        )


def _ask_one(en: str, bn: str, language: str) -> str:
    return enforce_one_question(bn if (language or "").startswith("bn") else en)


def _ask_choice(choices: list[str], language: str, *, kind: str = "item") -> str:
    if len(choices) < 2:
        return _ask_one("Which one do you mean?", "কোনটার কথা বলছেন?", language)
    a, b = choices[0], choices[1]
    if (language or "").startswith("bn"):
        return enforce_one_question(f"{a} নাকি {b}?")
    if kind == "person":
        return enforce_one_question(f"Do you mean {a} or {b}?")
    return enforce_one_question(f"Do you mean {a} or {b}?")


class AcknowledgementManager:
    """Rate-limited, disableable short acknowledgements ('Ok, on it') for slow tasks."""

    DEFAULT_EN = ("Okay, on it.", "Got it, checking now.", "On it.")
    DEFAULT_BN = ("ঠিক আছে, দেখছি।", "একটু দেখছি।")

    def __init__(
        self,
        *,
        enabled: bool = True,
        min_delay_ms: int = 450,
        cooldown_seconds: float = 12.0,
        max_per_minute: int = 4,
        phrases_en: tuple[str, ...] = DEFAULT_EN,
        phrases_bn: tuple[str, ...] = DEFAULT_BN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = bool(enabled)
        self.min_delay_ms = max(0, int(min_delay_ms))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.max_per_minute = max(0, int(max_per_minute))
        self.phrases_en = tuple(phrases_en) or self.DEFAULT_EN
        self.phrases_bn = tuple(phrases_bn) or self.DEFAULT_BN
        self.clock = clock
        self._history: deque[float] = deque(maxlen=64)
        self._last_ack: float = -1e9
        self._index = 0

    def can_acknowledge(self) -> bool:
        if not self.enabled or self.max_per_minute <= 0:
            return False
        now = self.clock()
        if (now - self._last_ack) < self.cooldown_seconds:
            return False
        while self._history and (now - self._history[0]) >= 60.0:
            self._history.popleft()
        return len(self._history) < self.max_per_minute

    def next_phrase(self, language: str = "en") -> str | None:
        if not self.can_acknowledge():
            return None
        pool = self.phrases_bn if (language or "").lower().startswith("bn") else self.phrases_en
        phrase = pool[self._index % len(pool)]
        self._index += 1
        now = self.clock()
        self._last_ack = now
        self._history.append(now)
        return phrase


__all__ = [
    "AcknowledgementManager",
    "ConversationState",
    "ConversationTurn",
    "EntityMention",
    "PendingClarification",
    "ReferenceResolution",
    "extract_entities",
    "read_active_mission",
]
