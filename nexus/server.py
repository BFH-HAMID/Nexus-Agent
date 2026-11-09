"""Local HTTP + SSE server.

The original ``/dashboard`` hosts chat, repository/model controls, native
operations and public World modules on one shared server and identity. Legacy
``/panel``, ``/console`` and ``/hud`` entry points redirect here; VS Code API
consumers retain their existing JSON/SSE endpoints.

Endpoints
---------
GET  /health                     version, workspace, role/agent counts
GET  /api/roles                  the whole team
GET  /api/agents                 configured model agents
GET  /api/pipelines              multi-role pipelines
POST /api/task                   {task, role?, read_only?, max_turns?} -> {task_id}
GET  /api/events/<task_id>       SSE stream of live agent events
POST /api/approve/<task_id>      {approved, scope} - answers a pending approval
POST /api/cancel/<task_id>       stop a run
GET  /api/tasks                  run history
GET  /api/stats                  dashboard snapshot: agents, keys, telemetry
GET  /api/memory/<role>          per-role memory notes
GET  /api/knowledge/<view>       memory policy, index and retrieval
GET  /api/brain                  skills + long-term memory + past sessions
GET  /api/skills                 the learned skills library
GET  /api/skills/<name>          one SKILL.md in full
GET  /api/recall?q=              search past sessions (FTS5)
POST /api/skills                 {name, description, body, tags} -> create a skill
GET  /dashboard                  the full GUI dashboard (this is `nexus gui`)
GET  /panel                      compact panel (also embedded by the VS Code extension)

Everything is stdlib-only so the server has no extra dependencies.
"""

from __future__ import annotations

import json
import os
import select
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .agent import NexusAgent
from .config import load_config
from .mission import Mission
from .tools import ApprovalDecision
from .types import Event, EventType
from .webapi import Limiter

PANEL_DIR = Path(__file__).resolve().parent / "panel"
APPROVAL_TIMEOUT = 600.0
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


@dataclass
class Session:
    task_id: str
    task: str
    role: str | None = None
    read_only: bool = False
    pipeline: str | None = None
    auto_yes: bool = False
    local_model: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    computer_request: dict[str, Any] | None = None  # set only by the authenticated explicit computer route
    status: str = "queued"          # queued | running | awaiting_approval | done | error | cancelled
    pending: dict[str, Any] | None = None
    approval_event: threading.Event = field(default_factory=threading.Event)
    approval_decision: ApprovalDecision | None = None
    result: dict[str, Any] | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    created: float = field(default_factory=time.time)
    agent: NexusAgent | None = None
    thread: threading.Thread | None = None
    log: list[dict[str, Any]] = field(default_factory=list)
    changed: threading.Condition = field(default_factory=threading.Condition)
    sequence: int = 0
    event_callback: Any = field(default=None, repr=False)
    presence_history: list | None = field(default=None, repr=False)

    def push(self, event: dict[str, Any]) -> None:
        with self.changed:
            self.sequence += 1
            stamped = {"ts": time.time(), **event, "event_id": self.sequence}
            self.log.append(stamped)
            if len(self.log) > 4000:
                self.log = self.log[-3000:]
            self.changed.notify_all()
        if self.event_callback is not None:
            try:
                self.event_callback(stamped)
            except (ValueError, OSError, TimeoutError):
                pass  # Observational sharing never changes a tool decision/outcome.

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task": self.task,
            "role": self.role,
            "pipeline": self.pipeline,
            "github_repo": self.options.get("github_repo"),
            "status": self.status,
            "created": self.created,
            "result": self.result,
            "events": self.sequence,
        }


class ServerState:
    def __init__(self, workspace: Path, *, token: str = "", auto_yes: bool = False, quiet: bool = False,
                 demo: bool = False):
        self.workspace = Path(workspace).resolve()
        from .redaction import install_log_redaction
        install_log_redaction(self.workspace)
        self.token = token or os.environ.get("NEXUS_SERVE_TOKEN", "")
        self.auto_yes = auto_yes
        self.quiet = quiet
        self.demo = demo or os.environ.get("NEXUS_DEMO", "").lower() in {"1", "true", "yes"}
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        # New dashboard APIs share one process-wide limiter (see nexus/webapi.py).
        self.api_limiter = Limiter(limit=120, window=60.0)
        # Mission Control: one summary projection plus the global live feed.
        self.mission = Mission(self)
        self.config = load_config(self.workspace)
        self.started = time.time()
        from .gui_modules import GuiModules
        self.gui = GuiModules(self)
        self.task_queue: list[str] = []
        self.active_task: str | None = None
        self.queue_paused = False
        from .workbench import Workbench

        self.workbench = Workbench(self)

    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        """Everything the dashboard needs for its first paint."""
        from . import __version__
        from . import keys as keyring

        cfg = self.config
        with self.lock:
            sessions = list(self.sessions.values())
        running = sum(1 for s in sessions if s.status in {"queued", "running", "awaiting_approval"})
        done = sum(1 for s in sessions if s.status == "done")
        failed = sum(1 for s in sessions if s.status == "error")
        tokens_in = sum(int((s.result or {}).get("input_tokens") or 0) for s in sessions)
        tokens_out = sum(int((s.result or {}).get("output_tokens") or 0) for s in sessions)

        agents: dict[str, Any] = {}
        for name, agent in cfg.agents.items():
            agents[name] = {
                "provider": agent.provider,
                "model": agent.model,
                "base_url": agent.effective_base_url,
                "tool_mode": agent.tool_mode,
                "fallback": agent.fallback,
                "optional": agent.optional,
                "key_present": bool(agent.resolved_api_key),
                "key_family": agent.key_family,
                "key_source": agent.key_source,
                "description": agent.description,
            }
        keys = {}
        for family in keyring.PROVIDERS:
            key = keyring.get_key(family)
            keys[family] = {
                "label": keyring.PROVIDERS[family].label,
                "configured": bool(key),
                "masked": keyring.mask(key),
                "source": keyring.key_source(family),
                "console": keyring.PROVIDERS[family].console_url,
            }
        categories: dict[str, int] = {}
        for role in cfg.roles.roles.values():
            categories[role.category or "general"] = categories.get(role.category or "general", 0) + 1
        return {
            "version": __version__,
            "demo": self.demo,
            "workspace": str(self.workspace),
            "uptime_s": round(time.time() - self.started, 1),
            "default_agent": cfg.default_agent,
            "prefer_local": cfg.settings.prefer_local,
            "dashboard_policy": {"local_only": self.workbench.settings()["local_only"]},
            "local_agents": cfg.local_agents,
            "roles": len(cfg.roles),
            "categories": dict(sorted(categories.items())),
            "agents": agents,
            "keys": keys,
            "pipelines": {
                name: {"description": pipe.description,
                       "steps": [str(step.get("role") or "?") for step in pipe.steps]}
                for name, pipe in cfg.pipelines.items()
            },
            "settings": {
                "auto_route": cfg.settings.auto_route,
                "max_turns": cfg.settings.max_turns,
                "router_agent": cfg.settings.router_agent,
                "memory_enabled": cfg.settings.memory_enabled,
            },
            "sessions": {"total": len(sessions), "running": running, "done": done, "error": failed},
            "tokens": {"in": tokens_in, "out": tokens_out},
            "brain": self.brain_summary(),
            "auth_required": bool(self.token),
        }

    def brain_summary(self) -> dict[str, Any]:
        """Cheap counts for the dashboard header (no transcript reads)."""
        try:
            from . import skills as skilllib
            from .memory import MemoryStore, env_flag
            from .sessions import SessionStore

            memory = MemoryStore(enabled=env_flag("NEXUS_MEMORY", bool(self.config.settings.memory_enabled)))
            stats = SessionStore().stats()
            return {
                "skills": len(skilllib.discover(self.workspace)),
                "memory_entries": len(memory.memory.entries),
                "user_entries": len(memory.user.entries),
                "sessions": stats["sessions"],
                "learning": bool(getattr(self.config.settings, "learning", True)),
            }
        except Exception:  # noqa: BLE001 - never break /api/stats
            return {"skills": 0, "memory_entries": 0, "user_entries": 0, "sessions": 0, "learning": False}

    # -- learning surfaces ------------------------------------------------ #
    def skills(self) -> list[dict[str, Any]]:
        from . import skills as skilllib

        rows = []
        for skill in skilllib.discover(self.workspace):
            data = skill.to_dict()
            data["uses"] = int(skill.usage().get("uses", 0))
            data["last_used"] = skill.usage().get("last_used")
            rows.append(data)
        return rows

    def brain(self) -> dict[str, Any]:
        from .memory import MemoryStore, env_flag
        from .sessions import SessionStore

        store = self.config.settings
        memory = MemoryStore(enabled=env_flag("NEXUS_MEMORY", bool(store.memory_enabled)))
        sessions = SessionStore()
        library = self.skills()
        return {
            "skills": {"total": len(library), "recent": library[:12],
                        "categories": sorted({s.get("category") or "general" for s in library})},
            "memory": {"enabled": memory.enabled, "memory": memory.memory.to_dict(),
                        "user": memory.user.to_dict()},
            "sessions": {**sessions.stats(), "recent": sessions.recent(8)},
            "learning": {
                "enabled": bool(getattr(store, "learning", True)),
                "agent": getattr(store, "learning_agent", "") or store.router_agent,
                "min_tools": int(getattr(store, "learning_min_tools", 4) or 0),
                "nudge_interval": int(getattr(store, "skill_nudge_interval", 8) or 0),
            },
        }

    def recall(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        from .sessions import SessionStore

        store = SessionStore()
        return store.search(query, limit=limit) if query else store.recent(limit)

    def create_skill(self, payload: dict[str, Any]) -> dict[str, Any]:
        from . import skills as skilllib

        name = str(payload.get("name") or "").strip().lower()
        if not name:
            raise ValueError("name is required")
        path = skilllib.create_skill(
            name=name,
            description=str(payload.get("description") or ""),
            body=str(payload.get("body") or ""),
            tags=payload.get("tags") or [],
            category=str(payload.get("category") or "learned"),
            workspace=self.workspace,
            overwrite=bool(payload.get("overwrite")),
        )
        return {"ok": True, "path": str(path), "name": name}

    def memory(self, role: str) -> dict[str, Any]:
        from .tools import RoleMemory

        memory = RoleMemory(self.workspace, self.config.settings.memory_dir,
                            enabled=self.config.settings.memory_enabled)
        name = self.config.role(role).name
        return {
            "role": name,
            "path": str(memory.path_for(name)),
            "enabled": memory.enabled,
            "text": memory.read(name),
        }

    def create(self, task: str, role: str | None, read_only: bool, pipeline: str | None = None,
               auto_yes: bool = False, local_model: str | None = None, options: dict | None = None) -> Session:
        session = Session(task_id=uuid.uuid4().hex[:12], task=task, role=role, read_only=read_only,
                          pipeline=pipeline, auto_yes=auto_yes, local_model=local_model,
                          options=options or self.workbench.prepare({"local_model": local_model}))
        if pipeline and self.config.pipeline(pipeline):
            session.options["pipeline_steps"] = self.config.pipeline(pipeline).steps
        with self.lock:
            self.sessions[session.task_id] = session
        return session

    def decide_approval(self, task_id: str, approved: bool, scope: str = "once",
                        approval_id: str | None = None, reason: str = "") -> tuple[int, dict[str, Any]]:
        """Answer one live task approval. Returns (http_status, body).

        The Run page and the Approval Center both call this so there is exactly
        one set of checks: boolean decision, valid scope, current approval id,
        and a fresh preview before any file-changing grant.
        """
        session = self.get(task_id)
        if session is None or session.pending is None:
            return 409, {"error": "no pending approval"}
        if not isinstance(approved, bool) or scope not in {"once", "session"}:
            return 400, {"error": "approved must be boolean; scope must be once or session"}
        with session.changed:
            if (session.pending is None or session.approval_event.is_set()
                    or session.cancel.is_set()
                    or (getattr(session, 'computer_request', None) is not None and approval_id != session.pending['approval_id'])
                    or (approval_id and approval_id != session.pending["approval_id"])):
                return 409, {"error": "approval is no longer pending; refresh task status"}
            diff = session.pending.get("proposal", {})
            if approved and diff.get("available"):
                from .workbench import proposal

                try:
                    current = proposal(self.workspace, session.pending["tool"], session.pending["args"])
                except (ValueError, OSError, UnicodeError) as exc:
                    return 409, {"error": "Cannot revalidate preview: " + str(exc)}
                if current.get("before_hash") != diff.get("before_hash"):
                    return 409, {"error": "File changed since preview; deny and request a fresh edit"}
            session.approval_decision = ApprovalDecision(
                approved=approved, scope=scope, reason=str(reason or ""),
            )
            session.approval_event.set()
        return 200, {"ok": True}

    def get(self, task_id: str) -> Session | None:
        with self.lock:
            return self.sessions.get(task_id)

    def start(self, session: Session) -> None:
        self.workbench.save_run(session)
        with self.lock:
            self.task_queue.append(session.task_id)
        self.schedule()

    def schedule(self) -> None:
        with self.lock:
            if self.active_task or getattr(self, "workflow_busy", False) or self.queue_paused or not self.task_queue:
                return
            ident = self.task_queue.pop(0)
            session = self.sessions[ident]
            self.active_task = ident
            def worker():
                try:
                    self._run(session)
                finally:
                    with self.lock:
                        self.active_task = None
                    self.schedule()
            session.thread = threading.Thread(target=worker, daemon=True, name=f"nexus-{ident}")
            session.thread.start()

    # ------------------------------------------------------------------ #
    def _notice(self, session: Session) -> None:
        """Record one real notice for a finished run. Never raises."""
        try:
            from . import channels

            result = session.result or {}
            detail = " · ".join(part for part in (
                str(result.get("role") or result.get("agent") or ""),
                f"{result.get('duration_s')}s" if result.get("duration_s") is not None else "",
                f"{result.get('tool_calls')} tool call(s)" if result.get("tool_calls") else "",
            ) if part)
            if result.get("stopped_reason"):
                detail = (detail + " · " + str(result["stopped_reason"]))[:300]
            if result.get("error"):
                detail = (detail + " · " + str(result["error"]))[:300]
            channels.record(self, "run", state_name=session.status,
                            title=session.task or "Run finished", detail=detail,
                            task_id=session.task_id)
        except Exception:  # noqa: BLE001 - history must not fail because an alert did
            pass

    def _emit(self, session: Session):
        def handler(event: Event) -> None:
            if session.cancel.is_set() and event.type not in {EventType.TOOL_OUTPUT, EventType.PROCESS, EventType.TOOL_RESULT, EventType.FILE_CHANGE}:
                raise RuntimeError("run cancelled")
            serialized = event.to_json()
            self.workbench.studio.checkpoint(session, serialized)
            session.push(serialized)
            if not self.quiet:
                kind = event.type.value
                if kind in {"role", "agent", "error"}:
                    print(f"[{session.task_id}] {kind}: {event.data}")
        return handler

    def _approval(self, session: Session):
        def callback(spec, args, risk, preview) -> ApprovalDecision:
            with session.changed:
                if session.cancel.is_set():
                    return ApprovalDecision(False, reason="cancelled")
                session.approval_event.clear()
                session.approval_decision = None
                session.status = "awaiting_approval"
                try:
                    from .workbench import proposal

                    diff = (self.workbench.repositories.github.tasks.proposal(session, spec.name, args)
                            if session.options.get("github_repo") else proposal(self.workspace, spec.name, args))
                except (ValueError, OSError, UnicodeError) as exc:
                    if session.options.get("github_repo"):
                        return ApprovalDecision(False, reason="Remote approval preview failed: " + str(exc))
                    diff = {"available": False, "reason": str(exc)}
                session.pending = {
                    "proposal": diff,
                    "approval_id": uuid.uuid4().hex,
                    "tool": spec.name, "risk": risk.value, "args": args, "preview": preview,
                    "requested": time.time(), "timeout": APPROVAL_TIMEOUT,
                }
                session.push({"type": "approval_request", **session.pending})
                try:
                    from . import channels

                    channels.record(self, "approval", state_name="awaiting_approval",
                                    title=session.task or "Approval requested",
                                    detail=f"{spec.name} · risk {getattr(risk, 'value', risk)}",
                                    task_id=session.task_id)
                except Exception:  # noqa: BLE001 - an alert must never block an approval
                    pass
            got = session.approval_event.wait(APPROVAL_TIMEOUT)
            with session.changed:
                session.pending = None
                session.status = "cancelled" if session.cancel.is_set() else "running"
                decision = (session.approval_decision if got else None) or ApprovalDecision(
                    False, reason="no decision" if got else "approval timed out")
            session.push({"type": "approval_response", "tool": spec.name, "approved": decision.approved,
                          "scope": decision.scope, "reason": decision.reason})
            return decision
        return callback

    def _run(self, session: Session) -> None:
        try:
            if session.cancel.is_set():
                raise RuntimeError("run cancelled")
            if getattr(session, 'computer_request', None) is not None:
                from .computer_sessions import run as run_computer
                run_computer(self, session)
                return
            auto_yes = self.auto_yes or session.auto_yes
            config = load_config(self.workspace)
            if self.workbench.settings()["local_only"]:
                session.options["policy"]["local_only"] = True
                session.options["policy"]["default_local_model"] = self.workbench.settings()["default_local_model"]
            self.workbench.configure(config, session.options, session.local_model)
            self.workbench.studio.configure(config, session)
            agent = NexusAgent(
                config,
                workspace=self.workspace,
                emit=self._emit(session),
                approval_callback=None if auto_yes else self._approval(session),
                auto_yes=auto_yes,
            )
            agent.cancel_event = session.cancel
            if hasattr(agent, "gate"):
                agent.gate.policy = self.workbench.studio.policy(agent, session, self.workbench.write_policy(session.options["policy"]))
                agent.gate.callback = self._approval(session)
                self.workbench.studio.attach(agent, session)
            if session.options.get("github_repo"):
                remote = self.workbench.repositories.github.tasks
                record = remote.start(session)
                remote.attach(agent, session, record)
            if self.demo:
                from .demo import patch_agent_for_demo

                patch_agent_for_demo(agent)
                session.push({"type": "status", "message": "DEMO MODE - scripted run, no model called"})
            session.agent = agent
            session.status = "running"
            session.push({"type": "status", "message": f"starting run in {getattr(agent, 'remote_workspace', self.workspace)}"})
            steps: list[dict[str, Any]] = []
            if session.pipeline:
                session.push({"type": "status",
                              "message": f"pipeline '{session.pipeline}' -> {session.task}"})
                if "retry_from" in session.options:
                    pipe = config.pipeline(session.pipeline)
                    results = agent.run_steps(pipe.steps[session.options["retry_from"]:],
                        session.task + "\nPrior successful handoff:\n" + session.options.get("handoff", ""),
                        pipeline=pipe, read_only=session.read_only)
                else:
                    results = agent.run_pipeline(session.pipeline, session.task, read_only=session.read_only)
                steps = [
                    {"role": r.role, "agent": r.agent, "ok": r.ok, "summary": r.summary,
                     "tool_calls": r.tool_calls, "turns": r.turns}
                    for r in results
                ]
                if not results:
                    raise RuntimeError(f"pipeline '{session.pipeline}' produced no steps")
                result = results[-1]
            else:
                result = agent.run(
                    session.task,
                    role=session.role or None,
                    read_only=session.read_only,
                    **({'history': session.presence_history} if session.presence_history is not None else {}),
                )
            session.result = {
                "trace_id": getattr(result, "trace_id", ""),
                "session_id": getattr(result, "session_id", ""),
                "ok": result.ok,
                "execution": result.execution,
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
                "pipeline": session.pipeline,
                "steps": steps,
            }
            if session.options.get("github_repo"):
                session.result["github_task"] = self.workbench.repositories.github.tasks.get(session.task_id)
            if session.pipeline:
                session.result.update(
                    turns=sum(r.turns for r in results), tool_calls=sum(r.tool_calls for r in results),
                    input_tokens=sum(r.usage.input_tokens for r in results),
                    output_tokens=sum(r.usage.output_tokens for r in results),
                    duration_s=round(sum(r.duration_s for r in results), 2),
                    changed_files=sorted({name for r in results for name in r.changed_files}),
                )
            session.status = "cancelled" if session.cancel.is_set() else ("done" if result.ok else "error")
        except Exception as exc:  # noqa: BLE001
            session.status = "cancelled" if session.cancel.is_set() else "error"
            session.result = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
            session.push({"type": "error", "message": str(exc)})
        finally:
            try:
                self.workbench.studio.finished(session)
            except Exception as exc:
                session.result = {**(session.result or {}), "evidence_error": type(exc).__name__}
                session.push({"type": "status", "message": "Studio evidence/history update failed: " +
                              self.workbench.studio.redact_text(str(exc))[:300]})
            try:
                self.workbench.save_run(session)
            except Exception as exc:
                session.result = {**(session.result or {}), "history_error": type(exc).__name__}
                session.push({"type": "error", "message": "Run finished but durable history could not be saved"})
            session.push({"type": "done", "status": session.status, "result": session.result})
            self._notice(session)
            try:
                self.workbench.save_run(session)
            except Exception:
                pass  # Completion must still reach SSE if history storage is unavailable.


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #


def _client_disconnected(connection: Any) -> bool:
    """Return True when the peer has closed or reset its TCP connection."""
    if connection is None or not hasattr(connection, "recv"):
        return False
    try:
        fileno = connection.fileno()
    except (AttributeError, OSError, ValueError):
        return True
    if not isinstance(fileno, int) or fileno < 0:
        return True
    try:
        if hasattr(select, "poll"):
            poller = select.poll()
            rdhup = getattr(select, "POLLRDHUP", 0)
            poller.register(fileno, select.POLLIN | select.POLLERR | select.POLLHUP | rdhup)
            events = poller.poll(0)
            if not events:
                return False
            mask = events[0][1]
            if mask & (select.POLLERR | select.POLLHUP | select.POLLNVAL | rdhup):
                return True
            if not (mask & select.POLLIN):
                return False
        else:
            readable, _, errored = select.select([connection], [], [connection], 0)
            if errored:
                return True
            if not readable:
                return False
        peek_flag = getattr(socket, "MSG_PEEK", 0)
        if not peek_flag:
            return False
        try:
            return connection.recv(1, peek_flag) == b""
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True
    except (OSError, ValueError):
        return True


def make_handler(state: ServerState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"nexus/{__version__}"

        # -- plumbing ------------------------------------------------------ #
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            # Requests may contain legacy query credentials; never log their URLs.
            if not state.quiet:
                pass

        def handle_one_request(self) -> None:
            """Swallow the disconnect noise an SSE client leaves behind."""
            try:
                super().handle_one_request()
            except (ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        def handle_error(self, *args: Any) -> None:  # pragma: no cover
            pass

        def _origin_ok(self):
            host = self.headers.get('Host', '')
            try:
                import re
                if not re.fullmatch(r'(?:[A-Za-z0-9.-]+|\[[A-Fa-f0-9:]+\])(?::[0-9]+)?', host):
                    return False
                parsed_host = urlparse('http://' + host)
                if parsed_host.port is not None and not 1 <= parsed_host.port <= 65535:
                    return False
                hostname = parsed_host.hostname
                allowed = {'localhost', '127.0.0.1', '::1', str(self.server.server_address[0])}
                allowed.update(x.strip() for x in os.environ.get('NEXUS_ALLOWED_HOSTS', '').split(',') if x.strip())
                origin = self.headers.get('Origin')
                return hostname in allowed and (origin is None or origin in {'http://' + host, 'https://' + host})
            except ValueError:
                return False

        def _authorized(self) -> bool:
            self._principal = {'kind': 'operator', 'id': 'operator', 'scopes': []}
            if not state.token:
                return True  # Legacy loopback API; new presence always requires a token.
            import hmac

            from .gui_session import valid
            from .hud.devices import Devices, cookie_token
            provided = self.headers.get('X-Nexus-Token', '')
            if provided:
                if hmac.compare_digest(provided.encode(), state.token.encode()):
                    return True
                identity = Devices(state.workspace, state.token).authenticate(provided)
                if identity:
                    self._principal = identity
                return identity is not None
            query = parse_qs(urlparse(self.path).query)
            provided = (query.get('token') or [''])[0]  # Legacy master-token clients only.
            if provided:
                return hmac.compare_digest(provided.encode(), state.token.encode())
            device, presented = cookie_token(self.headers.get('Cookie', ''))
            if presented:
                identity = Devices(state.workspace, state.token).authenticate(device)
                if identity:
                    self._principal = identity
                return identity is not None
            return valid(state, self.headers.get('Host', ''), self.headers.get('Cookie', ''))

        def _device_allowed(self, path):
            if self._principal['kind'] != 'device' or self._public_asset(path):
                return True
            return path == '/api/session' or path.startswith('/api/presence/')

        def _credential_presented(self, parsed) -> bool:
            """Did this request actually offer a credential? A signed-out tab does not."""
            if self.headers.get('X-Nexus-Token') or self.headers.get('Cookie'):
                return True
            return bool((parse_qs(parsed.query).get('token') or [''])[0])

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            # A rejected upload may still have unread body bytes. Never parse those
            # as the next request on an HTTP/1.1 keep-alive connection.
            if self.command == 'POST' and status >= 400:
                self.close_connection = True
                self.send_header('Connection', 'close')
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Referrer-Policy", "no-referrer")
            from .gui_session import renew, valid
            if (self.command == 'GET' and urlparse(self.path).path == '/api/session'
                    and not getattr(self, '_session_cookie', None) and status == 200
                    and getattr(self, '_principal', {}).get('kind') != 'device'
                    and valid(state, self.headers.get('Host', ''), self.headers.get('Cookie', ''))):
                self._session_cookie = renew(state, self.headers.get('Host', ''), self.headers.get('Cookie', ''))
            if getattr(self, '_session_cookie', None):
                secure = '; Secure' if os.environ.get('NEXUS_GUI_HTTPS_PROXY') == '1' else ''
                self.send_header('Set-Cookie', self._session_cookie + secure)
                self._session_cookie = None
            for cookie in getattr(self, '_extra_cookies', []):
                secure = '; Secure' if os.environ.get('NEXUS_GUI_HTTPS_PROXY') == '1' else ''
                self.send_header('Set-Cookie', cookie + secure)
            self._extra_cookies = []
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            if getattr(self, '_session_cookie', None):
                secure = '; Secure' if os.environ.get('NEXUS_GUI_HTTPS_PROXY') == '1' else ''
                self.send_header('Set-Cookie', self._session_cookie + secure)
                self._session_cookie = None
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return {}
            return data if isinstance(data, dict) else {}

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Nexus-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        # -- routes -------------------------------------------------------- #
        @staticmethod
        def _public_asset(path: str) -> bool:
            """UI files carry no data - the browser must be able to load them
            before it knows the token (the token lives in the page's query string)."""
            return (
                path == "/" or path.startswith("/world/")
                or path in {"/dashboard", "/gui", "/panel", "/console", "/hud",
                            "/manifest.webmanifest", "/assistant-sw.js", "/assistant-icon.svg"}
                or path.startswith(("/dashboard/", "/gui/", "/panel/", "/console/"))
            )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if not self._origin_ok():
                return self._send_json({'error': 'Host/origin refused'}, 403)
            if not self._authorized() and not self._public_asset(path):
                from . import securitycenter

                securitycenter.note_denied(state, path, self._rolehub_client(),
                                           presented=self._credential_presented(parsed))
                return self._send_json({"error": "invalid or missing token"}, 401)

            if not self._device_allowed(path):
                return self._send_json({'error': 'Device token is scoped to the presence API, not administration'}, 403)

            if path == "/":
                return self._redirect("/dashboard" + ("?" + parsed.query if parsed.query else ""))

            if path == '/api/session':
                return self._send_json({'authenticated': True, 'required': bool(state.token), 'principal': self._principal})
            if path.startswith('/api/presence/'):
                return self._presence_get(path.removeprefix('/api/presence/'))
            if path in {"/manifest.webmanifest", "/assistant-sw.js", "/assistant-icon.svg"}:
                content_type = {"webmanifest": "application/manifest+json",
                                "js": "text/javascript", "svg": "image/svg+xml"}[path.rsplit(".", 1)[-1]]
                return self._static(path.lstrip("/"), content_type)
            if path.startswith(('/api/vision/', '/api/assistant/vision/', '/api/computer/')) and not state.token:
                return self._send_json({'error': 'Capture/computer HTTP requires a configured authentication token'}, 401)
            if path == '/api/vision/status':
                from .vision.api import get as vision_status
                return self._send_json(vision_status(state))
            if path.startswith("/api/assistant/"):
                from .assistant import api as assistant_api
                client = self._rolehub_client()
                bucket = "assistant-read:" + client
                if not state.api_limiter.allow(bucket):
                    return self._send_json({"error": "Assistant rate limit reached"}, 429)
                try:
                    result = assistant_api.get(state, path.removeprefix("/api/assistant/"), parse_qs(parsed.query))
                    return self._send_json(result)
                except Exception as exc:
                    return self._send_json({"error": state.workbench.studio.redact_text(str(exc))[:1000]}, 400)
            if path == "/api/world/overview":
                return self._world_read("world.overview", lambda desk: desk.overview(state))
            if path.startswith("/api/world/"):
                return self._send_json({"error": f"no route for {path}"}, 404)
            if path == "/api/settings/summary":
                return self._settings_read()
            if path.startswith("/api/settings/"):
                return self._send_json({"error": f"no route for {path}"}, 404)
            from .gui_modules import module_path
            if module_path(path):
                status, data = state.gui.call('GET', path, parse_qs(parsed.query))
                return self._send_json(data, status)
            if path.startswith('/world/'):
                from .hud.world.core import ASSETS
                name = path.removeprefix('/world/')
                permitted = {'world.js', 'world.js.LEGAL.txt', 'world.css', 'world.vendor.css', 'THIRD-PARTY.txt',
                             'NotoSansBengali-Regular.ttf', 'OFL-Bengali.txt', 'countries-detail.geojson',
                             'countries-low.geojson', 'earth-night.jpg', 'provenance.json'}
                if name not in permitted:
                    return self._send_json({'error': 'Unknown asset'}, 404)
                types = {'.ttf': 'font/ttf', '.geojson': 'application/geo+json', '.txt': 'text/plain; charset=utf-8'}
                return self._send_bytes((ASSETS / name).read_bytes(), types.get(Path(name).suffix, STATIC_TYPES.get(Path(name).suffix, 'application/octet-stream')))
            if path in {"/health", "/api/health"}:
                cfg = state.config
                return self._send_json({
                    "ok": True,
                    "version": __version__,
                    "demo": state.demo,
                    "workspace": str(state.workspace),
                    "roles": len(cfg.roles),
                    "agents": sorted(cfg.agents),
                    "default_agent": cfg.default_agent,
                    "pipelines": sorted(cfg.pipelines),
                    "auto_yes": state.auto_yes,
                    "auth_required": bool(state.token),
                })
            if path.startswith("/api/workbench/"):
                try:
                    return self._send_json(state.workbench.get(path[len("/api/workbench/"):], parse_qs(parsed.query)))
                except Exception as exc:
                    return self._send_json({"error": str(exc)[:2000]}, 400)
            if path == "/api/ollama":
                from .integrations import ollama_models

                return self._send_json(ollama_models())
            if path == "/api/github":
                from .integrations import github_status

                return self._send_json(github_status(state.workspace))
            if path == "/api/palette":
                return self._palette_index()
            if path == "/api/mission/summary":
                return self._mission_summary()
            if path == "/api/mission/stream":
                return self._mission_stream()
            if path.startswith("/api/router/"):
                if path not in {"/api/router/overview", "/api/router/rules", "/api/router/chains",
                                "/api/router/models", "/api/router/accuracy", "/api/router/history",
                                "/api/router/lab"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)
                if path == "/api/router/overview":
                    return self._router_read("routerstudio.overview", lambda studio: studio.overview(state))
                if path == "/api/router/rules":
                    return self._router_read("routerstudio.rules", lambda studio: studio.rules(state))
                if path == "/api/router/chains":
                    return self._router_read("routerstudio.chains", lambda studio: studio.chains(state))
                if path == "/api/router/models":
                    return self._router_read("routerstudio.models", lambda studio: studio.model_ranking(state))
                if path == "/api/router/accuracy":
                    return self._router_read("routerstudio.accuracy", lambda studio: studio.accuracy(state))
                if path == "/api/router/history":
                    return self._router_read("routerstudio.history", lambda studio: {
                        "schema": studio.SCHEMA, "observed_at": __import__("time").time(),
                        "versions": studio.versions(state.workspace),
                        "store": f"{studio.STORE_DIR}/{studio.HISTORY_DIR} (runtime state, never committed)"})
                return self._router_read("routerstudio.lab", lambda studio: studio.lab(state))
            if path == "/api/security/audit":
                query = parse_qs(parsed.query)
                raw = (query.get("limit") or ["120"])[0]
                limit = int(raw) if raw.isdigit() else 120
                import re

                wanted = (query.get("action") or [""])[0][:120]
                if wanted and not re.fullmatch(r"[a-z0-9._-]+", wanted):
                    from . import webapi as _webapi

                    return self._send_json(_webapi.failed(
                        "invalid_request", "action filter must be a prefix like security."), 400)
                return self._security_read("security.audit", lambda center: {
                    "schema": center.SCHEMA, "observed_at": time.time(),
                    "events": center.read_events(state.workspace, limit, wanted),
                    "chain": center.verify_chain(state.workspace)})
            if path.startswith("/api/security/"):
                if path not in {"/api/security/overview", "/api/security/vault", "/api/security/sessions",
                                "/api/security/policy", "/api/security/two-factor", "/api/security/injection",
                                "/api/security/snapshots", "/api/security/snapshot"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)
                from . import securitycenter

                nonce = securitycenter.cookie_nonce(self.headers.get("Cookie", ""))
                if path == "/api/security/overview":
                    return self._security_read("security.overview",
                                               lambda center: center.overview(state, nonce))
                if path == "/api/security/vault":
                    return self._security_read("security.vault", lambda center: center.vault_view(state))
                if path == "/api/security/sessions":
                    return self._security_read("security.sessions",
                                               lambda center: center.sessions_view(state.workspace, nonce))
                if path == "/api/security/policy":
                    return self._security_read("security.policy", lambda center: center.policy_view(state))
                if path == "/api/security/two-factor":
                    return self._security_read("security.twofactor",
                                               lambda center: center.totp_status(state.workspace))
                if path == "/api/security/snapshots":
                    return self._security_read("security.snapshots",
                                               lambda center: center.snapshots_view(state.workspace))
                if path == "/api/security/snapshot":
                    wanted = (parse_qs(parsed.query).get("name") or [""])[0]
                    return self._security_read("security.snapshot", lambda center: center.snapshot_detail(
                        state.workspace, wanted)["blob"])
                from .sessions import SessionStore
                store = SessionStore()
                return self._security_read("security.injection", lambda center: {
                    "schema": center.SCHEMA, "observed_at": time.time(),
                    "sessions": store.recent_for(state.workspace, limit=20),
                    "rules": [name for name, _ in center.INJECTION_RULES]})
            if path == "/api/knowledge/overview":
                return self._knowledge_read("knowledge.overview", lambda knowledge: knowledge.overview(state))
            if path.startswith("/api/knowledge/"):
                query = parse_qs(parsed.query)
                if path not in {"/api/knowledge/policy", "/api/knowledge/memory", "/api/knowledge/collections",
                                "/api/knowledge/collection", "/api/knowledge/search", "/api/knowledge/context",
                                "/api/knowledge/personal/list", "/api/knowledge/personal/export"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)
                from . import knowledge

                if path == "/api/knowledge/personal/list":
                    q = (query.get("q") or [""])[0][:400]
                    status_filter = (query.get("status") or [""])[0][:40]
                    return self._knowledge_read(
                        "knowledge.personal.list",
                        lambda module: module.personal_list(state, query=q, status=status_filter),
                    )
                if path == "/api/knowledge/personal/export":
                    return self._knowledge_read(
                        "knowledge.personal.export",
                        lambda module: module.personal_export(state),
                    )
                if path == "/api/knowledge/policy":
                    return self._knowledge_read("knowledge.policy",
                                                lambda module: module.policy_view(state.workspace))
                if path == "/api/knowledge/memory":
                    return self._knowledge_read("knowledge.memory", lambda module: module.memory_view(state))
                if path == "/api/knowledge/collections":
                    return self._knowledge_read("knowledge.collections",
                                                lambda module: module.collections_view(state.workspace))
                if path == "/api/knowledge/collection":
                    wanted = (query.get("name") or [""])[0]
                    return self._knowledge_read("knowledge.collection", lambda module: module.collection_view(
                        state.workspace, wanted))
                if path == "/api/knowledge/search":
                    text = (query.get("q") or [""])[0]
                    name = (query.get("collection") or [""])[0]
                    raw = (query.get("limit") or ["0"])[0]
                    limit = int(raw) if raw.isdigit() else 0
                    return self._knowledge_read("knowledge.search", lambda module: knowledge.search(
                        state, text, name, limit, client=self._rolehub_client()))
                task = (query.get("task") or [""])[0][:400]
                raw = (query.get("top_k") or ["0"])[0]
                return self._knowledge_read("knowledge.context", lambda module: knowledge.context(
                    state, task, int(raw) if raw.isdigit() else 0))
            if path == "/api/channels/overview":
                return self._channels_read("channels.overview", lambda channels: channels.overview(state))
            if path.startswith("/api/channels/"):
                query = parse_qs(parsed.query)
                if path not in {"/api/channels/inbox", "/api/channels/delivery",
                                "/api/channels/telegram", "/api/channels/policy"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)

                if path == "/api/channels/inbox":
                    raw = (query.get("limit") or ["60"])[0]
                    limit = int(raw) if raw.isdigit() else 60
                    return self._channels_read("channels.inbox",
                                               lambda module: module.inbox(state, limit))
                if path == "/api/channels/delivery":
                    return self._channels_read("channels.delivery",
                                               lambda module: module.delivery_view(state))
                if path == "/api/channels/telegram":
                    return self._channels_read("channels.telegram",
                                               lambda module: module.telegram_view(state))
                return self._channels_read("channels.policy",
                                           lambda module: module.policy_view(state.workspace))
            if path == "/api/cost/overview":
                return self._cost_read("cost.overview", lambda cost: cost.overview(state))
            if path.startswith("/api/cost/"):
                query = parse_qs(parsed.query)
                if path not in {"/api/cost/policy", "/api/cost/report", "/api/cost/latency",
                                "/api/cost/breakdown", "/api/cost/budget", "/api/cost/providers",
                                "/api/cost/compare", "/api/cost/comparisons", "/api/cost/resources"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)

                def number(name, default):
                    raw = (query.get(name) or [str(default)])[0]
                    return int(raw) if raw.isdigit() else default

                def word(name, default):
                    return (query.get(name) or [default])[0]

                if path == "/api/cost/policy":
                    return self._cost_read("cost.policy", lambda module: module.policy_view(state.workspace))
                if path == "/api/cost/report":
                    return self._cost_read("cost.report", lambda module: module.report(state, number("days", 7)))
                if path == "/api/cost/latency":
                    return self._cost_read("cost.latency", lambda module: module.latency_view(
                        state, number("days", 7), word("group", "model")))
                if path == "/api/cost/breakdown":
                    return self._cost_read("cost.breakdown", lambda module: module.breakdown(
                        state, number("days", 7), word("group", "model")))
                if path == "/api/cost/budget":
                    return self._cost_read("cost.budget", lambda module: module.budget_state(state.workspace))
                if path == "/api/cost/providers":
                    return self._cost_read("cost.providers", lambda module: module.providers(state))
                if path == "/api/cost/comparisons":
                    return self._cost_read("cost.comparisons", lambda module: module.comparisons(
                        state.workspace, number("limit", 20)))
                if path == "/api/cost/resources":
                    return self._cost_read("cost.resources", lambda module: module.resources(state))
                ident = word("id", "")
                if not ident:
                    return self._send_json({"error": "An id is required"}, 400)
                return self._cost_read("cost.compare", lambda module: module.compare_status(state, ident))
            if path.startswith("/api/workflows/"):
                query = parse_qs(parsed.query)
                if path == "/api/workflows/overview":
                    return self._workflow_read("workflow.overview", lambda studio: studio.overview(state))
                if path == "/api/workflows/families":
                    return self._workflow_read("workflow.families", lambda studio: studio.families(state))
                if path == "/api/workflows/schedules":
                    return self._workflow_read("workflow.schedules", lambda studio: {
                        **studio.schedules_view(state), "scheduler": studio.scheduler_for(state).status()})
                if path == "/api/workflows/runs":
                    raw = (query.get("limit") or ["60"])[0]
                    limit = int(raw) if raw.isdigit() else 60
                    return self._workflow_read("workflow.runs",
                                               lambda studio: studio.run_rows(state, limit))
                if path in {"/api/workflows/version", "/api/workflows/run"}:
                    identifier = (query.get("id") or [""])[0]
                    if not identifier:
                        return self._send_json({"error": "id is required"}, 400)
                    if path == "/api/workflows/version":
                        return self._workflow_read("workflow.version",
                                                   lambda studio: studio.version_detail(state, identifier))
                    return self._workflow_read("workflow.run",
                                               lambda studio: studio.run_detail(state, identifier))
                if path in {"/api/workflows/diff", "/api/workflows/run-diff"}:
                    left = (query.get("a") or [""])[0]
                    right = (query.get("b") or [""])[0]
                    if not left or not right:
                        return self._send_json({"error": "a and b are required"}, 400)
                    if path == "/api/workflows/diff":
                        return self._workflow_read("workflow.diff",
                                                   lambda studio: studio.diff_versions(state, left, right))
                    return self._workflow_read("workflow.run-diff",
                                               lambda studio: studio.diff_runs(state, left, right))
                return self._send_json({"error": f"no route for {path}"}, 404)
            if path.startswith("/api/approvals/"):
                if path not in {"/api/approvals/inbox", "/api/approvals/policy", "/api/approvals/history"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)
                if path == "/api/approvals/inbox":
                    return self._approvals_read("approval.inbox", lambda center: center.inbox(state))
                if path == "/api/approvals/policy":
                    return self._approvals_read("approval.policy", lambda center: center.policy(state))
                try:
                    limit = int((parse_qs(parsed.query).get("limit") or ["40"])[0])
                except ValueError:
                    limit = 40
                return self._approvals_read("approval.history",
                                            lambda center: center.history(state, limit))
            if path.startswith("/api/computer/"):
                if path not in {"/api/computer/state", "/api/computer/files", "/api/computer/trash",
                                "/api/computer/browser", "/api/computer/snapshots"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)
                query = parse_qs(parsed.query)
                if path == "/api/computer/state":
                    return self._computer_read("computer.state", lambda studio: studio.state_view(state))
                if path == "/api/computer/files":
                    try:
                        depth = int((query.get("depth") or ["1"])[0])
                    except ValueError:
                        depth = 1
                    return self._computer_read("computer.files", lambda studio: studio.tree(state, {
                        "path": (query.get("path") or ["."])[0], "depth": depth}))
                if path == "/api/computer/trash":
                    return self._computer_read("computer.trash", lambda studio: studio.trash_view(state))
                if path == "/api/computer/browser":
                    return self._computer_read("computer.browser", lambda studio: studio.browser_view(state))
                return self._computer_read("computer.snapshots", lambda studio: {
                    "schema": studio.SCHEMA, "observed_at": __import__("time").time(),
                    "items": studio.snapshots(state)})
            if path.startswith("/api/roles/") and path not in {"/api/roles/index", "/api/roles/detail",
                                                               "/api/roles/history", "/api/roles/diff",
                                                               "/api/roles/quality"}:
                return self._send_json({"error": f"no route for {path}"}, 404)
            if path == "/api/roles/quality":
                return self._rolehub_read("rolehub.quality", lambda hub: hub.quality(state))
            if path == "/api/roles/index":
                return self._rolehub_read("rolehub.index", lambda hub: hub.index(state, parse_qs(parsed.query)))
            if path == "/api/roles/detail":
                name = (parse_qs(parsed.query).get("name") or [""])[0]
                return self._rolehub_read("rolehub.detail", lambda hub: hub.detail(state, name))
            if path == "/api/roles/history":
                return self._rolehub_read("rolehub.history", lambda hub: {
                    "schema": hub.SCHEMA, "observed_at": __import__("time").time(),
                    "versions": hub.versions(state.workspace),
                    "store": "workspace .nexus/rolehub (runtime state, never committed)"})
            if path == "/api/roles/diff":
                return self._rolehub_read("rolehub.diff", lambda hub: hub.diff(state, parse_qs(parsed.query)))
            if path == "/api/roles":
                return self._send_json([r.to_dict() for r in state.config.roles.roles.values()])
            if path == "/api/agents":
                return self._send_json({
                    name: {
                        "provider": cfg.provider, "model": cfg.model,
                        "base_url": cfg.effective_base_url,
                        "tool_mode": cfg.tool_mode, "fallback": cfg.fallback,
                        "description": cfg.description,
                        "key_present": bool(cfg.resolved_api_key),
                        "key_source": cfg.key_source,
                    }
                    for name, cfg in state.config.agents.items()
                })
            if path == "/api/pipelines":
                return self._send_json({
                    name: {"description": p.description, "steps": p.steps}
                    for name, p in state.config.pipelines.items()
                })
            if path == "/api/tasks":
                with state.lock:
                    return self._send_json([s.snapshot() for s in state.sessions.values()])
            if path == "/api/monitoring":
                from .monitoring import collect_self_monitoring
                from .redaction import mask
                from .redaction import secrets as known_secrets
                from .studio import redact

                data = redact(collect_self_monitoring(state.workspace, studio=state.workbench.studio))
                cleaned = json.loads(mask(json.dumps(data, allow_nan=False), known_secrets(state.workspace)))
                return self._send_json(cleaned)
            if path == "/api/schedules":
                from .redaction import mask
                from .redaction import secrets as known_secrets
                from .scheduler import Scheduler, scheduler_summary
                from .studio import redact

                sched = Scheduler(state.workspace, studio=state.workbench.studio)
                data = redact({"summary": scheduler_summary(state.workspace), "items": [e.to_dict() for e in sched.list()]})
                cleaned = json.loads(mask(json.dumps(data, allow_nan=False), known_secrets(state.workspace)))
                return self._send_json(cleaned)
            if path == "/api/chief/history":
                from .chief import ChiefOfStaff
                from .redaction import mask
                from .redaction import secrets as known_secrets
                from .studio import redact

                query = parse_qs(parsed.query)
                status_filter = (query.get("status") or [None])[0]
                limit_val = int((query.get("limit") or ["50"])[0])
                data = redact({"items": ChiefOfStaff(state.workspace, studio=state.workbench.studio).history(status=status_filter, limit=limit_val)})
                cleaned = json.loads(mask(json.dumps(data, allow_nan=False), known_secrets(state.workspace)))
                return self._send_json(cleaned)
            if path == "/api/proactive/status":
                from .jarvis.proactive import ProactiveEngine
                from .redaction import mask
                from .redaction import secrets as known_secrets
                from .studio import redact

                data = redact(ProactiveEngine(state.workspace, studio=state.workbench.studio, server_state=state).status())
                cleaned = json.loads(mask(json.dumps(data, allow_nan=False), known_secrets(state.workspace)))
                return self._send_json(cleaned)
            if path == "/api/chief/missions":
                from .chief import chief_hud_summary

                return self._send_json(chief_hud_summary(state.workspace))
            if path.startswith("/api/chief/missions/"):
                from .chief import ChiefError, ChiefOfStaff, load_mission
                from .redaction import mask
                from .redaction import secrets as known_secrets
                from .studio import redact

                tail = unquote(path.removeprefix("/api/chief/missions/"))
                if tail.endswith("/replay"):
                    mission_id = tail.removesuffix("/replay")
                    query = parse_qs(parsed.query)
                    mode = (query.get("mode") or ["inspect"])[0]
                    if mode not in {"inspect", "why"}:
                        return self._send_json({"error": "HTTP replay supports read-only inspect or why"}, 400)
                    step_id = (query.get("step_id") or [None])[0]
                    raw_trace_step = (query.get("trace_step") or [None])[0]
                    trace_step = int(raw_trace_step) if raw_trace_step is not None and raw_trace_step != "" else None
                    try:
                        chief = ChiefOfStaff(state.workspace, studio=state.workbench.studio)
                        data = redact(chief.replay(mission_id, step_id=step_id, trace_step=trace_step, mode=mode))
                        cleaned = json.loads(mask(json.dumps(data, allow_nan=False), known_secrets(state.workspace)))
                        return self._send_json(cleaned)
                    except ChiefError:
                        return self._send_json({"error": f"unknown mission: {mission_id}"}, 404)

                mission_id = unquote(path.rsplit("/", 1)[-1])
                try:
                    data = redact(load_mission(state.workspace, mission_id).to_dict())
                    cleaned = json.loads(mask(json.dumps(data, allow_nan=False), known_secrets(state.workspace)))
                    return self._send_json(cleaned)
                except ChiefError:
                    return self._send_json({"error": f"unknown mission: {mission_id}"}, 404)
            if path.startswith("/api/events/"):
                return self._sse(path.rsplit("/", 1)[-1])
            if path.startswith("/api/task/"):
                session = state.get(path.rsplit("/", 1)[-1])
                if session is None:
                    return self._send_json({"error": "unknown task"}, 404)
                return self._send_json({**session.snapshot(), "pending": session.pending,
                                        "log": session.log[-200:]})
            if path == "/api/stats":
                return self._send_json(state.stats())
            if path == "/api/skills":
                return self._send_json(state.skills())
            if path.startswith("/api/skills/"):
                from . import skills as skilllib

                name = unquote(path.rsplit("/", 1)[-1])
                skill = skilllib.get(name, state.workspace)
                if skill is None:
                    return self._send_json({"error": f"unknown skill: {name}"}, 404)
                return self._send_json({**skill.to_dict(), "body": skill.body})
            if path == "/api/brain":
                try:
                    return self._send_json(state.brain())
                except Exception as exc:  # noqa: BLE001
                    return self._send_json({"error": f"brain unavailable: {exc}"}, 400)
            if path == "/api/recall":
                query = (parse_qs(parsed.query).get("q") or [""])[0]
                try:
                    return self._send_json(state.recall(query))
                except Exception as exc:  # noqa: BLE001
                    return self._send_json({"error": f"recall unavailable: {exc}"}, 400)
            if path.startswith("/api/memory/"):
                role = unquote(path.rsplit("/", 1)[-1])
                if role not in state.config.roles:
                    return self._send_json({"error": f"unknown role: {role}"}, 404)
                try:
                    return self._send_json(state.memory(role))
                except Exception as exc:  # noqa: BLE001
                    return self._send_json({"error": f"memory unavailable: {exc}"}, 400)

            # -- the GUI ------------------------------------------------- #
            if path in {"/dashboard", "/gui"}:
                return self._static("dashboard.html", STATIC_TYPES[".html"])
            if path.startswith("/dashboard/"):
                return self._static_asset(path[len("/dashboard/") :])
            if path.startswith("/gui/"):
                return self._static_asset(path[len("/gui/") :])

            # Old UI URLs are aliases, not additional dashboards.
            if path in {'/panel', '/console', '/hud', '/console/index.html', '/panel/index.html', '/panel/panel.html'}:
                return self._redirect('/dashboard')
            if path.startswith(('/panel/', '/console/')):
                return self._static_asset(path.split('/', 2)[-1])
            return self._send_json({"error": f"no route for {path}"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if not self._origin_ok():
                return self._send_json({'error': 'Host/origin refused'}, 403)
            if not self._authorized():
                from . import securitycenter

                securitycenter.note_denied(state, path, self._rolehub_client(),
                                           presented=self._credential_presented(parsed))
                return self._send_json({"error": "invalid or missing token"}, 401)
            if not self._device_allowed(path):
                return self._send_json({'error': 'Device token cannot access administration'}, 403)
            if (path.startswith('/api/presence/') or self._principal['kind'] == 'device') and not self.headers.get('X-Nexus-Token') and not self.headers.get('Origin'):
                return self._send_json({'error': 'Cookie controls require a same-origin Origin header'}, 403)
            if path.startswith('/api/presence/'):
                return self._presence_post(path.removeprefix('/api/presence/'))
            if path == '/api/session':
                from . import securitycenter
                from .gui_session import forget, issue
                payload = self._read_json()
                if payload.get('forget'):
                    try:
                        securitycenter.revoke_current(state.workspace, self.headers.get('Cookie', ''))
                    except (OSError, ValueError):
                        pass
                    from .hud.devices import Devices, cookie
                    if self._principal['kind'] == 'device':
                        Devices(state.workspace, state.token).revoke(self._principal['id'])
                    self._session_cookie = forget()
                    self._extra_cookies = [cookie()]
                elif self._principal['kind'] == 'device':
                    from .hud.devices import cookie, cookie_token
                    host = self.headers.get('Host', '')
                    if urlparse('http://' + host).hostname not in {'localhost', '127.0.0.1', '::1'} and os.environ.get('NEXUS_GUI_HTTPS_PROXY') != '1':
                        return self._send_json({'error': 'Device login requires loopback or a trusted HTTPS proxy'}, 403)
                    if type(payload.get('remember', True)) is not bool:
                        return self._send_json({'error': 'remember must be boolean'}, 400)
                    token = self.headers.get('X-Nexus-Token') or cookie_token(self.headers.get('Cookie', ''))[0]
                    self._session_cookie = cookie(token, max_age=self._principal['expires'] - time.time())
                    if payload.get('remember') is False:
                        import re
                        self._session_cookie = re.sub(r'; Max-Age=\d+', '', self._session_cookie)
                    self._extra_cookies = [forget()]
                elif state.token:
                    host = self.headers.get('Host', '')
                    if urlparse('http://' + host).hostname not in {'localhost', '127.0.0.1', '::1'} and os.environ.get('NEXUS_GUI_HTTPS_PROXY') != '1':
                        return self._send_json({'error': 'Remember login requires loopback; use an authenticated private proxy remotely'}, 403)
                    remember = payload.get('remember', True)
                    if type(remember) is not bool:
                        return self._send_json({'error': 'remember must be boolean'}, 400)
                    self._session_cookie = issue(state, host, remember=remember)
                    from .hud.devices import cookie
                    self._extra_cookies = [cookie()]  # Explicit owner re-login replaces device identity.
                return self._send_json({'authenticated': not payload.get('forget')})
            if path.startswith(('/api/vision/', '/api/assistant/vision/', '/api/computer/')) and not state.token:
                return self._send_json({'error': 'Capture/computer HTTP requires a configured authentication token'}, 401)
            if path.startswith('/api/vision/'):
                from .vision import api as vision_api
                operation = path.removeprefix('/api/vision/')
                client = self._rolehub_client()
                if operation != 'off' and not state.api_limiter.allow('vision-write:' + client):
                    return self._send_json({'error': 'Vision rate limit reached'}, 429)
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= vision_api.MAX_BODY or self.headers.get('Transfer-Encoding'):
                        return self._send_json({'error': 'Bounded vision JSON required'}, 413)
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError('Object required')
                    return self._send_json(vision_api.post(state, operation, payload, client))
                except (ValueError, OSError, TimeoutError, RecursionError):
                    return self._send_json({'error': 'Vision refused: check vision.yaml, dependencies, limits and fresh cloud review. No automatic retry.'}, 409)
            if path.startswith("/api/assistant/"):
                from .assistant import api as assistant_api
                client = self._rolehub_client()
                bucket = "assistant-write:" + client
                if not state.api_limiter.allow(bucket):
                    return self._send_json({"error": "Assistant rate limit reached"}, 429)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= assistant_api.MAX_BODY or self.headers.get("Transfer-Encoding"):
                        return self._send_json({"error": "Bounded assistant JSON body required"}, 413)
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("JSON object required")
                    result = assistant_api.post(state, path.removeprefix("/api/assistant/"), payload, client)
                    return self._send_json(result)
                except (ValueError, json.JSONDecodeError) as exc:
                    return self._send_json({"error": str(exc)[:500]}, 400)
                except Exception as exc:
                    return self._send_json({"error": state.workbench.studio.redact_text(str(exc))[:1000]}, 400)
            if path.startswith("/api/world/"):
                if path != "/api/world/refresh":
                    return self._send_json({"error": f"no route for {path}"}, 404)
                return self._world_action("refresh", self._read_json())
            from .gui_modules import module_path
            if module_path(path):
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 16000 or self.headers.get('Transfer-Encoding'):
                        return self._send_json({'error': 'Bounded JSON body required'}, 413)
                    payload = json.loads(self.rfile.read(length))
                except (ValueError, json.JSONDecodeError):
                    return self._send_json({'error': 'Invalid JSON'}, 400)
                status, data = state.gui.call('POST', path, parse_qs(parsed.query), payload)
                return self._send_json(data, status)
            if path.startswith('/api/computer/'):
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 550000 or self.headers.get('Transfer-Encoding'):
                        return self._send_json({'error': 'Bounded computer JSON body required'}, 413)
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError('Object required')
                except (ValueError, RecursionError):
                    return self._send_json({'error': 'Invalid computer JSON'}, 400)
            else:
                payload = self._read_json()

            if path in {"/api/roles/route", "/api/roles/export", "/api/roles/restore"}:
                return self._rolehub_action(path.rsplit("/", 1)[-1], payload)

            if path.startswith("/api/router/"):
                action = path.rsplit("/", 1)[-1]
                if action not in {"simulate", "rule", "weights", "enable", "lab", "restore"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)
                return self._router_action(action, payload)

            if path.startswith("/api/security/"):
                action = path.removeprefix("/api/security/")
                if action not in {"audit/verify", "session/revoke", "vault/set", "vault/delete",
                                  "two-factor/begin", "two-factor/confirm", "two-factor/disable",
                                  "policy/save", "injection/scan", "snapshot", "snapshot/restore"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)
                from . import securitycenter

                nonce = securitycenter.cookie_nonce(self.headers.get("Cookie", ""))
                return self._security_action(action, payload, nonce)

            if path.startswith("/api/channels/"):
                action = path.removeprefix("/api/channels/")
                if action not in {"policy/save", "delivery/save", "notify", "push", "ack", "telegram"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)
                return self._channels_action(action, payload)

            if path.startswith("/api/cost/"):
                action = path.removeprefix("/api/cost/")
                if action not in {"policy/save", "compare", "clear"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)
                return self._cost_action(action, payload)

            if path.startswith("/api/knowledge/"):
                action = path.removeprefix("/api/knowledge/")
                if action not in {"policy/save", "index", "collection/delete", "document/delete",
                                  "entry/add", "entry/remove", "cleanup",
                                  "personal/add", "personal/confirm", "personal/edit", "personal/delete"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)
                return self._knowledge_action(action, payload)

            if path.startswith("/api/workflows/"):
                action = path.removeprefix("/api/workflows/")
                if action not in {"validate", "version/save", "run", "cancel", "schedule/save",
                                  "schedule/toggle", "schedule/remove", "schedule/run", "trigger/preview"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)
                return self._workflow_action(action, payload)

            if path == "/api/proactive/action":
                if state.demo:
                    return self._send_json({"error": "Dashboard requires live mode. Restart Nexus without --demo and unset NEXUS_DEMO."}, 409)
                from .jarvis.proactive import ProactiveDisabledError, ProactiveEngine
                from .redaction import mask
                from .redaction import secrets as known_secrets
                from .studio import redact

                action = str(payload.get("action") or "").strip()
                engine = ProactiveEngine(state.workspace, studio=state.workbench.studio, server_state=state)
                try:
                    if action == "tick":
                        res = engine.tick_once()
                    elif action == "brief":
                        res = engine.run_morning_brief(
                            trigger="greeting" if payload.get("greeting") else "manual",
                            present=bool(payload.get("present", True)),
                            force=bool(payload.get("force", True)),
                        )
                    elif action == "focus":
                        res = engine.set_focus_mode(
                            bool(payload.get("enabled", True)),
                            minutes=int(payload.get("minutes", 60)),
                            reason=str(payload.get("reason") or ""),
                        )
                    elif action == "remind":
                        res = engine.create_from_voice(str(payload.get("text") or ""))
                    elif action == "stop_suggesting":
                        res = engine.learn_stop_suggesting(str(payload.get("topic") or ""))
                    elif action == "unmute":
                        res = engine.unmute_suggestion(str(payload.get("target") or ""))
                    elif action == "accept_suggestion":
                        res = engine.accept_suggestion(payload.get("id") or None)
                    elif action == "ack_toast":
                        res = engine.ack_hud_toast(str(payload.get("id") or ""))
                    else:
                        return self._send_json({"error": "Unsupported proactive action"}, 400)
                except (ProactiveDisabledError, ValueError, TypeError) as exc:
                    return self._send_json({"error": str(exc)[:500]}, 400)
                cleaned = json.loads(mask(json.dumps(redact(res), allow_nan=False), known_secrets(state.workspace)))
                return self._send_json(cleaned)

            if path == "/api/approvals/decide":
                return self._approvals_action(payload)

            if path.startswith("/api/computer/"):
                action = path.removeprefix("/api/computer/")
                if action not in {"settings", "file/preview", "file/write", "file/undo", "file/trash",
                                 "trash/restore", "browser/open", "action", "action/review", "panic", "resume"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)
                return self._computer_action(action, payload)

            if path.startswith("/api/workbench/"):
                try:
                    if state.demo and path.rsplit("/", 1)[-1] in {"retry", "execute-plan"}:
                        raise ValueError("Live mode required")
                    return self._send_json(state.workbench.post(path[len("/api/workbench/"):], payload))
                except Exception as exc:
                    return self._send_json({"error": str(exc)[:2000]}, 400)
            if path == "/api/task":
                if state.demo:
                    return self._send_json({"error": "Dashboard requires live mode. Restart Nexus without --demo and unset NEXUS_DEMO."}, 409)
                task = str(payload.get("task") or "").strip()
                if not task:
                    return self._send_json({"error": "task is required"}, 400)
                wanted_pipeline = payload.get("pipeline") or None
                if wanted_pipeline and wanted_pipeline not in state.config.pipelines:
                    return self._send_json(
                        {"error": f"unknown pipeline '{wanted_pipeline}' "
                                  f"(known: {', '.join(sorted(state.config.pipelines)) or 'none'})"}, 400)
                try:
                    options = state.workbench.prepare(payload)
                    local_model = payload.get("local_model") or None
                    if options["policy"]["local_only"]:
                        local_model = local_model or options["policy"]["default_local_model"]
                    state.workbench.configure(load_config(state.workspace), options, local_model)
                except ValueError as exc:
                    return self._send_json({"error": str(exc)}, 400)
                if local_model:
                    from .integrations import ollama_models

                    local = ollama_models()
                    if not isinstance(local_model, str) or local_model not in local["models"]:
                        return self._send_json({"error": local.get("error") or
                                                "Model is not installed in Ollama. Refresh the model list."}, 400)
                session = state.create(task, payload.get("role"), bool(payload.get("read_only")) or options["mode"] == "plan",
                                       pipeline=wanted_pipeline,
                                       auto_yes=bool(payload.get("auto_yes")) and options["mode"] != "plan",
                                       local_model=local_model, options=options)
                try:
                    state.start(session)
                except Exception as exc:
                    session.status = "error"
                    return self._send_json({"error": "Could not persist/start task: " + type(exc).__name__}, 500)
                return self._send_json({"task_id": session.task_id, "status": session.status}, 202)

            if path == "/api/skills":
                try:
                    return self._send_json(state.create_skill(payload), 201)
                except (ValueError, FileExistsError, OSError) as exc:
                    return self._send_json({"error": str(exc)}, 400)
            if path == "/api/schedules":
                if state.demo:
                    return self._send_json({"error": "Scheduling requires live mode."}, 409)
                from .scheduler import Scheduler

                try:
                    sched = Scheduler(state.workspace, studio=state.workbench.studio)
                    action_name = str(payload.get("action") or "add")
                    if action_name == "add":
                        entry = sched.add(
                            str(payload.get("expression") or ""),
                            task=str(payload.get("task") or ""),
                            timezone=payload.get("timezone") or None,
                            target=payload.get("target") or None,
                            priority=int(payload.get("priority", 5)),
                        )
                        return self._send_json(entry.to_dict(), 201)
                    if action_name in {"pause", "resume"}:
                        entry = sched.set_enabled(str(payload.get("id") or ""), action_name == "resume")
                        return self._send_json(entry.to_dict(), 200)
                    if action_name == "remove":
                        removed = sched.remove(str(payload.get("id") or ""))
                        return self._send_json({"removed": removed, "id": str(payload.get("id") or "")}, 200)
                    return self._send_json({"error": f"unsupported schedule action: {action_name}"}, 400)
                except (ValueError, TypeError) as exc:
                    return self._send_json({"error": str(exc)}, 400)
            if path == "/api/chief/missions":
                if state.demo:
                    return self._send_json({"error": "Chief-of-Staff requires live mode."}, 409)
                from .chief import ChiefDisabledError, ChiefError, ChiefOfStaff, save_mission

                try:
                    chief = ChiefOfStaff(state.workspace, studio=state.workbench.studio)
                    mission = chief.plan(
                        str(payload.get("goal") or ""),
                        pipeline=payload.get("pipeline") or None,
                        cost_cap_usd=payload.get("cost_cap_usd"),
                        concurrency=payload.get("concurrency"),
                        max_retries=payload.get("max_retries"),
                        critic_enabled=payload.get("critic_enabled"),
                        source="hud",
                    )
                    auto_ok = ChiefOfStaff.can_auto_start(mission, chief.chief_config(), chief.autonomy_policy())
                    if payload.get("start") and (auto_ok or str(payload.get("confirm") or "").strip().upper() == "GO"):
                        mission = chief.start_background(mission.id, auto_approve=True)
                        return self._send_json(mission.to_dict(), 202)
                    if not auto_ok:
                        mission.status = "awaiting_approval"
                        save_mission(state.workspace, mission)
                    return self._send_json(mission.to_dict(), 201)
                except ChiefDisabledError as exc:
                    return self._send_json({"error": str(exc)}, 403)
                except (ChiefError, ValueError, TypeError) as exc:
                    return self._send_json({"error": str(exc)}, 400)
            if path.startswith("/api/chief/missions/"):
                if state.demo:
                    return self._send_json({"error": "Chief-of-Staff requires live mode."}, 409)
                from .chief import ChiefDisabledError, ChiefError, ChiefOfStaff

                tail = path.removeprefix("/api/chief/missions/")
                if "/" not in tail:
                    return self._send_json({"error": f"no route for {path}"}, 404)
                mission_id, verb = tail.split("/", 1)
                if verb not in {"approve", "pause", "resume", "cancel"}:
                    return self._send_json({"error": f"no route for {path}"}, 404)
                try:
                    chief = ChiefOfStaff(state.workspace, studio=state.workbench.studio)
                    if verb == "approve":
                        mission = chief.approve(
                            mission_id,
                            approved=bool(payload.get("approved", True)),
                            confirm=str(payload.get("confirm") or ""),
                            background=bool(payload.get("background", True)),
                        )
                    elif verb == "pause":
                        mission = chief.pause(mission_id)
                    elif verb == "resume":
                        mission = chief.resume(mission_id, background=bool(payload.get("background", True)))
                    else:
                        mission = chief.cancel(mission_id)
                    return self._send_json(mission.to_dict(), 200)
                except ChiefDisabledError as exc:
                    return self._send_json({"error": str(exc)}, 403)
                except ChiefError as exc:
                    return self._send_json({"error": str(exc)}, 409)
            if path == "/api/recall":
                query = str(payload.get("query") or "")
                try:
                    return self._send_json(state.recall(query, int(payload.get("limit") or 8)))
                except Exception as exc:  # noqa: BLE001
                    return self._send_json({"error": f"recall unavailable: {exc}"}, 400)

            if path.startswith("/api/approve/"):
                status, body = state.decide_approval(
                    path.rsplit("/", 1)[-1], payload.get("approved"),
                    payload.get("scope") or "once", payload.get("approval_id"),
                    payload.get("reason") or "")
                return self._send_json(body, status)

            if path.startswith("/api/cancel/"):
                session = state.get(path.rsplit("/", 1)[-1])
                if session is None:
                    return self._send_json({"error": "unknown task"}, 404)
                with state.lock:
                    if session.task_id in state.task_queue:
                        state.workbench.post("queue", {"action": "cancel", "task_id": session.task_id})
                        return self._send_json({"ok": True, "status": "cancelled"})
                if session.status in {"done", "error", "cancelled"}:
                    return self._send_json({"error": "run is already finished or stopping"}, 409)
                with session.changed:
                    session.cancel.set()
                    if session.pending is not None:
                        session.approval_decision = ApprovalDecision(False, reason="cancelled")
                        session.approval_event.set()
                    session.status = "cancelled"
                return self._send_json({"ok": True, "status": session.status})

            return self._send_json({"error": f"no route for {path}"}, 404)

        # -- helpers ------------------------------------------------------- #
        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

        def _static_asset(self, name: str) -> None:
            """Serve an asset from nexus/panel/ with the right content type."""
            if name == 'operations.js':
                return self._send_bytes((Path(__file__).parent / 'hud/assets/hud.js').read_bytes(), 'text/javascript')
            clean = name.split("?", 1)[0].lstrip("/")
            suffix = Path(clean).suffix.lower()
            ctype = STATIC_TYPES.get(suffix, "application/octet-stream")
            return self._static(clean, ctype)

        def _static(self, name: str, content_type: str) -> None:
            target = (PANEL_DIR / name).resolve()
            try:
                target.relative_to(PANEL_DIR.resolve())
            except ValueError:
                return self._send_json({"error": "forbidden"}, 403)
            if target.suffix.lower() == '.html' and target.name != 'dashboard.html':
                return self._redirect('/dashboard')
            if not target.is_file():
                return self._send_json({"error": f"missing panel asset {name}"}, 404)
            self._send_bytes(target.read_bytes(), content_type)

        def _palette_index(self) -> None:
            """Light command-palette index: auth already checked, one limiter, one audit line."""
            from . import palette as palette_module
            from . import webapi

            client = str((getattr(self, "client_address", ("", 0)) or ("", 0))[0] or "local")
            bucket = "palette:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited", f"Too many palette requests; retry in {wait}s."), 429)
            try:
                index = palette_module.build(state.config)
                webapi.audit_api(state.workspace, "palette.index", actor="user", target="dashboard",
                                 result="ok", risk="low", detail={"roles": len(index["roles"])})
            except (OSError, ValueError):
                return self._send_json(webapi.failed("audit_unavailable",
                                                     "Palette index withheld: the audit trail is unavailable."), 503)
            return self._send_json(webapi.ok(index))

        def _settings_read(self) -> None:
            """Settings hub: one read-only snapshot, limited per client, sampled."""
            from . import settingshub, webapi

            client = self._rolehub_client()
            bucket = "settings:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many settings reads; retry in {wait}s."), 429)
            try:
                data = settingshub.summary(state)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "Settings withheld: the local preference store is unavailable."), 503)
            settingshub.audit_read(state, client)
            return self._send_json(webapi.ok(data))

        def _world_read(self, action: str, build) -> None:
            """Public-source reads: cached only, limited per client, sampled."""
            from . import webapi, worlddesk

            client = self._rolehub_client()
            bucket = "world:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many World reads; retry in {wait}s."), 429)
            try:
                data = build(worlddesk)
                worlddesk.audit_read(state, action.removeprefix("world."), client)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "World data withheld: the local cache is unavailable."), 503)
            return self._send_json(webapi.ok(data))

        def _world_action(self, action: str, payload) -> None:
            """Contacting a publisher is an explicit, typed, audited act."""
            from . import webapi, worlddesk

            client = self._rolehub_client()
            bucket = "world-write:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many World actions; retry in {wait}s."), 429)
            if not isinstance(payload, dict):
                return self._send_json(webapi.failed("invalid_request", "A JSON object is required"), 400)
            try:
                data = worlddesk.refresh(state, payload)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "Public sources unavailable; the cache was not changed."), 503)
            return self._send_json(webapi.ok(data))

        def _channels_read(self, action: str, build) -> None:
            """Alert, channel and preference reads: limited, sampled, redacted."""
            from . import channels, webapi

            client = self._rolehub_client()
            bucket = "channels:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many channel reads; retry in {wait}s."), 429)
            try:
                data = build(channels)
                channels.audit_read(state, action.removeprefix("channels."), client)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "Alert state withheld: a local store is unavailable."), 503)
            return self._send_json(webapi.ok(data))

        def _channels_action(self, action: str, payload) -> None:
            """Preferences need a revision; anything that changes delivery needs a token."""
            from . import channels, webapi

            client = self._rolehub_client()
            bucket = "channels-write:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many channel actions; retry in {wait}s."), 429)
            if not isinstance(payload, dict):
                return self._send_json(webapi.failed("invalid_request", "A JSON object is required"), 400)
            try:
                if action == "policy/save":
                    data = channels.save_policy(state, payload)
                elif action == "delivery/save":
                    data = channels.save_delivery(state, payload)
                elif action == "notify":
                    data = channels.pull(state, payload)
                elif action == "push":
                    data = channels.push(state, payload)
                elif action == "ack":
                    data = channels.acknowledge(state, payload)
                else:
                    data = channels.telegram_action(state, payload)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "The alert store is unavailable; nothing was changed."), 503)
            return self._send_json(webapi.ok(data))

        def _cost_read(self, action: str, build) -> None:
            """Cost reads: limited per client, sampled into the same audit chain."""
            from . import costcenter, webapi

            client = self._rolehub_client()
            bucket = "cost:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many cost reads; retry in {wait}s."), 429)
            try:
                data = build(costcenter)
                costcenter.audit_read(state, action.removeprefix("cost."), client)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "Cost data withheld: a local store is unavailable."), 503)
            return self._send_json(webapi.ok(data))

        def _cost_action(self, action: str, payload) -> None:
            """Every cost write needs its own typed token and is audited."""
            from . import costcenter, webapi

            client = self._rolehub_client()
            bucket = "cost-write:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many cost actions; retry in {wait}s."), 429)
            if not isinstance(payload, dict):
                return self._send_json(webapi.failed("invalid_request", "A JSON object is required"), 400)
            try:
                if action == "policy/save":
                    data = costcenter.save_policy(state, payload)
                elif action == "compare":
                    data = costcenter.compare(state, payload)
                else:
                    data = costcenter.cleanup(state, payload)
            except costcenter.BudgetExceeded as exc:
                return self._send_json(webapi.failed("budget_exceeded", str(exc)[:400]), 409)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "The cost store is unavailable; nothing was changed."), 503)
            return self._send_json(webapi.ok(data))

        def _knowledge_read(self, action: str, build) -> None:
            """Memory & knowledge reads: limited per client, sampled audit, redacted."""
            from . import knowledge, webapi

            client = self._rolehub_client()
            bucket = "memory:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many memory reads; retry in {wait}s."), 429)
            try:
                data = build(knowledge)
                knowledge.audit_read(state, action.removeprefix("knowledge."), client)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "Memory state withheld: a local store is unavailable."), 503)
            return self._send_json(webapi.ok(data))

        def _knowledge_action(self, action: str, payload) -> None:
            """Every knowledge write needs its own typed token and is audited."""
            from . import knowledge, webapi

            client = self._rolehub_client()
            bucket = "memory-write:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many knowledge actions; retry in {wait}s."), 429)
            if not isinstance(payload, dict):
                return self._send_json(webapi.failed("invalid_request", "A JSON object is required"), 400)
            try:
                if action == "policy/save":
                    data = knowledge.save_policy(state, payload)
                elif action == "index":
                    data = knowledge.index_collection(state, payload)
                elif action == "collection/delete":
                    data = knowledge.delete_collection(state, payload)
                elif action == "document/delete":
                    data = knowledge.remove_document(state, payload)
                elif action == "entry/add":
                    data = knowledge.add_entry(state, payload)
                elif action == "entry/remove":
                    data = knowledge.remove_entry(state, payload)
                elif action == "personal/add":
                    data = knowledge.personal_add(state, payload)
                elif action == "personal/confirm":
                    data = knowledge.personal_confirm(state, payload)
                elif action == "personal/edit":
                    data = knowledge.personal_edit(state, payload)
                elif action == "personal/delete":
                    data = knowledge.personal_delete(state, payload)
                else:
                    data = knowledge.cleanup(state, payload)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "The knowledge store is unavailable; nothing was changed."), 503)
            return self._send_json(webapi.ok(data))

        def _rolehub_client(self) -> str:
            return str((getattr(self, "client_address", ("", 0)) or ("", 0))[0] or "local")

        def _rolehub_read(self, action: str, build) -> None:
            """Read-only Role Hub surface: limited, audited (sampled), redacted."""
            from . import rolehub, webapi

            client = self._rolehub_client()
            bucket = "roles:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited", f"Too many role reads; retry in {wait}s."), 429)
            try:
                data = build(rolehub)
                rolehub.audit(state, client, action)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "audit_unavailable", "Role data withheld: the local store or audit trail is unavailable."), 503)
            return self._send_json(webapi.ok(data))

        def _rolehub_action(self, action: str, payload: dict) -> None:
            """route/export/restore: validated, limited, always audited."""
            from . import rolehub, webapi

            client = self._rolehub_client()
            bucket = ("roles-write:" if action == "restore" else "roles:") + client
            limit_ok = state.api_limiter.allow(bucket)
            if not limit_ok:
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited", f"Too many role actions; retry in {wait}s."), 429)
            try:
                if action == "route":
                    data = rolehub.route(state, payload)
                elif action == "export":
                    data = rolehub.export(state, payload)
                elif action == "restore":
                    data = rolehub.restore(state, payload)
                else:  # pragma: no cover - the route table above decides this
                    return self._send_json(webapi.failed("unknown_action", "Unknown role action"), 404)
                rolehub.audit(state, client, f"rolehub.{action}", every=0)
            except (ValueError, OSError) as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            return self._send_json(webapi.ok(data))

        def _router_read(self, action: str, build) -> None:
            """Read-only Router Studio surface: limited, audited (sampled), redacted."""
            from . import routerstudio, webapi

            client = self._rolehub_client()
            bucket = "router:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many router reads; retry in {wait}s."), 429)
            try:
                data = build(routerstudio)
                routerstudio.audit(state, client, action)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "audit_unavailable", "Router data withheld: the local store or audit trail is unavailable."), 503)
            return self._send_json(webapi.ok(data))

        def _router_action(self, action: str, payload: dict) -> None:
            """simulate/rule/weights/enable/lab/restore: validated, limited, audited."""
            from . import routerstudio, webapi

            client = self._rolehub_client()
            bucket = ("router:" if action == "simulate" else "router-write:") + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many router actions; retry in {wait}s."), 429)
            if not isinstance(payload, dict):
                return self._send_json(webapi.failed("invalid_request", "A JSON object is required"), 400)
            payload = {**payload, "_client": client}
            try:
                if action == "simulate":
                    data = routerstudio.simulate(state, payload)
                    routerstudio.audit(state, client, "routerstudio.simulate")
                elif action == "rule":
                    data = routerstudio.rule_action(state, payload)
                elif action == "weights":
                    data = routerstudio.save_weights(state, payload)
                elif action == "enable":
                    data = routerstudio.set_enabled(state, payload)
                elif action == "lab":
                    data = routerstudio.run_lab(state, payload)
                else:  # pragma: no cover - the route table above decides this
                    return self._send_json(webapi.failed("unknown_action", "Unknown router action"), 404)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except OSError:
                return self._send_json(webapi.failed(
                    "store_unavailable", "The router store is unavailable; nothing was saved."), 503)
            return self._send_json(webapi.ok(data))

        def _workflow_read(self, action: str, build) -> None:
            """Workflow Studio reads: limited per client, sampled audit, redacted."""
            from . import webapi, workflowstudio

            client = self._rolehub_client()
            bucket = "workflows:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many workflow reads; retry in {wait}s."), 429)
            # Opening the page is enough to revive a scheduler after a restart.
            workflowstudio.scheduler_for(state).ensure_started()
            try:
                data = build(workflowstudio)
                workflowstudio.audit_read(state, action, client)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "Workflow state withheld: a local store is unavailable."), 503)
            return self._send_json(webapi.ok(data))

        def _workflow_action(self, action: str, payload) -> None:
            """Every workflow mutation is validated, limited, audited and token-gated."""
            from . import webapi, workflowstudio

            client = self._rolehub_client()
            bucket = "workflows-write:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many workflow actions; retry in {wait}s."), 429)
            if not isinstance(payload, dict):
                return self._send_json(webapi.failed("invalid_request", "A JSON object is required"), 400)
            body = {**payload, "_client": client}
            try:
                if action == "version/save":
                    data = workflowstudio.save_version(state, body)
                elif action == "run":
                    data = workflowstudio.run_workflow(state, body)
                elif action == "cancel":
                    data = workflowstudio.cancel_run(state, body)
                elif action == "schedule/save":
                    data = workflowstudio.save_schedule(state, body)
                elif action == "schedule/toggle":
                    data = workflowstudio.toggle_schedule(state, body)
                elif action == "schedule/remove":
                    data = workflowstudio.remove_schedule(state, body)
                elif action == "schedule/run":
                    data = workflowstudio.run_schedule_now(state, body)
                elif action == "trigger/preview":
                    data = {"schema": workflowstudio.SCHEMA, "observed_at": time.time(),
                            "times": workflowstudio.preview_trigger(payload.get("trigger"))}
                else:
                    data = workflowstudio.validate_definition(state, body)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "The workflow store is unavailable; nothing was changed."), 503)
            workflowstudio.audit_action(state, action, client, data)
            workflowstudio.scheduler_for(state).ensure_started()
            return self._send_json(webapi.ok(data))

        def _security_read(self, action: str, build) -> None:
            """Security Center reads: limited per client, sampled audit, redacted."""
            from . import securitycenter, webapi

            client = self._rolehub_client()
            bucket = "security:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many security reads; retry in {wait}s."), 429)
            try:
                data = build(securitycenter)
                securitycenter.audit_read(state, action, client)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "Security state withheld: a local store is unavailable."), 503)
            return self._send_json(webapi.ok(data))

        def _security_action(self, action: str, payload, nonce: str = "") -> None:
            """Every security mutation needs its own token, and 2FA once enrolled."""
            from . import securitycenter, webapi

            client = self._rolehub_client()
            bucket = "security-write:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many security actions; retry in {wait}s."), 429)
            if not isinstance(payload, dict):
                return self._send_json(webapi.failed("invalid_request", "A JSON object is required"), 400)
            body = {**payload, "_client": client}
            try:
                if action == "audit/verify":
                    chain = securitycenter.verify_chain(state.workspace)
                    if not chain.get("ok"):
                        first = chain.get("first_bad") or {}
                        securitycenter.audit_action(
                            state, "security.audit.verify", client,
                            {"name": str(first.get("reason", "broken"))}, risk="medium", target="audit")
                    else:
                        securitycenter.audit_action(state, "security.audit.verify", client,
                                                    {"name": f"seq {chain.get('seq')}"}, risk="low", target="audit")
                    data = {"schema": securitycenter.SCHEMA, "observed_at": time.time(), "chain": chain}
                elif action == "session/revoke":
                    data = securitycenter.revoke_sessions(state, body, nonce)
                elif action == "vault/set":
                    data = securitycenter.set_secret(state, body)
                elif action == "vault/delete":
                    data = securitycenter.delete_secret(state, body)
                elif action == "two-factor/begin":
                    data = securitycenter.begin_enrolment(state, body)
                elif action == "two-factor/confirm":
                    data = securitycenter.confirm_enrolment(state, body)
                elif action == "two-factor/disable":
                    data = securitycenter.disable_two_factor(state, body)
                elif action == "policy/save":
                    data = securitycenter.save_policy(state, body)
                elif action == "snapshot":
                    data = securitycenter.take_snapshot(state, body)
                elif action == "snapshot/restore":
                    data = securitycenter.restore_snapshot(state, body)
                else:
                    data = securitycenter.scan_injection(state, body)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "The security store is unavailable; nothing was changed."), 503)
            return self._send_json(webapi.ok(data))

        def _approvals_read(self, action: str, build) -> None:
            """Approval Center reads: limited per client, sampled audit, redacted."""
            from . import approvalcenter, webapi

            client = self._rolehub_client()
            bucket = "approvals:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many approval reads; retry in {wait}s."), 429)
            try:
                data = build(approvalcenter)
                approvalcenter.audit_read(state, client, action)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "Approvals withheld: a local store is unavailable."), 503)
            return self._send_json(webapi.ok(data))

        def _approvals_action(self, payload) -> None:
            """Every decision is validated, limited, and audited on the spot."""
            from . import approvalcenter, webapi

            client = self._rolehub_client()
            bucket = "approvals-write:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many approval decisions; retry in {wait}s."), 429)
            if not isinstance(payload, dict):
                return self._send_json(webapi.failed("invalid_request", "A JSON object is required"), 400)
            try:
                data = approvalcenter.decide(state, {**payload, "_client": client})
            except approvalcenter.Stale as exc:
                return self._send_json(webapi.failed("stale_request", str(exc)[:400]), 409)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except OSError:
                return self._send_json(webapi.failed(
                    "store_unavailable", "The approval store is unavailable; nothing was changed."), 503)
            return self._send_json(webapi.ok(data))

        def _computer_read(self, action: str, build) -> None:
            """Computer Control reads: limited per client, audited (sampled), redacted."""
            from . import computerstudio, webapi
            if not state.token:
                return self._send_json({'error': 'Computer HTTP requires a configured authentication token'}, 401)

            client = self._rolehub_client()
            bucket = "computer:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many computer reads; retry in {wait}s."), 429)
            try:
                data = build(computerstudio)
                computerstudio.audit(state, client, action)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except (OSError, KeyError):
                return self._send_json(webapi.failed(
                    "store_unavailable", "Computer state withheld: the local store is unavailable."), 503)
            return self._send_json(webapi.ok(data))

        def _computer_action(self, action: str, payload: dict) -> None:
            """Every mutation is validated, limited, and audited on the spot."""
            from . import computerstudio, webapi
            if not state.token:
                return self._send_json({'error': 'Computer HTTP requires a configured authentication token'}, 401)

            client = self._rolehub_client()
            bucket = ("computer:" if action == "file/preview" else "computer-write:") + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many computer actions; retry in {wait}s."), 429)
            if not isinstance(payload, dict):
                return self._send_json(webapi.failed("invalid_request", "A JSON object is required"), 400)
            payload = {**payload, "_client": client}
            try:
                if action == 'action':
                    from .computer_sessions import start
                    data = start(state, payload)
                elif action == 'action/review':
                    from .computer_sessions import review
                    data = review(state, payload)
                elif action == "settings":
                    data = computerstudio.save_settings(state, payload)
                elif action == "file/preview":
                    data = computerstudio.preview(state, payload)
                elif action == "file/write":
                    data = computerstudio.write(state, payload)
                elif action == "file/undo":
                    data = computerstudio.undo(state, payload)
                elif action == "file/trash":
                    data = computerstudio.trash(state, payload)
                elif action == "trash/restore":
                    data = computerstudio.restore(state, payload)
                elif action == "browser/open":
                    data = computerstudio.browser_open(state, payload)
                elif action == "panic":
                    data = computerstudio.panic(state, payload)
                else:
                    data = computerstudio.resume(state, payload)
            except ValueError as exc:
                return self._send_json(webapi.failed("invalid_request", str(exc)[:400]), 400)
            except OSError:
                return self._send_json(webapi.failed(
                    "store_unavailable", "The computer store is unavailable; nothing was changed."), 503)
            return self._send_json(webapi.ok(data))

        def _presence_get(self, path):
            from .hud.presence_api import service
            if not state.token:
                return self._send_json({'error': 'Presence requires a configured serve token'}, 401)
            api = service(state)
            if path == 'events':
                return self._presence_stream(api)
            if not state.api_limiter.allow('presence-read:' + self._rolehub_client()):
                return self._send_json({'error': 'Presence rate limit'}, 429)
            try:
                if path == 'config':
                    result = api.config(self._principal)
                elif path == 'state':
                    result = api.snapshot(self._principal)
                elif path.startswith('world/'):
                    api.check(self._principal)
                    from .hud.presence_world import cached
                    result = cached(state, path.removeprefix('world/'), parse_qs(urlparse(self.path).query))
                elif path == 'push-key':
                    api.check(self._principal, 'push')
                    from .hud.presence_policy import require
                    from .hud.push import public_key
                    require(state.workspace, 'push_enabled')
                    result = {'public_key': public_key()}
                else:
                    return self._send_json({'error': 'Unknown presence route'}, 404)
                return self._send_json(result)
            except (ValueError, OSError, TypeError, KeyError, TimeoutError):
                return self._send_json({'error': 'Presence unavailable; inspect operator YAML, authentication and private state'}, 409)

        def _presence_post(self, path):
            if not state.token:
                return self._send_json({'error': 'Presence requires a configured serve token'}, 401)
            if not state.api_limiter.allow('presence-write:' + self._rolehub_client()):
                return self._send_json({'error': 'Presence rate limit'}, 429)
            from .hud.presence_api import service
            try:
                length = int(self.headers.get('Content-Length', '0'))
                limit = 3_000_000 if path == 'voice/transcribe' else 32_000
                if not 0 < length <= limit or self.headers.get('Transfer-Encoding'):
                    return self._send_json({'error': 'Bounded presence JSON required'}, 413)
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('Object required')
                if not self._authorized():  # Revalidate after a slow body upload.
                    return self._send_json({'error': 'Credential revoked'}, 401)
                return self._send_json(service(state).post(path, self._principal, payload))
            except (ValueError, OSError, TypeError, KeyError, TimeoutError, RecursionError) as exc:
                from .redaction import mask, secrets
                message = mask(str(exc), secrets(state.workspace) + [state.token])[:300]
                return self._send_json({'error': message or 'Presence control refused; no automatic retry'}, 409)

        def _presence_stream(self, api):
            from .mission import encode
            try:
                cfg = api.check(self._principal)
                cursor = max(0, int(self.headers.get('Last-Event-ID') or 0))
                with api.lock:
                    if api.streams >= cfg['max_streams']:
                        return self._send_json({'error': 'Presence stream limit'}, 429)
                    api.streams += 1
            except (ValueError, OSError):
                return self._send_json({'error': 'Presence stream disabled or unauthorized'}, 409)
            started = False
            try:
                first = api.snapshot(self._principal, cursor)
                self.send_response(200)
                for key, value in [('Content-Type', 'text/event-stream; charset=utf-8'),
                                   ('Cache-Control', 'no-store, no-transform'), ('Connection', 'close'),
                                   ('X-Accel-Buffering', 'no'), ('Referrer-Policy', 'no-referrer')]:
                    self.send_header(key, value)
                self.end_headers()
                started = True
                self.close_connection = True
                self.connection.settimeout(5)  # A stalled device cannot hold a slot indefinitely.
                while not api.closed.is_set():
                    if not self._authorized():
                        self.wfile.write(encode('presence.revoked', {'revoked': True}))
                        self.wfile.flush()
                        return
                    if _client_disconnected(self.connection):
                        return
                    payload = first if first is not None else api.snapshot(self._principal, cursor)
                    first = None
                    if not self._authorized():
                        return
                    cursor = payload['state']['revision']
                    self.wfile.write(encode('presence.state', payload, cursor))
                    self.wfile.flush()
                    if api.closed.wait(1):
                        return
            except (OSError, ValueError, TypeError, KeyError, TimeoutError):
                if not started:
                    self.close_connection = True
                    return self._send_json({'error': 'Presence snapshot unavailable; local inspection required'}, 409)
                return
            finally:
                with api.lock:
                    api.streams -= 1

        def _mission_client(self) -> str:
            return str((getattr(self, "client_address", ("", 0)) or ("", 0))[0] or "local")

        def _mission_summary(self) -> None:
            """Read-only projection of the local records; audited (sampled)."""
            from . import webapi

            client = self._mission_client()
            bucket = "mission:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many mission reads; retry in {wait}s."), 429)
            try:
                state.mission.ensure_health()
                summary = state.mission.summary()
                state.mission.audit(client)
            except (OSError, ValueError):
                return self._send_json(webapi.failed(
                    "audit_unavailable", "Mission summary withheld: the audit trail is unavailable."), 503)
            return self._send_json(webapi.ok(summary))

        def _mission_stream(self) -> None:
            """Global SSE feed: heartbeat, task.update, approval.new, metrics.tick, alert.new."""
            from . import mission as mission_module
            from . import webapi

            client = self._mission_client()
            bucket = "mission-stream:" + client
            if not state.api_limiter.allow(bucket):
                wait = state.api_limiter.retry_after(bucket)
                return self._send_json(webapi.failed("rate_limited",
                                                     f"Too many stream reconnects; retry in {wait}s."), 429)
            subscriber = state.mission.subscribe()
            if subscriber is None:
                return self._send_json(webapi.failed(
                    "too_many_streams", "Too many dashboard streams are already open."), 503)
            try:
                state.mission.audit(client, action="mission.stream")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    self.wfile.write(b"retry: 5000\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                last_write = time.monotonic()
                poll_interval = 0.25
                keepalive_interval = 5.0
                while not state.mission.stopped:
                    if not self._authorized():
                        return
                    if _client_disconnected(getattr(self, "connection", None)):
                        return
                    item = subscriber.next(timeout=poll_interval)
                    if item is None:
                        if state.mission.stopped or _client_disconnected(getattr(self, "connection", None)):
                            return
                        if time.monotonic() - last_write < keepalive_interval:
                            continue
                        frame = mission_module.keepalive()
                    else:
                        name, payload = item
                        if payload.get("shutdown"):
                            return
                        frame = mission_module.encode(name, payload, payload.get("seq"))
                    try:
                        self.wfile.write(frame)
                        self.wfile.flush()
                        last_write = time.monotonic()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
            finally:
                state.mission.unsubscribe(subscriber)

        def _sse(self, task_id: str) -> None:
            session = state.get(task_id)
            if session is None:
                return self._send_json({"error": "unknown task"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.close_connection = True
            self.send_header("Connection", "close")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def write(obj: dict[str, Any]) -> bool:
                try:
                    prefix = f"id: {obj['event_id']}\n" if "event_id" in obj else ""
                    self.wfile.write(f"{prefix}data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n".encode())
                    self.wfile.flush()
                    return True
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return False

            # Each subscriber follows its own cursor. A shared queue duplicated replay
            # and made simultaneous browser/sidebar subscribers steal each other's events.
            try:
                cursor = max(0, int(self.headers.get("Last-Event-ID") or 0))
            except ValueError:
                cursor = 0
            while True:
                if not self._authorized():
                    return
                with session.changed:
                    entries = [entry for entry in session.log if entry["event_id"] > cursor]
                    if not entries:
                        if session.log and session.log[-1]["type"] == "done":
                            return
                        session.changed.wait(timeout=1)
                        entries = [entry for entry in session.log if entry["event_id"] > cursor]
                if not entries:
                    if not write({"type": "ping", "status": session.status}):
                        return
                for entry in entries:
                    if not self._authorized() or not write(entry):
                        return
                    cursor = entry["event_id"]
                    if entry["type"] == "done":
                        return

    return Handler


def serve(*, host: str = "127.0.0.1", port: int = 8787, workspace: Path | None = None,
          token: str = "", auto_yes: bool = False, quiet: bool = False, demo: bool = False,
          open_panel: bool = False) -> int:
    ws = Path(workspace or Path.cwd()).resolve()
    state = ServerState(ws, token=token, auto_yes=auto_yes, quiet=quiet, demo=demo)
    import ipaddress
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == 'localhost'
    if not loopback and not state.token:
        raise ValueError('Exposing the GUI requires NEXUS_SERVE_TOKEN and exact NEXUS_ALLOWED_HOSTS')
    # Persisted workflow schedules are picked up here, so a restart resumes them
    # (missed windows are recorded as missed, never replayed).
    from .workflowstudio import scheduler_for

    if scheduler_for(state).start():
        print("  schedules  workflow scheduler running (persisted in .nexus/workflows)")
    if os.environ.get("NEXUS_ASSISTANT_SCHEDULER", "").lower() in {"1", "true", "yes", "on"}:
        from .assistant.service import service
        service(state).start_worker()
        print("  assistant  opt-in reminders/monitors/brief scheduler running")
    if host in {"0.0.0.0", "::"} and not state.token:
        raise ValueError("Exposing the GUI requires NEXUS_SERVE_TOKEN; configure exact NEXUS_ALLOWED_HOSTS too")
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    httpd.daemon_threads = True
    bound_host = "0.0.0.0" if host in {"0.0.0.0", "::"} else host
    if bound_host == "0.0.0.0" and not state.token:
        print(
            "warning: serving on all interfaces without a token. "
            "Set NEXUS_SERVE_TOKEN (or --token) before exposing this port."
        )
    print(f"nexus server {__version__} on http://{host}:{port}  (workspace {ws})")
    print(f"  dashboard  http://{host}:{port}/dashboard   <- the GUI")
    print("  operations, approvals, models, repositories and world/weather share this GUI")
    print(f"  health     http://{host}:{port}/health")
    print(f"  roles      {len(state.config.roles)} | agents {sorted(state.config.agents)}")
    if state.demo:
        print("  mode       DEMO configured; live dashboard task submission is disabled")
    elif not any(state.config.agents[name].resolved_api_key for name in state.config.agents):
        print("  api keys   NONE - run `nexus keys set gemini` (or deepseek / openrouter) first")
    if open_panel:
        try:
            import webbrowser

            webbrowser.open(f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}/dashboard")
        except Exception:  # noqa: BLE001
            pass
    import signal
    old_term = None
    if threading.current_thread() is threading.main_thread():
        def shutdown_signal(signum, frame):
            raise KeyboardInterrupt
        old_term = signal.signal(signal.SIGTERM, shutdown_signal)
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        state.mission.close()
        if getattr(state, "presence", None):
            state.presence.closed.set()
        if getattr(state, "assistant", None):
            state.assistant.stop_worker()
            state.assistant.player.stop()
        if getattr(state, "workflow_scheduler", None):
            state.workflow_scheduler.stop()
        if getattr(state, "telegram_runtime", None):
            state.telegram_runtime.stop(timeout=2)
        with state.lock:
            sessions = list(state.sessions.values())
        for session in sessions:
            session.cancel.set()
            session.approval_event.set()
        with state.workbench.studio.question_lock:
            for event in state.workbench.studio.questions.values():
                event.set()
        for process in state.workbench.studio.execution.list():
            state.workbench.studio.execution.stop(process["id"])
        httpd.server_close()
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
    return 0
