"""Real disposable child processes; no providers or external commands are contacted."""
from __future__ import annotations

import os
import shlex
import sys
import threading

import psutil
import pytest

from nexus.live_process import stream_shell
from nexus.server import ServerState, Session
from nexus.tools import ApprovalGate, PathGuard, ToolContext, build_tools
from nexus.types import Event, EventType


def command(code):
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(code)}"


def no_live_pid(pid):
    try:
        return not psutil.Process(pid).is_running() or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_output_arrives_before_command_completes_and_stderr_is_separate(repo):
    ready, release = threading.Event(), repo / "release"
    events, results = [], []
    def emit(e):
        events.append(e)
        if e.type == EventType.TOOL_OUTPUT and "first" in e.data["text"]:
            ready.set()
    code = f"import pathlib,time,sys;print('first');sys.stderr.write('diagnostic\\n');\nwhile not pathlib.Path({str(release)!r}).exists(): time.sleep(.02)\nprint('last')"
    worker = threading.Thread(target=lambda: results.append(stream_shell(command(code), repo, timeout=10, emit=emit)))
    worker.start()
    try:
        assert ready.wait(5), "No output while process was still running"
        assert worker.is_alive()
        release.touch()
        worker.join(5)
        assert not worker.is_alive()
    finally:
        release.touch()
        worker.join(10)
    assert results[0][1] == 0
    assert "last" in results[0][0] and "[stderr]" in results[0][0]
    outputs = [e.data for e in events if e.type == EventType.TOOL_OUTPUT]
    assert any(e["stream"] == "stderr" and "diagnostic" in e["text"] for e in outputs)
    lifecycle = [e.data for e in events if e.type == EventType.PROCESS]
    assert [e["status"] for e in lifecycle] == ["running", "exited"]
    assert lifecycle[-1]["exit_code"] == 0
    assert len({e["process_id"] for e in outputs + lifecycle}) == 1


def test_cancel_terminates_process_group_and_reports_real_status(repo):
    cancelled, events = threading.Event(), []
    def emit(e):
        events.append(e)
        if e.type == EventType.TOOL_OUTPUT and "ready" in e.data["text"]:
            cancelled.set()
    with pytest.raises(RuntimeError, match="cancelled"):
        stream_shell(command("import time;print('ready');time.sleep(30)"), repo,
                     timeout=10, emit=emit, cancel=cancelled)
    lifecycle = [e.data for e in events if e.type == EventType.PROCESS]
    assert lifecycle[-1]["status"] == "cancelled"
    assert lifecycle[-1]["exit_code"] is not None
    assert no_live_pid(lifecycle[0]["pid"])


def test_timeout_cleans_up_and_emits_timed_out(repo):
    events = []
    with pytest.raises(__import__('subprocess').TimeoutExpired):
        stream_shell(command("import time;time.sleep(30)"), repo, timeout=.2, emit=events.append)
    lifecycle = [e.data for e in events if e.type == EventType.PROCESS]
    assert lifecycle[-1]["status"] == "timed_out"
    assert no_live_pid(lifecycle[0]["pid"])


def test_live_output_is_bounded_and_still_drains_large_output(repo):
    events = []
    out, code = stream_shell(command("import sys;sys.stdout.write('X'*1000000)"), repo,
                            timeout=10, emit=events.append, max_chars=4096)
    assert code == 0 and len(out) < 4300 and "capped" in out
    outputs = [e.data for e in events if e.type == EventType.TOOL_OUTPUT]
    assert sum(len(e["text"]) for e in outputs if e["stream"] != "notice") == 4096
    assert len([e for e in outputs if e["stream"] == "notice"]) == 1


def test_callback_failure_cannot_orphan_process(repo):
    pids = []
    def fail(e):
        if e.type == EventType.PROCESS:
            pids.append(e.data["pid"])
            raise ValueError("subscriber failed")
    with pytest.raises(ValueError, match="subscriber failed"):
        stream_shell(command("import time;time.sleep(30)"), repo, timeout=10, emit=fail)
    assert pids and no_live_pid(pids[0])


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group behavior")
def test_cancel_kills_descendants_even_if_shell_exits(repo):
    cancelled, children = threading.Event(), []
    def emit(e):
        if e.type == EventType.TOOL_OUTPUT and e.data["stream"] == "stdout":
            children.append(int(e.data["text"].strip()))
            cancelled.set()
    code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); print(p.pid); time.sleep(30)"
    with pytest.raises(RuntimeError, match="cancelled"):
        stream_shell(command(code), repo, timeout=10, emit=emit, cancel=cancelled)
    assert children
    # Signal delivery is asynchronous; under browser/CI load descendants may
    # briefly remain runnable after killpg returns. Still require actual exit.
    for _ in range(200):
        if no_live_pid(children[0]):
            break
        threading.Event().wait(.01)
    assert no_live_pid(children[0])


def test_new_and_appended_file_diff_and_full_tool_result_are_observable(repo):
    events = []
    ctx = ToolContext(repo, PathGuard(repo), ApprovalGate(mode="auto"), emit=events.append)
    registry = build_tools(ctx)
    assert registry.call("write_file", {"path": "live.txt", "content": "first\n"})[1] is False
    assert registry.call("write_file", {"path": "live.txt", "content": "second\n", "append": True})[1] is False
    assert registry.call("edit_file", {"path": "live.txt", "old_text": "first", "new_text": "changed"})[1] is False
    assert registry.call("delete_file", {"path": "live.txt"})[1] is False
    changes = [e.data for e in events if e.type == EventType.FILE_CHANGE]
    assert [e["action"] for e in changes] == ["created", "updated", "updated", "deleted"]
    assert "+first" in changes[0]["diff"] and "+second" in changes[1]["diff"]
    assert "-first" in changes[2]["diff"] and "+changed" in changes[2]["diff"]
    assert "-changed" in changes[3]["diff"]
    calls = [e.data for e in events if e.type == EventType.TOOL_CALL]
    results = [e.data for e in events if e.type == EventType.TOOL_RESULT]
    assert len({e["call_id"] for e in calls}) == 4
    assert [e["call_id"] for e in changes] == [e["call_id"] for e in calls]
    assert all("output" in e for e in results)


def test_denied_command_never_starts_or_emits_output(repo):
    events = []
    ctx = ToolContext(repo, PathGuard(repo), ApprovalGate(mode="plan"), emit=events.append)
    _, error = build_tools(ctx).call("run_command", {"command": command("print('not approved')")})
    assert error
    assert not any(e.type in {EventType.PROCESS, EventType.TOOL_OUTPUT} for e in events)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1, 0])
def test_invalid_timeout_never_starts_a_command(repo, timeout):
    events = []
    ctx = ToolContext(repo, PathGuard(repo), ApprovalGate(mode="auto"), emit=events.append)
    _, error = build_tools(ctx).call("run_command", {"command": "echo nope", "timeout": timeout})
    assert error and not any(e.type == EventType.PROCESS for e in events)


def test_cancel_preserves_termination_events_but_refuses_new_tool_calls(repo):
    state = ServerState(repo, quiet=True)
    session = Session("cancel-test", "test")
    session.cancel.set()
    emit = state._emit(session)
    emit(Event(EventType.PROCESS, {"status": "cancelled", "exit_code": -15}))
    emit(Event(EventType.TOOL_RESULT, {"is_error": True, "preview": "cancelled"}))
    with pytest.raises(RuntimeError, match="cancelled"):
        emit(Event(EventType.TOOL_CALL, {"tool": "run_command"}))
    assert [e["type"] for e in session.log] == ["process", "tool_result"]


def test_real_agent_context_receives_task_cancellation(repo, config):
    from nexus.agent import NexusAgent

    agent = NexusAgent(config, workspace=repo, auto_yes=True)
    signal = threading.Event()
    agent.cancel_event = signal
    assert agent.build_context(config.role("backend")).cancel_event is signal


def test_deleted_large_or_binary_file_has_metadata_not_unbounded_diff(repo):
    events = []
    ctx = ToolContext(repo, PathGuard(repo), ApprovalGate(mode="auto"), emit=events.append)
    registry = build_tools(ctx)
    for name, content in [("binary.dat", b"\x00\xff"), ("large.txt", b"A" * 500001)]:
        (repo / name).write_bytes(content)
        assert registry.call("delete_file", {"path": name})[1] is False
    changes = [e.data for e in events if e.type == EventType.FILE_CHANGE]
    assert len(changes) == 2
    assert all("text diff omitted" in e["diff"] for e in changes)
