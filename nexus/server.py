"""Local HTTP + SSE server.

This is the bridge for Phase 5 (VS Code sidebar) and for the zero-install
browser panel: both load ``/panel`` and talk to the same tiny JSON/SSE API.

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
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from . import __version__
from .agent import NexusAgent
from .config import load_config
from .tools import ApprovalDecision
from .types import Event, EventType

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

    def push(self, event: dict[str, Any]) -> None:
        with self.changed:
            self.sequence += 1
            stamped = {"ts": time.time(), **event, "event_id": self.sequence}
            self.log.append(stamped)
            if len(self.log) > 4000:
                self.log = self.log[-3000:]
            self.changed.notify_all()

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
        self.token = token or os.environ.get("NEXUS_SERVE_TOKEN", "")
        self.auto_yes = auto_yes
        self.quiet = quiet
        self.demo = demo or os.environ.get("NEXUS_DEMO", "").lower() in {"1", "true", "yes"}
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        self.config = load_config(self.workspace)
        self.started = time.time()
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
                }
                session.push({"type": "approval_request", **session.pending})
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
                )
            session.result = {
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
            try:
                self.workbench.save_run(session)
            except Exception:
                pass  # Completion must still reach SSE if history storage is unavailable.


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #


def make_handler(state: ServerState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"nexus/{__version__}"

        # -- plumbing ------------------------------------------------------ #
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            if not state.quiet:
                super().log_message(fmt, *args)

        def handle_one_request(self) -> None:
            """Swallow the disconnect noise an SSE client leaves behind."""
            try:
                super().handle_one_request()
            except (ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        def handle_error(self, *args: Any) -> None:  # pragma: no cover
            pass

        def _authorized(self) -> bool:
            if not state.token:
                return True
            provided = self.headers.get("X-Nexus-Token", "")
            if not provided:
                query = parse_qs(urlparse(self.path).query)
                provided = (query.get("token") or [""])[0]
            return provided == state.token

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
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
            self.send_header("Access-Control-Allow-Origin", "*")
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
                path == "/"
                or path in {"/dashboard", "/gui", "/panel", "/console"}
                or path.startswith(("/dashboard/", "/gui/", "/panel/", "/console/"))
            )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if not self._authorized() and not self._public_asset(path):
                return self._send_json({"error": "invalid or missing token"}, 401)

            if path == "/":
                return self._redirect("/dashboard" + ("?" + parsed.query if parsed.query else ""))

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

            # -- the cinematic simulation console (single self-contained file) -- #
            if path in {"/console", "/console/"}:
                return self._static("index.html", STATIC_TYPES[".html"])
            if path.startswith("/console/"):
                return self._static_asset(path[len("/console/") :])

            # -- the compact panel (VS Code webview) --------------------- #
            if path in {"/panel", "/panel/"}:
                return self._static("panel.html", "text/html; charset=utf-8")
            if path.startswith("/panel/"):
                return self._static_asset(path[len("/panel/") :])
            return self._send_json({"error": f"no route for {path}"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if not self._authorized():
                return self._send_json({"error": "invalid or missing token"}, 401)
            payload = self._read_json()

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
            if path == "/api/recall":
                query = str(payload.get("query") or "")
                try:
                    return self._send_json(state.recall(query, int(payload.get("limit") or 8)))
                except Exception as exc:  # noqa: BLE001
                    return self._send_json({"error": f"recall unavailable: {exc}"}, 400)

            if path.startswith("/api/approve/"):
                session = state.get(path.rsplit("/", 1)[-1])
                if session is None or session.pending is None:
                    return self._send_json({"error": "no pending approval"}, 409)
                scope = payload.get("scope") or "once"
                if not isinstance(payload.get("approved"), bool) or scope not in {"once", "session"}:
                    return self._send_json({"error": "approved must be boolean; scope must be once or session"}, 400)
                with session.changed:
                    if (session.pending is None or session.approval_event.is_set()
                            or session.cancel.is_set()
                            or (payload.get("approval_id") and
                                payload["approval_id"] != session.pending["approval_id"])):
                        return self._send_json({"error": "approval is no longer pending; refresh task status"}, 409)
                    diff = session.pending.get("proposal", {})
                    if payload["approved"] and diff.get("available"):
                        from .workbench import proposal

                        try:
                            current = proposal(state.workspace, session.pending["tool"], session.pending["args"])
                        except (ValueError, OSError, UnicodeError) as exc:
                            return self._send_json({"error": "Cannot revalidate preview: " + str(exc)}, 409)
                        if current.get("before_hash") != diff.get("before_hash"):
                            return self._send_json({"error": "File changed since preview; deny and request a fresh edit"}, 409)
                    session.approval_decision = ApprovalDecision(
                        approved=payload["approved"], scope=scope,
                        reason=str(payload.get("reason") or ""),
                    )
                    session.approval_event.set()
                return self._send_json({"ok": True})

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
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

        def _static_asset(self, name: str) -> None:
            """Serve an asset from nexus/panel/ with the right content type."""
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
            if not target.is_file():
                return self._send_json({"error": f"missing panel asset {name}"}, 404)
            self._send_bytes(target.read_bytes(), content_type)

        def _sse(self, task_id: str) -> None:
            session = state.get(task_id)
            if session is None:
                return self._send_json({"error": "unknown task"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.close_connection = True
            self.send_header("Connection", "close")
            self.send_header("Access-Control-Allow-Origin", "*")
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
                with session.changed:
                    entries = [entry for entry in session.log if entry["event_id"] > cursor]
                    if not entries:
                        if session.log and session.log[-1]["type"] == "done":
                            return
                        session.changed.wait(timeout=15)
                        entries = [entry for entry in session.log if entry["event_id"] > cursor]
                if not entries:
                    if not write({"type": "ping", "status": session.status}):
                        return
                for entry in entries:
                    if not write(entry):
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
    print(f"  console    http://{host}:{port}/console     <- read-only registry view")
    print(f"  panel      http://{host}:{port}/panel       (compact / VS Code)")
    print(f"  health     http://{host}:{port}/health")
    print(f"  roles      {len(state.config.roles)} | agents {sorted(state.config.agents)}")
    if state.demo:
        print("  mode       DEMO configured; live dashboard task submission is disabled")
    elif not any(state.config.agents[name].resolved_api_key for name in state.config.agents):
        print("  api keys   NONE - run `nexus keys set gemini` (or deepseek / openrouter) first")
    if open_panel:
        try:
            import webbrowser

            query = "?" + urlencode({"token": state.token}) if state.token else ""
            webbrowser.open(f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}/dashboard{query}")
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
