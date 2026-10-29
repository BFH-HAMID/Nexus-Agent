"""Dashboard services: persisted preferences/history and explicit, bounded actions.

No network probes or mutations run on page load. All observations carry source/time.
The local-only switch controls model routing, not an OS network sandbox.
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import AgentConfig, load_config, nexus_home
from .integrations import ollama_models, ollama_url, use_local_model

DEFAULTS = {"local_only": False, "default_local_model": "", "role_models": {},
            "write_paths": [], "language": "en", "layout": {}, "pricing": {}}


def observation(data: Any, source: str) -> dict:
    return {"data": data, "source": source, "checked_at": time.time()}


def local_endpoint(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host in {"localhost", "host.docker.internal"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False


def safe_path(workspace: Path, name: str, *, secret_check: bool = True) -> Path:
    if not name or "\x00" in name:
        raise ValueError("A workspace-relative path is required")
    root = workspace.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Path escapes the workspace")
    parts = tuple(part.lower() for part in path.relative_to(root).parts)
    if secret_check and any(p in {".git", ".nexus", ".env", "keys.json", ".venv", "node_modules", ".ssh", ".aws", ".npmrc", ".netrc", "credentials"}
                            or (p.startswith(".env.") and p != ".env.example") or p.endswith((".pem", ".key")) for p in parts):
        raise ValueError("Private/runtime paths are not exposed in the workbench")
    return path


def proposal(workspace: Path, tool: str, args: dict) -> dict:
    """Match write/edit semantics without executing them. Never previews shell scripts."""
    if tool not in {"write_file", "edit_file"}:
        return {"available": False, "reason": "No reliable file diff for this tool; review its full arguments."}
    path = safe_path(workspace, str(args.get("path") or ""))
    if path.exists() and (not path.is_file() or path.stat().st_size > 500_000):
        raise ValueError("Preview requires a text file smaller than 500 KB")
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    if tool == "write_file":
        after = (before if args.get("append") else "") + str(args.get("content", ""))
    else:
        from .tools import _fuzzy_find

        old = str(args.get("old_text") or "")
        if not old:
            raise ValueError("old_text is required")
        if old not in before:
            old = _fuzzy_find(before, old) or ""
        if not old or (before.count(old) > 1 and not args.get("replace_all")):
            raise ValueError("Edit is missing or ambiguous; no reliable preview")
        after = before.replace(old, str(args.get("new_text", "")), -1 if args.get("replace_all") else 1)
    name = path.relative_to(workspace.resolve()).as_posix()
    diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                       fromfile="a/" + name, tofile="b/" + name))
    return {"available": True, "path": name, "before": before[:100_000], "after": after[:100_000],
            "diff": diff[:160_000], "truncated": max(len(before), len(after), len(diff)) > 100_000,
            "before_hash": hashlib.sha256(before.encode()).hexdigest()}


class Workbench:
    def __init__(self, state):
        self.state = state
        self.workspace = state.workspace
        self.key = hashlib.sha256(str(self.workspace).encode()).hexdigest()
        self.path = nexus_home() / "dashboard.db"
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.previews: dict[str, dict] = {}
        from .studio import Studio

        self.studio = Studio(self)
        from .repository import Repositories
        self.repositories = Repositories(self)

    @contextmanager
    def connect(self):
        from filelock import FileLock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, FileLock(str(self.path) + ".lock", timeout=15):
            with self._connect_unlocked() as conn:
                yield conn

    @contextmanager
    def _connect_unlocked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        from .studio_crypto import connection, enabled
        if enabled(self.path):
            with connection(self.path, locked=True) as conn:
                yield conn
            return
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        conn.execute("CREATE TABLE IF NOT EXISTS preferences (workspace TEXT PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, workspace TEXT, created REAL, body TEXT)")
        conn.commit()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def settings(self) -> dict:
        with self.lock, self.connect() as conn:
            row = conn.execute("SELECT body FROM preferences WHERE workspace=?", (self.key,)).fetchone()
        return {**json.loads(json.dumps(DEFAULTS)), **(json.loads(row[0]) if row else {})}

    def save_settings(self, payload: dict) -> dict:
        settings = self.settings()
        for key in DEFAULTS:
            if key in payload:
                settings[key] = payload[key]
        if type(settings["local_only"]) is not bool or settings["language"] not in {"en", "bn"}:
            raise ValueError("Invalid local-only policy or language")
        if not isinstance(settings["default_local_model"], str) or len(settings["default_local_model"]) > 200:
            raise ValueError("Invalid default local model")
        pricing = settings["pricing"]
        if not isinstance(pricing, dict):
            raise ValueError("pricing must be a model-ID to rate object")
        for model, rate in pricing.items():
            if not isinstance(model, str) or not isinstance(rate, dict) or not str(rate.get("source", "")).strip():
                raise ValueError("Each model price needs an explicit user-provided source")
            for field in ("input_per_million", "output_per_million"):
                value = rate.get(field)
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                    raise ValueError("Prices must be finite nonnegative numbers (USD per million tokens)")
        mapping = settings["role_models"]
        if not isinstance(mapping, dict) or len(mapping) > 2000:
            raise ValueError("role_models must be an object")
        for role, model in mapping.items():
            if role not in self.state.config.roles or not isinstance(model, str):
                raise ValueError("Unknown role or invalid model")
            if not model.startswith("ollama:") and model not in self.state.config.agents:
                raise ValueError("Choose a configured agent or ollama:<model>")
        paths = settings["write_paths"]
        if not isinstance(paths, list) or len(paths) > 100:
            raise ValueError("write_paths must be a list of relative folders")
        for path in paths:
            if not isinstance(path, str):
                raise ValueError("Invalid write path")
            safe_path(self.workspace, path)
        if not isinstance(settings["layout"], dict) or len(json.dumps(settings)) > 100_000:
            raise ValueError("Settings too large or invalid layout")
        with self.lock, self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO preferences VALUES (?,?)", (self.key, json.dumps(settings)))
        return settings

    def save_run(self, session) -> None:
        body = {**session.snapshot(), "log": session.log[-4000:], "local_model": session.local_model,
                "read_only": session.read_only, "options": session.options,
                "fingerprint": self.fingerprint(allow_unavailable=True)}
        with self.lock, self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
                         (session.task_id, self.key, session.created, json.dumps(body, default=str)))

    def history(self, query: str = "", task_id: str = "") -> list[dict]:
        with self.lock, self.connect() as conn:
            rows = conn.execute("SELECT body FROM runs WHERE workspace=? ORDER BY created DESC LIMIT 300",
                                (self.key,)).fetchall()
        items = [json.loads(row[0]) for row in rows]
        items.extend(self.studio.items("archived-run"))
        items = sorted(items, key=lambda item: item.get("created", 0), reverse=True)[:300]
        return [item for item in items if (not task_id or item["task_id"] == task_id)
                and query.lower() in (item["task"] + json.dumps(item.get("result"))).lower()]

    def command(self, args: list[str], timeout: float = 15) -> str:
        try:
            result = subprocess.run(args, cwd=self.workspace, capture_output=True, text=True,
                                    timeout=timeout, encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"{args[0]} unavailable or timed out ({type(exc).__name__})") from exc
        if result.returncode:
            raise ValueError((result.stderr or result.stdout or f"{args[0]} failed")[:2000])
        if len(result.stdout) > 500_000:
            raise ValueError("Output exceeds the 500 KB safety limit; narrow the workspace/log first")
        return result.stdout

    def git_status(self) -> dict:
        branch = self.command(["git", "branch", "--show-current"]).strip()
        head = self.command(["git", "rev-parse", "HEAD"]).strip()
        status = self.command(["git", "status", "--porcelain=v1", "-uall"])
        try:
            ahead_behind = self.command(["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"]).strip()
        except ValueError:
            ahead_behind = "Unavailable: no upstream or upstream history missing"
        return observation({"branch": branch, "head": head, "status": status,
                            "ahead_behind": ahead_behind, "note": "Local refs only; no background fetch."}, "git (local)")

    def fingerprint(self, allow_unavailable: bool = False) -> str:
        try:
            content = self.command(["git", "branch", "--show-current"]) + self.command(["git", "rev-parse", "HEAD"]) + self.command(["git", "diff", "HEAD", "--no-ext-diff"])
            paths = self.command(["git", "ls-files", "--others", "--exclude-standard", "-z"]).split("\0")
            for name in paths:
                if not name:
                    continue
                path = safe_path(self.workspace, name)
                if path.stat().st_size > 10_000_000:
                    raise ValueError("Untracked file too large for a safe retry fingerprint")
                content += name + hashlib.sha256(path.read_bytes()).hexdigest()
            return hashlib.sha256(content.encode()).hexdigest()
        except (ValueError, OSError):
            if allow_unavailable:
                return ""
            raise ValueError("Cannot safely verify workspace state; review changes and start a new task") from None

    def files(self) -> dict:
        names = set(self.command(["git", "diff", "--name-only", "HEAD", "-z"]).split("\0"))
        names.update(self.command(["git", "ls-files", "--others", "--exclude-standard", "-z"]).split("\0"))
        safe = []
        for name in sorted(names - {""}):
            try:
                safe_path(self.workspace, name)
                safe.append(name)
            except ValueError:
                continue
        return observation(safe, "git diff HEAD + untracked workspace files")

    def file(self, name: str) -> dict:
        path = safe_path(self.workspace, name)
        if path.exists() and (not path.is_file() or path.stat().st_size > 500_000):
            raise ValueError("Only text files smaller than 500 KB can be viewed")
        after = path.read_text(encoding="utf-8") if path.exists() else ""
        rel = path.relative_to(self.workspace).as_posix()
        try:
            before = self.command(["git", "show", "HEAD:" + rel])
        except ValueError:
            before = ""
        return observation({"path": rel, "content": after, "diff": "".join(difflib.unified_diff(
            before.splitlines(True), after.splitlines(True), fromfile="HEAD/" + rel, tofile=rel))}, "workspace + git HEAD")

    def configure(self, config, options: dict, local_model: str | None):
        settings = options["policy"]
        selected = local_model or settings["default_local_model"]
        if settings["local_only"]:
            if not selected or not local_endpoint(ollama_url()):
                raise ValueError("Local-only requires a selected model on a loopback/private Ollama address")
            use_local_model(config, selected)
            config.agents = {"selected-ollama": config.agents["selected-ollama"]}
        elif local_model:
            use_local_model(config, local_model)
        for role, target in ({} if options.get("manual_local", bool(local_model)) else settings["role_models"]).items():
            if target.startswith("ollama:"):
                model = target[7:]
                name = "role-local-" + hashlib.sha256(model.encode()).hexdigest()[:12]
                config.agents[name] = AgentConfig(name=name, provider="openai-compatible", model=model,
                    base_url=ollama_url() + "/v1", api_key="ollama", tool_mode="text", timeout=420)
            else:
                name = target
            if name not in config.agents:
                raise ValueError(f"Role {role} conflicts with local-only policy")
            config.roles.roles[role].agent = name
        override = options.get('role_agent_override')
        if override:
            name, role = override['agent'], override['role']
            if name not in config.agents or role not in config.roles or options.get('manual_local'):
                raise ValueError('Workflow model override conflicts with available agents/manual local selection')
            config.roles.get(role).agent = name
        if settings["local_only"]:
            for agent in config.agents.values():
                agent.fallback = None
        return config

    def write_policy(self, settings: dict):
        def check(spec, args):
            if spec.is_readonly_call(args):
                return ""
            paths = settings["write_paths"]
            if not paths or spec.name in {"finish", "remember", "skill_view"}:
                return ""
            if spec.name not in {"write_file", "edit_file"}:
                return "Restricted write folders: shell/git/other write tools cannot be safely confined; refused"
            try:
                path = safe_path(self.workspace, str(args.get("path") or ""))
                if not any(path.is_relative_to(safe_path(self.workspace, folder)) for folder in paths):
                    return "File is outside the allowed write folders"
            except ValueError as exc:
                return str(exc)
            return ""
        return check

    def health(self, name: str) -> dict:
        from .providers import ProviderRegistry

        config = load_config(self.workspace)
        if name not in config.agents:
            raise ValueError("Unknown agent")
        cfg = config.agents[name]
        if self.settings()["local_only"] and not local_endpoint(cfg.effective_base_url or ""):
            raise ValueError("Local-only policy blocks hosted model probes")
        # Bound reachability checks; /models does not prove an inference quota is available.
        cfg.timeout = 8
        started = time.monotonic()
        ok, message = ProviderRegistry(config.agents).get(name).health()
        return observation({"agent": name, "reachable": ok, "message": message,
                            "latency_ms": round((time.monotonic() - started) * 1000),
                            "scope": "Model-list endpoint, not an inference/quota guarantee"}, "provider /models probe")

    def ollama(self) -> dict:
        result = ollama_models()
        if not result["connected"]:
            return observation(result, "Ollama /api/tags")
        data = httpx.get(ollama_url() + "/api/tags", timeout=8).raise_for_status().json()
        loaded = httpx.get(ollama_url() + "/api/ps", timeout=8).raise_for_status().json()
        return observation({"installed": data.get("models", []), "loaded": loaded.get("models", [])}, "Ollama tags + ps")

    def capability(self, model: str, smoke: bool = False) -> dict:
        if self.settings()["local_only"] and not local_endpoint(ollama_url()):
            raise ValueError("Local-only blocks inference on a public Ollama endpoint")
        if model not in ollama_models()["models"]:
            raise ValueError("Choose an installed Ollama model")
        response = httpx.post(ollama_url() + "/api/show", json={"model": model}, timeout=10).raise_for_status().json()
        result = {"model": model, "capabilities": response.get("capabilities"),
                  "native_tools": ("tools" in response["capabilities"]) if isinstance(response.get("capabilities"), list) else None, "text_tools": "instruction-following not yet tested"}
        if smoke:
            started = time.monotonic()
            answer = httpx.post(ollama_url() + "/api/chat", timeout=120, json={"model": model, "stream": False,
                "messages": [{"role": "user", "content": 'Reply only with {"tool":"echo","value":"nexus"}. This is a harmless formatting test.'}],
                "options": {"num_predict": 80, "temperature": 0}}).raise_for_status().json()
            text = answer.get("message", {}).get("content", "")
            try:
                passed = json.loads(text) == {"tool": "echo", "value": "nexus"}
            except ValueError:
                passed = False
            result.update({"text_test_passed": passed, "response": text[:1000],
                           "duration_s": round(time.monotonic() - started, 3)})
            count, duration = answer.get("eval_count"), answer.get("eval_duration")
            result["tokens_per_second"] = round(count / (duration / 1e9), 2) if count and duration else None
        return observation(result, "Ollama show" + (" + explicit small inference test" if smoke else ""))

    def metrics(self) -> dict:
        data = {"cpu_percent": None, "cpu_cores": None, "ram": None, "swap": None,
                "disk": None, "nexus_process_rss": None, "uptime_s": None,
                "gpu": None, "gpu_devices": [], "gpu_error": "NVIDIA monitoring unavailable"}
        try:
            import psutil
            data.update(cpu_percent=psutil.cpu_percent(interval=.15), cpu_cores=psutil.cpu_count(),
                        ram=dict(psutil.virtual_memory()._asdict()), swap=dict(psutil.swap_memory()._asdict()),
                        disk=dict(psutil.disk_usage(str(self.state.workspace))._asdict()),
                        nexus_process_rss=psutil.Process().memory_info().rss,
                        uptime_s=max(0, time.time() - psutil.boot_time()))
        except Exception as exc:  # Monitoring failure must not break dashboard controls.
            data["measurement_error"] = str(exc)[:300]
        if shutil.which("nvidia-smi"):
            try:
                data["gpu"] = self.command(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                                            "--format=csv,noheader,nounits"], timeout=4).strip()
                def number(value):
                    try:
                        parsed = float(value.strip())
                        return parsed if math.isfinite(parsed) and parsed >= 0 else None
                    except ValueError:
                        return None
                for row in csv.reader(data["gpu"].splitlines()):
                    if len(row) == 4:
                        data["gpu_devices"].append({"name": row[0].strip(), "utilization_percent": number(row[1]),
                            "memory_used_mib": number(row[2]), "memory_total_mib": number(row[3])})
                data["gpu_error"] = None if data["gpu_devices"] else "No readable NVIDIA GPU measurements"
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                data["gpu_error"] = str(exc)[:300]
        data["note"] = "Server environment measurements, not browser hardware. Container/VM totals may reflect the host. RAM/disk bytes; GPU MiB. Non-NVIDIA GPU monitoring is unavailable."
        return observation(data, "psutil + optional nvidia-smi")

    def launch_job(self, kind: str, action) -> dict:
        ident = uuid.uuid4().hex[:12]
        with self.lock:
            if sum(j["status"] == "running" for j in self.jobs.values()) >= 2:
                raise ValueError("Two operations already running; wait for completion")
            self.jobs[ident] = {"id": ident, "kind": kind, "status": "running", "started": time.time()}
        def worker():
            try:
                result = action(ident)
                with self.lock:
                    self.jobs[ident].update(status="done", result=result, finished=time.time())
            except Exception as exc:
                with self.lock:
                    self.jobs[ident].update(status="error", error=str(exc)[:2000], finished=time.time())
        threading.Thread(target=worker, daemon=True, name="workbench-" + ident).start()
        return dict(self.jobs[ident])

    def pull(self, model: str) -> dict:
        if not model or model.startswith("-") or len(model) > 200 or any(c.isspace() for c in model):
            raise ValueError("Invalid model name")
        def run(ident):
            success = False
            with httpx.stream("POST", ollama_url() + "/api/pull", json={"model": model, "stream": True}, timeout=600) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    progress = json.loads(line)
                    success = progress.get("status") == "success"
                    if progress.get("error"):
                        raise ValueError(progress["error"])
                    with self.lock:
                        self.jobs[ident]["progress"] = progress
            if not success:
                raise ValueError("Ollama stream ended without a success status; inspect inventory before retry")
            return {"model": model, "message": "Ollama confirmed success"}
        return self.launch_job("ollama-pull", run)

    def github(self, view: str, number: str = "") -> dict:
        if view == "issues":
            args = ["gh", "issue", "list", "--limit", "30", "--json", "number,title,url,state"]
        elif view == "prs":
            args = ["gh", "pr", "list", "--limit", "30", "--json", "number,title,url,state,headRefName"]
        elif view == "checks" and number.isdigit():
            args = ["gh", "pr", "view", number, "--json", "number,url,statusCheckRollup"]
        elif view == "logs" and number.isdigit():
            return observation(self.command(["gh", "run", "view", number, "--log-failed"], timeout=30), "GitHub Actions failed logs (untrusted task data)")
        elif view == "issue" and number.isdigit():
            args = ["gh", "issue", "view", number, "--json", "number,title,body,url"]
        else:
            raise ValueError("Unknown GitHub view or invalid number")
        return observation(json.loads(self.command(args, timeout=20)), "GitHub CLI (untrusted repository content)")

    def pr_preview(self, payload: dict) -> dict:
        status = self.git_status()["data"]
        branch = status["branch"]
        if not branch or branch in {"main", "master"} or branch.startswith("-"):
            raise ValueError("Create a feature branch in your terminal first; main/detached pushes are refused")
        if status["status"].strip():
            raise ValueError("Review and commit local changes in your terminal before creating a PR")
        title, body = str(payload.get("title", "")).strip(), str(payload.get("body", "")).strip()
        if not title or not body or len(title) > 250 or len(body) > 30_000:
            raise ValueError("A title and reviewed PR body are required")
        base = str(payload.get("base", "main"))
        self.command(["git", "check-ref-format", "--branch", base])
        diff = self.command(["git", "diff", "--no-ext-diff", "origin/" + base + "...HEAD"])
        remote = self.command(["git", "remote", "get-url", "origin"]).strip()
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", remote)
        if not match or ".." in match[1].split("/"):
            raise ValueError("PR wizard requires a GitHub origin URL without embedded credentials")
        repository = match[1]
        ident = uuid.uuid4().hex
        preview = {"id": ident, "branch": branch, "base": base, "title": title, "body": body,
                   "remote": remote, "repository": repository, "diff": diff, "fingerprint": self.fingerprint(),
                   "expires": time.time() + 300,
                   "test_evidence": [j for j in self.jobs.values() if j["kind"] == "tests"][-3:]}
        self.previews[ident] = preview
        return observation(preview, "local committed diff; test evidence only from explicit workbench test jobs")

    def create_pr(self, payload: dict) -> dict:
        preview = self.previews.get(str(payload.get("preview_id")))
        if not preview or preview["expires"] < time.time():
            raise ValueError("Preview missing/expired; review again")
        if payload.get("confirm") != "PUSH AND CREATE " + preview["branch"]:
            raise ValueError("Typed confirmation does not match branch")
        current = self.git_status()["data"]
        if current["branch"] != preview["branch"] or self.fingerprint() != preview["fingerprint"] or current["status"].strip():
            raise ValueError("Workspace changed since preview; review again")
        if self.command(["git", "remote", "get-url", "origin"]).strip() != preview["remote"]:
            raise ValueError("Remote changed; review again")
        with self.lock:
            if self.previews.pop(preview["id"], None) is None:
                raise ValueError("Preview already used; review again")
        self.command(["git", "push", "origin", preview["branch"]], timeout=120)
        url = self.command(["gh", "pr", "create", "--repo", preview["repository"], "--base", preview["base"], "--head", preview["branch"],
                            "--title", preview["title"], "--body", preview["body"]], timeout=60).strip()
        self.previews.pop(preview["id"], None)
        return observation({"url": url}, "git push + GitHub CLI (explicit confirmation)")

    def prepare(self, payload: dict) -> dict:
        policy = self.settings()
        options = {"policy": policy, "manual_local": bool(payload.get("local_model")), "enqueue": bool(payload.get("enqueue")),
                   "mode": str(payload.get("mode") or "execute")}
        repository = payload.get("github_repo")
        if repository is not None:
            from .github_repositories import SLUG
            if not isinstance(repository, str) or not SLUG.fullmatch(repository) or '..' in repository.split('/'):
                raise ValueError("Choose a valid GitHub repository")
            options["github_repo"] = repository
        if options["mode"] not in {"plan", "execute"}:
            raise ValueError("Unknown run mode")
        return options

    def configuration(self, payload=None):
        import yaml

        from .config import ConfigError
        from .gui_config import Configuration
        try:
            service = Configuration(self)
            return observation(service.snapshot() if payload is None else service.save(payload),
                               "existing workspace YAML configuration")
        except (yaml.YAMLError, ConfigError):
            raise ValueError("Configuration YAML is invalid; inspect it locally. File contents and keys are not exposed.") from None

    def get(self, path: str, query: dict) -> Any:
        if path == "configuration":
            return self.configuration()
        if path.startswith("repository/"):
            return self.repositories.get(path[11:], query)
        if path.startswith("studio/"):
            return self.studio.get(path[7:], query)
        def arg(name):
            return (query.get(name) or [""])[0]
        if path == "settings":
            return observation(self.settings(), "workspace preferences (dashboard.db)")
        if path == "history":
            items = self.history(arg("q"), arg("id"))
            for item in items:
                live = self.state.sessions.get(item["task_id"])
                if live:
                    item.update(live.snapshot())
                    item["log"] = live.log[-4000:]
                if item["status"] in {"queued", "running", "awaiting_approval"} and item["task_id"] not in self.state.sessions:
                    item["status"] = "interrupted"
                    item["note"] = "Server restarted; side effects must be reviewed before any retry"
            if not arg("id"):
                items = [{k: v for k, v in item.items() if k != "log"} for item in items]
            return observation(items, "durable dashboard run history (latest 300)")
        if path == "git":
            return self.git_status()
        if path == "files":
            return self.files()
        if path == "file":
            return self.file(arg("path"))
        if path == "metrics":
            return self.metrics()
        if path == "queue":
            return observation({"active": self.state.active_task, "paused": self.state.queue_paused,
                                "waiting": [self.state.sessions[i].snapshot() for i in self.state.task_queue]}, "server task scheduler")
        if path == "jobs":
            with self.lock:
                return observation(list(self.jobs.values()), "current-server jobs; not resumed after restart")
        if path == "github":
            return self.github(arg("view"), arg("number"))
        if path == "errors":
            errors = []
            for item in self.history():
                for event in item.get("log", []):
                    if event.get("type") == "error" or event.get("is_error") or event.get("error"):
                        message = str(event.get("message") or event.get("error") or event.get("preview") or "")
                        hint = ("Provider quota exhausted; check quota or choose another model" if "429" in message
                                else "Insufficient balance; fund provider or use a local model" if "402" in message
                                else "Model/route missing; check installed model and endpoint" if "404" in message
                                else "Check server reachability and inspect the recorded error")
                        errors.append({"task_id": item["task_id"], "message": message, "hint": hint, "ts": event.get("ts")})
            return observation(errors[-100:], "recorded run errors, not a threat scanner")
        raise ValueError("Unknown workbench endpoint")

    def post(self, path: str, payload: dict) -> Any:
        if path == "configuration":
            return self.configuration(payload)
        if path.startswith("repository/"):
            return self.repositories.post(path[11:], payload)
        if path.startswith("studio/"):
            return self.studio.post(path[7:], payload)
        if path == "settings":
            return observation(self.save_settings(payload), "saved workspace preferences")
        if path == "health":
            return self.health(str(payload.get("agent", "")))
        if path == "ollama":
            return self.ollama()
        if path == "capability":
            smoke = payload.get("smoke", False)
            if type(smoke) is not bool or (smoke and payload.get("confirm") != "RUN SMALL MODEL TEST"):
                raise ValueError("An inference test requires explicit RUN SMALL MODEL TEST confirmation")
            return self.capability(str(payload.get("model", "")), smoke)
        if path == "pull":
            model = str(payload.get("model", ""))
            if payload.get("confirm") != "PULL " + model:
                raise ValueError("Confirm download and disk/network usage")
            return self.pull(model)
        if path == "delete-model":
            model = str(payload.get("model", ""))
            if model not in ollama_models()["models"] or payload.get("confirm") != "DELETE " + model:
                raise ValueError("Choose an installed model and type DELETE <model>")
            httpx.request("DELETE", ollama_url() + "/api/delete", json={"model": model}, timeout=30).raise_for_status()
            return observation({"deleted": model}, "explicit Ollama delete")
        if path == "tests":
            if payload.get("confirm") != "RUN TESTS":
                raise ValueError("Tests execute repository code; explicit confirmation required")
            return self.launch_job("tests", lambda ident: {"output": self.command([sys.executable, "-m", "pytest", "-q"], timeout=180),
                                                          "fingerprint": self.fingerprint(allow_unavailable=True)})
        if path == "pr-preview":
            return self.pr_preview(payload)
        if path == "create-pr":
            return self.create_pr(payload)
        if path == "queue":
            action = payload.get("action")
            with self.state.lock:
                if action == "pause":
                    self.state.queue_paused = True
                elif action == "resume":
                    self.state.queue_paused = False
                elif action == "reorder":
                    order = payload.get("order")
                    if not isinstance(order, list) or len(order) != len(set(order)) or set(order) != set(self.state.task_queue):
                        raise ValueError("Order must contain exactly the current waiting task IDs")
                    self.state.task_queue[:] = order
                elif action == "cancel":
                    ident = str(payload.get("task_id"))
                    if ident not in self.state.task_queue:
                        raise ValueError("Task is not waiting; use the active run Stop button")
                    session = self.state.sessions[ident]
                    session.cancel.set()
                    session.status = "cancelled"
                    session.result = {"ok": False, "error": "cancelled before execution"}
                    session.push({"type": "done", "status": "cancelled", "result": session.result})
                    self.state.task_queue.remove(ident)
                    self.save_run(session)
                else:
                    raise ValueError("Unknown queue action")
            self.state.schedule()
            return self.get("queue", {})
        if path in {"retry", "execute-plan"}:
            items = self.history(task_id=str(payload.get("task_id", "")))
            if not items:
                raise ValueError("Run not found")
            item = items[0]
            if item["task_id"] in self.state.sessions and self.state.sessions[item["task_id"]].status in {"queued", "running", "awaiting_approval"}:
                raise ValueError("Run is still active")
            if not item.get("fingerprint") or item["fingerprint"] != self.fingerprint():
                raise ValueError("Workspace changed/unverifiable: review side effects and start a new task")
            options = self.prepare({})  # latest, possibly stricter security policy
            options["manual_local"] = item.get("options", {}).get("manual_local", bool(item.get("local_model")))
            task = item["task"]
            pipeline = item.get("pipeline")
            role = item.get("role")
            read_only = bool(item.get("read_only"))
            if path == "execute-plan":
                if item.get("options", {}).get("mode") != "plan" or item["status"] != "done":
                    raise ValueError("Choose a completed planning run")
                if payload.get("confirm") != "EXECUTE REVIEWED PLAN" or not str(payload.get("plan", "")).strip():
                    raise ValueError("Review/edit the plan and explicitly confirm execution")
                task += "\n\nUser-reviewed plan:\n" + str(payload["plan"])[:40_000]
                read_only = False
            else:
                if item["status"] != "error" or payload.get("confirm") != "RETRY AFTER REVIEW":
                    raise ValueError("Only failed runs can retry after side-effect review")
                if pipeline:
                    steps = item.get("result", {}).get("steps") or []
                    first = next((i for i, step in enumerate(steps) if not step.get("ok")), None)
                    if first is None or not self.state.config.pipeline(pipeline):
                        raise ValueError("No recorded failed pipeline step; start a new run")
                    saved_steps = item.get("options", {}).get("pipeline_steps")
                    current_steps = self.state.config.pipeline(pipeline).steps
                    if saved_steps is not None and saved_steps != current_steps:
                        raise ValueError("Pipeline definition changed; start a reviewed new run")
                    options["retry_from"] = first + item.get("options", {}).get("retry_from", 0)
                    options["handoff"] = item.get("options", {}).get("handoff", "") + "\n" + "\n".join(str(step.get("summary", "")) for step in steps[:first])
            self.configure(load_config(self.workspace), options, item.get("local_model"))
            session = self.state.create(task, role, read_only, pipeline=pipeline,
                local_model=item.get("local_model"), options=options)
            self.state.start(session)
            return {"task_id": session.task_id, "status": session.status}
        raise ValueError("Unknown workbench action")
