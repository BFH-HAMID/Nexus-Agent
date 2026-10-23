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
GET  /panel                      built-in web UI (also used by the extension)

Everything is stdlib-only so the server has no extra dependencies.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .agent import NexusAgent
from .config import load_config
from .tools import ApprovalDecision, preview_args
from .types import Event, EventType

PANEL_DIR = Path(__file__).resolve().parent / "panel"
APPROVAL_TIMEOUT = 600.0


@dataclass
class Session:
    task_id: str
    task: str
    role: str | None = None
    read_only: bool = False
    status: str = "queued"          # queued | running | awaiting_approval | done | error | cancelled
    events: "queue.Queue[dict[str, Any]]" = field(default_factory=queue.Queue)
    pending: dict[str, Any] | None = None
    approval_event: threading.Event = field(default_factory=threading.Event)
    approval_decision: ApprovalDecision | None = None
    result: dict[str, Any] | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    created: float = field(default_factory=time.time)
    agent: NexusAgent | None = None
    thread: threading.Thread | None = None
    log: list[dict[str, Any]] = field(default_factory=list)

    def push(self, event: dict[str, Any]) -> None:
        stamped = {"ts": time.time(), **event}
        self.log.append(stamped)
        if len(self.log) > 4000:
            self.log = self.log[-3000:]
        self.events.put(stamped)

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task": self.task,
            "role": self.role,
            "status": self.status,
            "created": self.created,
            "result": self.result,
            "events": len(self.log),
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
        self.lock = threading.Lock()
        self.config = load_config(self.workspace)

    def create(self, task: str, role: str | None, read_only: bool) -> Session:
        session = Session(task_id=uuid.uuid4().hex[:12], task=task, role=role, read_only=read_only)
        with self.lock:
            self.sessions[session.task_id] = session
        return session

    def get(self, task_id: str) -> Session | None:
        with self.lock:
            return self.sessions.get(task_id)

    def start(self, session: Session) -> None:
        thread = threading.Thread(target=self._run, args=(session,), daemon=True,
                                  name=f"nexus-{session.task_id}")
        session.thread = thread
        thread.start()

    # ------------------------------------------------------------------ #
    def _emit(self, session: Session):
        def handler(event: Event) -> None:
            session.push(event.to_json())
            if not self.quiet:
                kind = event.type.value
                if kind in {"role", "agent", "error"}:
                    print(f"[{session.task_id}] {kind}: {event.data}")
        return handler

    def _approval(self, session: Session):
        def callback(spec, args, risk, preview) -> ApprovalDecision:
            if session.cancel.is_set():
                return ApprovalDecision(False, reason="cancelled")
            session.status = "awaiting_approval"
            session.pending = {
                "tool": spec.name,
                "risk": risk.value,
                "args": args,
                "preview": preview,
            }
            session.approval_event.clear()
            session.approval_decision = None
            session.push({"type": "approval_request", **session.pending})
            got = session.approval_event.wait(APPROVAL_TIMEOUT)
            session.pending = None
            session.status = "running"
            if not got:
                return ApprovalDecision(False, reason="approval timed out")
            decision = session.approval_decision or ApprovalDecision(False, reason="no decision")
            session.push({"type": "approval_response", "tool": spec.name, "approved": decision.approved,
                          "scope": decision.scope})
            return decision
        return callback

    def _run(self, session: Session) -> None:
        try:
            agent = NexusAgent(
                load_config(self.workspace),
                workspace=self.workspace,
                emit=self._emit(session),
                approval_callback=None if self.auto_yes else self._approval(session),
                auto_yes=self.auto_yes,
            )
            if self.demo:
                from .demo import patch_agent_for_demo

                patch_agent_for_demo(agent)
                session.push({"type": "status", "message": "DEMO MODE - scripted run, no model called"})
            session.agent = agent
            session.status = "running"
            session.push({"type": "status", "message": f"starting run in {self.workspace}"})
            result = agent.run(
                session.task,
                role=session.role or None,
                read_only=session.read_only,
            )
            session.result = {
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
            }
            session.status = "cancelled" if session.cancel.is_set() else ("done" if result.ok else "error")
        except Exception as exc:  # noqa: BLE001
            session.status = "error"
            session.result = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
            session.push({"type": "error", "message": str(exc)})
        finally:
            session.push({"type": "done", "status": session.status, "result": session.result})
            session.events.put(None)  # sentinel: close SSE streams


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
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if not self._authorized():
                return self._send_json({"error": "invalid or missing token"}, 401)

            if path in {"/", "/health"}:
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
            if path == "/api/roles":
                return self._send_json([r.to_dict() for r in state.config.roles.roles.values()])
            if path == "/api/agents":
                return self._send_json({
                    name: {
                        "provider": cfg.provider, "model": cfg.model, "base_url": cfg.base_url,
                        "tool_mode": cfg.tool_mode, "fallback": cfg.fallback, "description": cfg.description,
                        "key_present": bool(cfg.resolved_api_key),
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
            if path in {"/panel", "/panel/"}:
                return self._static("panel.html", "text/html; charset=utf-8")
            if path.startswith("/panel/"):
                name = path[len("/panel/") :]
                ctype = "text/css" if name.endswith(".css") else "application/javascript"
                return self._static(name, ctype)
            return self._send_json({"error": f"no route for {path}"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if not self._authorized():
                return self._send_json({"error": "invalid or missing token"}, 401)
            payload = self._read_json()

            if path == "/api/task":
                task = str(payload.get("task") or "").strip()
                if not task:
                    return self._send_json({"error": "task is required"}, 400)
                session = state.create(task, payload.get("role"), bool(payload.get("read_only")))
                state.start(session)
                return self._send_json({"task_id": session.task_id, "status": session.status}, 202)

            if path.startswith("/api/approve/"):
                session = state.get(path.rsplit("/", 1)[-1])
                if session is None or session.pending is None:
                    return self._send_json({"error": "no pending approval"}, 409)
                session.approval_decision = ApprovalDecision(
                    approved=bool(payload.get("approved")),
                    scope=str(payload.get("scope") or "once"),
                    reason=str(payload.get("reason") or ""),
                )
                session.approval_event.set()
                return self._send_json({"ok": True})

            if path.startswith("/api/cancel/"):
                session = state.get(path.rsplit("/", 1)[-1])
                if session is None:
                    return self._send_json({"error": "unknown task"}, 404)
                session.cancel.set()
                if session.pending is not None:
                    session.approval_decision = ApprovalDecision(False, reason="cancelled")
                    session.approval_event.set()
                session.status = "cancelled"
                return self._send_json({"ok": True, "status": session.status})

            return self._send_json({"error": f"no route for {path}"}, 404)

        # -- helpers ------------------------------------------------------- #
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
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def write(obj: dict[str, Any]) -> bool:
                try:
                    self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    return True
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return False

            # replay what already happened, then follow live
            for entry in list(session.log):
                if not write(entry):
                    return
            if session.status in {"done", "error", "cancelled"}:
                write({"type": "done", "status": session.status, "result": session.result})
                return
            while True:
                try:
                    item = session.events.get(timeout=15)
                except queue.Empty:
                    if not write({"type": "ping", "status": session.status}):
                        return
                    continue
                if item is None:
                    return
                if not write(item):
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
    print(f"  panel      http://{host}:{port}/panel")
    print(f"  health     http://{host}:{port}/health")
    print(f"  roles      {len(state.config.roles)} | agents {sorted(state.config.agents)}")
    if state.demo:
        print("  mode       DEMO (scripted runs - no API key or local model needed)")
    if open_panel:
        try:
            import webbrowser

            webbrowser.open(f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}/panel")
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
    return 0
