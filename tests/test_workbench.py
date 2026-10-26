"""Workbench services: real Git fixture, scripted network, no model/GitHub calls."""
from __future__ import annotations

import json
import time

import pytest

from nexus.config import load_config
from nexus.server import ServerState
from nexus.tools import ApprovalGate
from nexus.types import Risk, ToolSpec
from nexus.workbench import local_endpoint, proposal, safe_path


@pytest.fixture()
def wb(repo):
    return ServerState(repo, quiet=True).workbench


def test_preferences_durable_and_workspace_scoped(wb, repo):
    result = wb.save_settings({"local_only": True, "default_local_model": "test:8b", "language": "bn"})
    assert result["local_only"] is True
    assert ServerState(repo, quiet=True).workbench.settings()["language"] == "bn"
    other = repo / "other"
    other.mkdir()
    assert ServerState(other, quiet=True).workbench.settings()["language"] == "en"
    assert not (repo / "dashboard.db").exists()


def test_settings_validation_and_prices(wb):
    for payload in ({"local_only": "true"}, {"role_models": {"unknown": "gemini"}},
                    {"write_paths": ["../escape"]}, {"write_paths": [".git"]},
                    {"pricing": {"model": {"input_per_million": -1, "output_per_million": 1, "source": "test"}}}):
        with pytest.raises(ValueError):
            wb.save_settings(payload)
    prices = {"test-model": {"input_per_million": 1, "output_per_million": 2, "source": "user supplied test rate"}}
    assert wb.save_settings({"pricing": prices})["pricing"] == prices


def test_local_only_removes_all_hosted_agents(wb):
    wb.save_settings({"local_only": True, "default_local_model": "test:8b"})
    config = wb.configure(load_config(wb.workspace), wb.prepare({}), None)
    assert set(config.agents) == {"selected-ollama"}
    assert all(role.agent == "selected-ollama" for role in config.roles.roles.values())
    assert config.settings.router_agent == config.settings.learning_agent == "selected-ollama"
    assert all(agent.fallback is None for agent in config.agents.values())
    assert not local_endpoint("http://localhost.example.com/v1")
    assert not local_endpoint("https://example.com")


def test_local_only_refuses_public_ollama(wb, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "https://example.com")
    wb.save_settings({"local_only": True, "default_local_model": "test:8b"})
    with pytest.raises(ValueError, match="loopback/private"):
        wb.configure(load_config(wb.workspace), wb.prepare({}), None)


def test_per_role_mapping_and_manual_pin(wb):
    wb.save_settings({"role_models": {"backend": "ollama:test:8b", "docs": "gemini"}})
    config = wb.configure(load_config(wb.workspace), wb.prepare({}), None)
    backend = config.agent_for_role(config.role("backend"))
    assert backend.model == "test:8b" and backend.fallback is None
    config = wb.configure(load_config(wb.workspace), wb.prepare({"local_model": "manual:8b"}), "manual:8b")
    assert all(role.agent == "selected-ollama" for role in config.roles.roles.values())


def test_folder_policy_cannot_be_bypassed_by_auto_approval(wb, repo):
    wb.save_settings({"write_paths": ["src"]})
    gate = ApprovalGate(mode="auto")
    gate.policy = wb.write_policy(wb.settings())
    write = ToolSpec(name="write_file", description="", input_schema={}, risk=Risk.WRITE)
    shell = ToolSpec(name="run_command", description="", input_schema={}, risk=Risk.WRITE)
    assert gate.check(write, {"path": "src/hello.txt"}).approved
    assert not gate.check(write, {"path": "outside.txt"}).approved
    assert not gate.check(shell, {"command": "echo hello"}).approved
    (repo / "src").mkdir()
    (repo / "src" / "escape").symlink_to(repo.parent, target_is_directory=True)
    assert not gate.check(write, {"path": "src/escape/outside.txt"}).approved


def test_diff_matches_append_and_edit_without_writing(wb, repo):
    file = repo / "hello.txt"
    file.write_text("Hello\n")
    append = proposal(repo, "write_file", {"path": "hello.txt", "content": "Nexus\n", "append": True})
    assert append["after"] == "Hello\nNexus\n"
    edit = proposal(repo, "edit_file", {"path": "hello.txt", "old_text": "Hello", "new_text": "Nexus"})
    assert "-Hello" in edit["diff"] and "+Nexus" in edit["diff"]
    assert file.read_text() == "Hello\n"
    assert not proposal(repo, "run_command", {"command": "echo hello"})["available"]
    for path in ("../escape", ".git/config", ".env", "keys.json"):
        with pytest.raises(ValueError):
            safe_path(repo, path)


def test_durable_run_history_does_not_claim_resume(wb, repo):
    session = wb.state.create("Inspect hello.txt", None, True, options=wb.prepare({"mode": "plan"}))
    session.push({"type": "status", "message": "queued"})
    wb.save_run(session)
    restarted = ServerState(repo, quiet=True).workbench
    rows = restarted.get("history", {})["data"]
    assert rows[0]["status"] == "interrupted"
    details = restarted.get("history", {"id": [session.task_id]})["data"][0]
    assert details["log"][0]["message"] == "queued"


def test_queue_pause_reorder_cancel_without_model_calls(wb):
    wb.post("queue", {"action": "pause"})
    first = wb.state.create("first", None, True)
    second = wb.state.create("second", None, True)
    wb.state.start(first)
    wb.state.start(second)
    assert wb.state.active_task is None
    wb.post("queue", {"action": "reorder", "order": [second.task_id, first.task_id]})
    assert wb.state.task_queue == [second.task_id, first.task_id]
    wb.post("queue", {"action": "cancel", "task_id": first.task_id})
    assert first.status == "cancelled"
    assert wb.state.task_queue == [second.task_id]
    with pytest.raises(ValueError):
        wb.post("queue", {"action": "reorder", "order": []})


def test_plan_execution_requires_review_and_unchanged_workspace(wb, repo, monkeypatch):
    session = wb.state.create("Inspect repository", None, True, options=wb.prepare({"mode": "plan"}))
    session.status = "done"
    session.result = {"ok": True, "text": "Create hello.txt"}
    wb.save_run(session)
    assert wb.history(task_id=session.task_id)[0]["fingerprint"]
    with pytest.raises(ValueError, match="explicitly confirm"):
        wb.post("execute-plan", {"task_id": session.task_id, "plan": "reviewed"})
    (repo / "changed.txt").write_text("changed")
    with pytest.raises(ValueError, match="Workspace changed"):
        wb.post("execute-plan", {"task_id": session.task_id, "plan": "reviewed", "confirm": "EXECUTE REVIEWED PLAN"})
    (repo / "changed.txt").unlink()
    monkeypatch.setattr(wb.state, "start", lambda s: None)
    result = wb.post("execute-plan", {"task_id": session.task_id, "plan": "Create hello.txt", "confirm": "EXECUTE REVIEWED PLAN"})
    assert not wb.state.get(result["task_id"]).read_only
    assert "User-reviewed plan" in wb.state.get(result["task_id"]).task


def test_metrics_are_sourced_and_gpu_unavailable_when_no_tool(wb, monkeypatch):
    monkeypatch.setattr("nexus.workbench.shutil.which", lambda name: None)
    result = wb.metrics()
    assert result["source"] == "psutil + optional nvidia-smi"
    assert result["checked_at"] <= time.time()
    assert result["data"]["gpu"] is None


def test_health_probe_blocks_hosted_under_local_only(wb):
    wb.save_settings({"local_only": True})
    with pytest.raises(ValueError, match="blocks hosted"):
        wb.health("gemini")


def test_git_files_and_safe_content(wb, repo):
    (repo / "hello.txt").write_text("Hello Nexus\n")
    names = wb.files()["data"]
    assert "hello.txt" in names
    result = wb.file("hello.txt")["data"]
    assert result["content"] == "Hello Nexus\n"
    assert "+Hello Nexus" in result["diff"]
    assert "head" in wb.git_status()["data"]


def test_mutating_actions_require_explicit_confirmation(wb):
    for path, payload in (("pull", {"model": "test:8b"}), ("tests", {}), ("create-pr", {})):
        with pytest.raises(ValueError):
            wb.post(path, payload)


def test_retry_pipeline_uses_failed_step_and_prior_handoff(wb, monkeypatch):
    source = wb.state.create("Inspect", None, True, pipeline="ship-feature")
    source.status = "error"
    source.result = {"steps": [{"ok": True, "summary": "backend handoff"}, {"ok": False, "summary": "QA failed"}]}
    wb.save_run(source)
    monkeypatch.setattr(wb.state, "start", lambda s: None)
    result = wb.post("retry", {"task_id": source.task_id, "confirm": "RETRY AFTER REVIEW"})
    retry = wb.state.get(result["task_id"])
    assert retry.options["retry_from"] == 1
    assert "backend handoff" in retry.options["handoff"]


def test_pr_preview_refuses_dirty_worktree(wb, repo):
    (repo / "hello.txt").write_text("Hello")
    with pytest.raises(ValueError):
        wb.pr_preview({"title": "test", "body": "reviewed"})


def test_github_fields_are_bounded_and_untrusted(wb, monkeypatch):
    called = []
    def command(args, timeout=15):
        called.append(args)
        return json.dumps({"title": "untrusted issue"})
    monkeypatch.setattr(wb, "command", command)
    assert "untrusted" in wb.github("issue", "12")["source"]
    assert called[0][:4] == ["gh", "issue", "view", "12"]
    with pytest.raises(ValueError):
        wb.github("logs", "--help")


def test_pr_preview_confirms_exact_repo_branch_and_one_use(wb, monkeypatch):
    calls = []
    monkeypatch.setattr(wb, "git_status", lambda: {"data": {"branch": "feature", "status": "", "head": "test"}})
    monkeypatch.setattr(wb, "fingerprint", lambda **kw: "unchanged")

    def command(args, timeout=15):
        calls.append(args)
        if args[:4] == ["git", "remote", "get-url", "origin"]:
            return "https://github.com/owner/repository.git"
        if args[:3] == ["gh", "pr", "create"]:
            return "https://github.com/owner/repository/pull/1"
        return ""

    monkeypatch.setattr(wb, "command", command)
    preview = wb.pr_preview({"title": "Reviewed", "body": "Reviewed diff", "base": "main"})["data"]
    assert preview["repository"] == "owner/repository"
    assert not any(c[:2] == ["git", "push"] for c in calls)
    with pytest.raises(ValueError, match="confirmation"):
        wb.create_pr({"preview_id": preview["id"], "confirm": "wrong"})
    result = wb.create_pr({"preview_id": preview["id"], "confirm": "PUSH AND CREATE feature"})
    assert result["data"]["url"].endswith("/pull/1")
    assert ["git", "push", "origin", "feature"] in calls
    gh = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert gh[3:5] == ["--repo", "owner/repository"]
    with pytest.raises(ValueError, match="missing/expired"):
        wb.create_pr({"preview_id": preview["id"], "confirm": "PUSH AND CREATE feature"})


def test_pr_rejects_branch_changed_after_preview(wb, monkeypatch):
    monkeypatch.setattr(wb, "git_status", lambda: {"data": {"branch": "feature", "status": "", "head": "test"}})
    monkeypatch.setattr(wb, "fingerprint", lambda **kw: "unchanged")
    monkeypatch.setattr(wb, "command", lambda args, **kw: "git@github.com:owner/repository.git" if args[1] == "remote" else "")
    preview = wb.pr_preview({"title": "Reviewed", "body": "Reviewed diff"})["data"]
    monkeypatch.setattr(wb, "git_status", lambda: {"data": {"branch": "different", "status": "", "head": "test"}})
    with pytest.raises(ValueError, match="Workspace changed"):
        wb.create_pr({"preview_id": preview["id"], "confirm": "PUSH AND CREATE feature"})


def test_model_smoke_requires_server_confirmation(wb):
    with pytest.raises(ValueError, match="explicit"):
        wb.post("capability", {"model": "test:8b", "smoke": True})


def test_local_only_preserves_local_per_role_mappings_but_refuses_hosted(wb):
    wb.save_settings({"local_only": True, "default_local_model": "default:8b",
                      "role_models": {"backend": "ollama:code:8b"}})
    config = wb.configure(load_config(wb.workspace), wb.prepare({}), "default:8b")
    assert config.agent_for_role(config.role("backend")).model == "code:8b"
    assert all(agent.fallback is None for agent in config.agents.values())
    wb.save_settings({"role_models": {"backend": "gemini"}})
    with pytest.raises(ValueError, match="conflicts with local-only"):
        wb.configure(load_config(wb.workspace), wb.prepare({}), "default:8b")


def test_metrics_gpu_structured_multiple_devices_and_unknown_readings(wb, monkeypatch):
    monkeypatch.setattr("nexus.workbench.shutil.which", lambda _: "/test-only/nvidia-smi")
    def sample(argv, timeout=4):
        assert argv[0] == "nvidia-smi"
        return '"Example GPU, one", 25, 1024, 8192\nExample GPU two, [N/A], [Not Supported], 16384\n'
    monkeypatch.setattr(wb, "command", sample)
    data = wb.metrics()["data"]
    assert data["gpu_devices"][0] == {"name": "Example GPU, one", "utilization_percent": 25,
        "memory_used_mib": 1024, "memory_total_mib": 8192}
    assert data["gpu_devices"][1]["utilization_percent"] is None
    assert data["gpu_devices"][1]["memory_used_mib"] is None
    assert data["gpu_error"] is None
    assert data["ram"]["total"] > 0 and data["disk"]["total"] > 0
    assert data["nexus_process_rss"] > 0


def test_gpu_failure_is_reported_without_inventing_zeroes(wb, monkeypatch):
    monkeypatch.setattr("nexus.workbench.shutil.which", lambda _: "/test-only/nvidia-smi")
    def fail(*args, **kwargs):
        raise ValueError("Driver unavailable")
    monkeypatch.setattr(wb, "command", fail)
    data = wb.metrics()["data"]
    assert data["gpu"] is None and data["gpu_devices"] == []
    assert data["gpu_error"] == "Driver unavailable"


def test_hardware_permission_failure_is_explicit_not_fake_zero(wb, monkeypatch):
    import psutil

    monkeypatch.setattr("nexus.workbench.shutil.which", lambda _: None)
    def denied(*args, **kwargs):
        raise psutil.AccessDenied(pid=123)
    monkeypatch.setattr(psutil, "Process", denied)
    data = wb.metrics()["data"]
    assert "measurement_error" in data
    assert data["cpu_percent"] is None and data["ram"] is None
