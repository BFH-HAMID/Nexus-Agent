"""Reviewed desktop/file/smart-home automation and semantic replay."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .common import AssistantError, assistant_dir, atomic_json, clean_text, read_json
from .personal_data import ExternalActions, NativeVault

MAX_BACKUP_FILES = 200
MAX_BACKUP_BYTES = 20_000_000
SKIP = {".git", ".nexus", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
SECRET_NAMES = {".env", "keys.json", "credentials.json", ".netrc", "id_rsa", "id_ed25519"}


class DesktopController:
    """Narrow mouse/keyboard operations with fresh review before every action."""

    def __init__(self, *, backend: str = "auto", review: Callable[[str, dict[str, Any]], bool] | None = None,
                 recorder=None):
        self.backend = backend
        self.review = review
        self.recorder = recorder

    def _backend(self) -> str:
        if self.backend != "auto":
            return self.backend
        if os.name == "nt":
            return "pywinauto"
        if shutil.which("xdotool"):
            return "xdotool"
        if shutil.which("ydotool"):
            return "ydotool"
        raise AssistantError("No supported desktop backend; install xdotool/ydotool or pywinauto")

    @staticmethod
    def _args(action: str, args: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "move": {"x", "y"}, "click": {"x", "y", "button"}, "type": {"text"},
            "key": {"keys"}, "scroll": {"clicks"},
        }
        if action not in allowed or set(args) - allowed[action]:
            raise AssistantError("Desktop action must be move/click/type/key/scroll with bounded arguments")
        clean = dict(args)
        for key in ("x", "y"):
            if key in clean and (type(clean[key]) is not int or not -100_000 <= clean[key] <= 100_000):
                raise AssistantError("Desktop coordinates must be bounded integers")
        if action == "click" and clean.get("button", "left") not in {"left", "right", "middle"}:
            raise AssistantError("Mouse button must be left/right/middle")
        if action == "type":
            clean["text"] = clean_text(clean.get("text"), 1000, lines=True)
        if action == "key":
            clean["keys"] = clean_text(clean.get("keys"), 120)
        if action == "scroll" and (type(clean.get("clicks")) is not int or not -20 <= clean["clicks"] <= 20):
            raise AssistantError("Scroll clicks must be an integer between -20 and 20")
        return clean

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        from ..computer_control import EmergencyStop

        clean = self._args(action, args)
        if EmergencyStop().stopped():
            raise AssistantError("Emergency stop is latched; desktop action refused")
        if self.review is None or self.review(action, clean) is not True:
            raise AssistantError("Fresh approval is required for every desktop action")
        if EmergencyStop().stopped():
            raise AssistantError("Emergency stop changed during approval")
        backend = self._backend()
        if backend == "pywinauto":
            self._windows(action, clean)
        else:
            self._linux(backend, action, clean)
        # Typed text/key sequences can contain credentials; only pointer semantics
        # are recorded automatically. Hand-authored replay still reviews every step.
        if self.recorder is not None and action in {"move", "click", "scroll"}:
            self.recorder.append(action, clean)
        return {"executed": action, "backend": backend, "arguments": clean,
                "note": "Execution was requested; visual success is not inferred."}

    @staticmethod
    def _linux(backend: str, action: str, args: dict[str, Any]) -> None:
        executable = shutil.which(backend)
        if not executable:
            raise AssistantError(f"{backend} is not installed")
        if backend == "xdotool":
            if action == "move":
                argv = [executable, "mousemove", str(args["x"]), str(args["y"])]
            elif action == "click":
                button = {"left": "1", "middle": "2", "right": "3"}[args.get("button", "left")]
                prefix = [executable, "mousemove", str(args["x"]), str(args["y"])] if "x" in args else []
                if prefix:
                    subprocess.run(prefix, timeout=5, check=False, stdin=subprocess.DEVNULL)
                argv = [executable, "click", button]
            elif action == "type":
                argv = [executable, "type", "--clearmodifiers", "--", args["text"]]
            elif action == "key":
                argv = [executable, "key", "--clearmodifiers", args["keys"]]
            else:
                argv = [executable, "click", "4" if args["clicks"] > 0 else "5"]
        else:
            if action == "move":
                argv = [executable, "mousemove", "--absolute", str(args["x"]), str(args["y"])]
            elif action == "click":
                code = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}[args.get("button", "left")]
                argv = [executable, "click", code]
            elif action == "type":
                argv = [executable, "type", "--", args["text"]]
            elif action == "key":
                argv = [executable, "key", args["keys"]]
            else:
                argv = [executable, "click", "0xC8" if args["clicks"] > 0 else "0xC9"]
        try:
            result = subprocess.run(argv, timeout=10, check=False, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssistantError(f"Desktop backend failed ({type(exc).__name__})") from exc
        if result.returncode:
            raise AssistantError("Desktop backend refused the action")

    @staticmethod
    def _windows(action: str, args: dict[str, Any]) -> None:
        try:
            from pywinauto import keyboard, mouse
        except ImportError as exc:
            raise AssistantError('Windows desktop support requires pip install -e ".[desktop]"') from exc
        if action == "move":
            mouse.move(coords=(args["x"], args["y"]))
        elif action == "click":
            coords = (args["x"], args["y"]) if "x" in args else None
            mouse.click(button=args.get("button", "left"), coords=coords)
        elif action == "type":
            # Braces make pywinauto metacharacters literal instead of key commands.
            encoded = "".join("{" + char + "}" if char in "+^%~()[]{}" else char for char in args["text"])
            keyboard.send_keys(encoded, with_spaces=True, with_tabs=True, with_newlines=True)
        elif action == "key":
            keyboard.send_keys(args["keys"])
        else:
            mouse.wheel(coords=None, wheel_dist=args["clicks"])


class WorkflowRecorder:
    """Semantic actions only—never raw keystrokes, passwords or hidden recording."""

    def __init__(self, workspace: Path | str):
        self.path = assistant_dir(workspace) / "recorded-workflow.json"
        self.active = False

    def start(self, name: str) -> dict[str, Any]:
        data = {"schema": 1, "name": clean_text(name, 120), "created": time.time(), "actions": []}
        atomic_json(self.path, data)
        self.active = True
        return data

    def append(self, action: str, args: dict[str, Any]) -> None:
        if not self.active:
            return
        data = read_json(self.path, {})
        rows = data.get("actions")
        if not isinstance(rows, list):
            raise AssistantError("Recorded workflow is invalid")
        if len(rows) >= 500:
            self.active = False
            raise AssistantError("Workflow action budget reached")
        rows.append({"action": action, "args": args, "recorded_at": time.time()})
        atomic_json(self.path, data)

    def stop(self) -> dict[str, Any]:
        self.active = False
        return read_json(self.path, {})

    def replay(self, controller: DesktopController) -> list[dict[str, Any]]:
        data = read_json(self.path, {})
        rows = data.get("actions")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
            raise AssistantError("No bounded semantic workflow is recorded")
        results = []
        for row in rows:
            if not isinstance(row, dict):
                raise AssistantError("Recorded workflow contains an invalid action")
            results.append(controller.execute(str(row.get("action")), row.get("args") or {}))
        return results


class BackupManager:
    def __init__(self, workspace: Path | str):
        self.workspace = Path(workspace).resolve()
        self.root = assistant_dir(workspace) / "backups"

    def create(self) -> dict[str, Any]:
        ident = uuid.uuid4().hex[:16]
        target = self.root / (ident + ".zip")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        count = total = 0
        try:
            with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(self.workspace.rglob("*")):
                    relative = path.relative_to(self.workspace)
                    if any(part in SKIP for part in relative.parts) or path.name in SECRET_NAMES or path.is_symlink():
                        continue
                    if not path.is_file() or path.stat().st_nlink != 1:
                        continue
                    size = path.stat().st_size
                    count += 1
                    total += size
                    if count > MAX_BACKUP_FILES or total > MAX_BACKUP_BYTES:
                        raise AssistantError("Backup file/byte budget exceeded")
                    archive.write(path, str(relative))
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {"id": ident, "path": str(target), "files": count, "bytes": total, "sha256": digest,
                "excludes": ".git/.nexus/secrets/caches; symlinks and hardlinked files"}

    def restore(self, ident: str, destination: Path | str, *, confirmation: str) -> dict[str, Any]:
        if confirmation != f"RESTORE BACKUP {ident}":
            raise AssistantError(f"Type RESTORE BACKUP {ident} exactly to confirm")
        archive_path = self.root / (ident + ".zip")
        target = (self.workspace / destination).resolve() if not Path(destination).is_absolute() else Path(destination).resolve()
        if not target.is_relative_to(self.workspace) or target == self.workspace:
            raise AssistantError("Restore destination must be a new directory inside the workspace")
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise AssistantError("Restore never overwrites a non-empty destination")
        if not archive_path.is_file() or archive_path.is_symlink():
            raise AssistantError("Unknown backup")
        count = total = 0
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                relative = Path(member.filename)
                if relative.is_absolute() or ".." in relative.parts or member.is_dir():
                    continue
                count += 1
                total += member.file_size
                if count > MAX_BACKUP_FILES or total > MAX_BACKUP_BYTES:
                    raise AssistantError("Backup contents exceed the restore budget")
                output = (target / relative).resolve()
                if not output.is_relative_to(target) or output.exists():
                    raise AssistantError("Unsafe or duplicate restore path")
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, output.open("xb") as destination_file:
                    shutil.copyfileobj(source, destination_file, length=1024 * 1024)
        return {"restored": count, "destination": str(target), "overwritten": 0}


class HomeAssistant:
    def __init__(self, vault: NativeVault, *, base_url: str, entities: list[str]):
        self.base_url = base_url.rstrip("/")
        if not self.base_url.startswith("https://") and not self.base_url.startswith(("http://127.0.0.1", "http://localhost")):
            raise AssistantError("Home Assistant requires HTTPS or explicit loopback HTTP")
        if not entities or len(entities) > 100 or any("." not in entity or len(entity) > 150 for entity in entities):
            raise AssistantError("An explicit bounded entity allowlist is required")
        self.entities = set(entities)
        self.vault = vault

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        token = self.vault.get("home_assistant")
        if not token:
            raise AssistantError("Home Assistant token is not configured")
        try:
            response = httpx.request(method, self.base_url + path,
                                     headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                                     json=body, timeout=15, follow_redirects=False, trust_env=False)
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise AssistantError("Home Assistant response budget exceeded")
            return response.json() if response.content else {}
        except (httpx.HTTPError, ValueError) as exc:
            raise AssistantError(f"Home Assistant unavailable ({type(exc).__name__})") from exc

    def states(self) -> list[dict[str, Any]]:
        rows = self._request("GET", "/api/states")
        return [{key: row.get(key) for key in ("entity_id", "state", "attributes", "last_changed")}
                for row in rows if isinstance(row, dict) and row.get("entity_id") in self.entities]

    def call(self, actions: ExternalActions, challenge: dict[str, Any], confirmation: str,
             payload: dict[str, Any]) -> Any:
        entity = str(payload.get("entity_id") or "")
        domain = str(payload.get("domain") or "")
        service = str(payload.get("service") or "")
        if (entity not in self.entities or entity.split(".", 1)[0] != domain
                or domain not in {"light", "switch", "fan", "climate", "media_player", "automation", "script", "scene"}
                or service not in {"turn_on", "turn_off", "toggle"}):
            raise AssistantError("Smart-home service/entity is outside the reviewed allowlist")

        def write():
            return self._request("POST", f"/api/services/{domain}/{service}", {"entity_id": entity})
        return actions.execute(challenge, confirmation, payload, write)


class MqttHome:
    """TLS MQTT publish with fresh approval and a topic-prefix allowlist."""

    def __init__(self, *, host: str, username: str, password: str, topic_prefixes: list[str], port: int = 8883):
        if not host or not 1 <= int(port) <= 65535 or not topic_prefixes:
            raise AssistantError("MQTT host, TLS port and topic allowlist are required")
        self.host, self.port, self.username, self.password = host, int(port), username, password
        self.topic_prefixes = topic_prefixes

    def publish(self, actions: ExternalActions, challenge: dict[str, Any], confirmation: str,
                payload: dict[str, Any]) -> Any:
        topic = str(payload.get("topic") or "")
        message = clean_text(payload.get("message"), 4000, lines=True)
        if not any(topic.startswith(prefix) for prefix in self.topic_prefixes) or len(topic) > 300 or any(c in topic for c in "#+\x00"):
            raise AssistantError("MQTT topic is outside the allowlist")

        def write():
            try:
                import paho.mqtt.client as mqtt
            except ImportError as exc:
                raise AssistantError('MQTT support requires pip install -e ".[smart-home]"') from exc
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.username_pw_set(self.username, self.password)
            client.tls_set()
            client.connect(self.host, self.port, keepalive=10)
            client.loop_start()
            try:
                info = client.publish(topic, message, qos=1, retain=False)
                info.wait_for_publish(timeout=10)
                if not info.is_published():
                    raise AssistantError("MQTT publish was not acknowledged")
            finally:
                client.disconnect()
                client.loop_stop()
            return {"published": True, "topic": topic, "retained": False}
        return actions.execute(challenge, confirmation, payload, write)
