"""Bounded command output events and cancellation of the command's process group.

Only invoked after the existing tool approval gate. This is not a new execution
endpoint, a PTY, or a way to start unmanaged long-lived servers.
"""
from __future__ import annotations

import codecs
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .types import Event, EventHandler, EventType


def terminate_group(process: subprocess.Popen) -> None:
    """Reap the direct child and terminate descendants, including inherited pipes."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            pass
        # A shell can exit while a descendant ignores TERM or keeps a pipe open.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        try:
            import psutil
            children = psutil.Process(process.pid).children(recursive=True)
        except Exception:
            children = []
        for child in children:
            try:
                child.kill()
            except Exception:
                pass
        if process.poll() is None:
            process.kill()
    process.wait(timeout=5)


def stream_shell(command: str | list[str], cwd: Path, *, timeout: float, emit: EventHandler,
                 cancel: threading.Event | None = None, call_id: str = "",
                 max_chars: int = 24_000, tool: str = "run_command", env: dict[str, str] | None = None, redact_workspace: Path | None = None) -> tuple[str, int]:
    from .computer_control import EmergencyStop
    emergency = EmergencyStop()
    if emergency.stopped() or cancel is not None and cancel.is_set():
        raise RuntimeError("command cancelled before launch")
    from .redaction import StreamMask, mask, secrets
    sensitive = secrets(redact_workspace or cwd, env)
    env = dict(os.environ if env is None else env)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("PAGER", "cat")
    env.setdefault("PYTHONUNBUFFERED", "1")
    process = subprocess.Popen(command, shell=isinstance(command, str), cwd=str(cwd), env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=os.name == "posix")
    ident = uuid.uuid4().hex
    common = {"process_id": ident, "call_id": call_id, "tool": tool, "pid": process.pid}
    chunks: queue.Queue = queue.Queue(maxsize=32)
    stop_reading = threading.Event()
    readers = []

    def read_pipe(pipe, stream):
        try:
            while not stop_reading.is_set():
                data = pipe.read1(4096)
                if not data:
                    break
                while not stop_reading.is_set():
                    try:
                        chunks.put((stream, data), timeout=.05)
                        break
                    except queue.Full:
                        continue
        finally:
            pipe.close()
            while not stop_reading.is_set():
                try:
                    chunks.put((stream, None), timeout=.05)
                    break
                except queue.Full:
                    continue

    limit = max(1024, min(max_chars or 24_000, 64_000))
    captured = {"stdout": "", "stderr": ""}
    decoders = {name: codecs.getincrementaldecoder("utf-8")("replace") for name in captured}
    masks = {name: StreamMask(sensitive) for name in captured}
    raw_seen = 0
    streamed = 0
    truncated = False
    reason = "exited"
    started = time.monotonic()
    ended = 0
    exit_seen = None
    try:
        emit(Event(EventType.PROCESS, {**common, "status": "running", "command": mask(command, sensitive) if isinstance(command, str) else [mask(a, sensitive) for a in command], "cwd": str(cwd)}))
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            thread = threading.Thread(target=read_pipe, args=(pipe, name), daemon=True)
            thread.start()
            readers.append(thread)
        while ended < 2:
            if emergency.stopped() or cancel is not None and cancel.is_set():
                reason = "cancelled"
                break
            if time.monotonic() - started >= timeout:
                reason = "timed_out"
                break
            if process.poll() is not None:
                exit_seen = exit_seen or time.monotonic()
                if time.monotonic() - exit_seen > .25:
                    # run_command is not a background-process manager.
                    terminate_group(process)
            try:
                stream, raw = chunks.get(timeout=.05)
            except queue.Empty:
                continue
            text = decoders[stream].decode(raw or b"", final=raw is None)
            if raw is None:
                ended += 1
            available = max(0, limit - raw_seen)
            raw_piece = text[:available]
            raw_seen += len(raw_piece)
            piece = masks[stream].feed(raw_piece, final=raw is None or raw_seen >= limit)
            if piece:
                captured[stream] += piece
                streamed += len(piece)
                emit(Event(EventType.TOOL_OUTPUT, {**common, "stream": stream, "text": piece}))
            if len(text) > len(raw_piece) and not truncated:
                truncated = True
                emit(Event(EventType.TOOL_OUTPUT, {**common, "stream": "notice", "text": f"\n[Live output capped at {limit} characters; remaining output drained, not retained.]\n"}))
        # Pipes can close before the process exits; remain cancellable while waiting.
        while process.poll() is None and reason == "exited":
            if emergency.stopped() or cancel is not None and cancel.is_set():
                reason = "cancelled"
            elif time.monotonic() - started >= timeout:
                reason = "timed_out"
            else:
                time.sleep(.05)
        terminate_group(process)
        emit(Event(EventType.PROCESS, {**common, "status": reason, "exit_code": process.returncode,
                   "duration_ms": round((time.monotonic() - started) * 1000, 2), "truncated": truncated}))
    finally:
        # Callback exceptions and cancellation must never orphan a child process.
        terminate_group(process)
        stop_reading.set()
        for thread in readers:
            thread.join(timeout=1)
        if not readers:
            process.stdout.close()
            process.stderr.close()
    if reason == "timed_out":
        raise subprocess.TimeoutExpired(command, timeout)
    if reason == "cancelled":
        raise RuntimeError("command cancelled; process group stopped")
    parts = [captured["stdout"].rstrip()]
    if captured["stderr"]:
        parts.append("[stderr]\n" + captured["stderr"].rstrip())
    if truncated:
        parts.append(f"[output capped at {limit} characters]")
    parts.append(f"[exit {process.returncode}]")
    return "\n".join(part for part in parts if part), process.returncode
