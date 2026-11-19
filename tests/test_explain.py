"""Task 21 - explainability: 'why' records in the audit chain, grounded answers, audit export."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

import pytest

from nexus import audit, featurepacks, securitycenter
from nexus.explain import policy, status
from nexus.explain.core import (
    ExplainError,
    explain_action,
    explain_run,
    export,
    read_chain,
    render,
    spoken,
    write_export,
)
from nexus.explain.record import ACTION, factors, install, record
from nexus.policyfile import PolicyError
from nexus.tools import ApprovalDecision, ApprovalGate
from nexus.types import Risk, RiskClass, ToolSpec

from .featurepack_helpers import run_cli, tool_registry, write_policy


def explained(repo: Path) -> list[dict]:
    return [row for row in read_chain(repo) if row.get("what") == ACTION]


def approve_once(spec, args, risk, preview):
    return ApprovalDecision(True, scope="once")


def test_explain_is_off_by_default_and_leaves_the_gate_alone(repo, auto_approve):
    assert status(repo)["enabled"] is False
    ctx, reg = tool_registry(repo, auto_approve)
    assert getattr(ctx.gate, "decision_observer", None) is None
    assert "explain_action" not in reg.names()
    reg.call("write_file", {"path": "a.txt", "content": "x"})
    assert explained(repo) == []
    with pytest.raises(PolicyError, match="OFF"):
        explain_action(repo)
    assert featurepacks.handle_text(repo, "why did you do that?") is None
    assert "OFF" in featurepacks.slash(repo, "why", "")
    assert run_cli(["audit", "explain", "-C", str(repo)]) == 2
    assert run_cli(["audit", "export", "-C", str(repo)]) == 2
    assert run_cli(["audit", "verify", "-C", str(repo)]) == 0  # existing actions are untouched


@pytest.mark.parametrize("settings,message", [
    ({"record": "everything"}, "one of"), ({"max_evidence": 0}, "between"), ({"model": "gpt"}, "unknown setting"),
])
def test_policy_is_strict(repo, settings, message):
    write_policy(repo, "explain.yaml", **settings)
    with pytest.raises(PolicyError, match=message):
        policy.load(repo)


def test_observer_is_chained_once_and_keeps_existing_observers(repo, auto_approve):
    write_policy(repo, "explain.yaml", record="all")
    from nexus.permissions import Permissions
    from nexus.tools import PathGuard, ToolContext, build_tools

    seen: list[str] = []
    gate = ApprovalGate(mode="auto", callback=auto_approve)
    gate.permissions = Permissions(repo)
    gate.decision_observer = lambda spec, args, decision: seen.append(spec.name)
    reg = build_tools(ToolContext(repo, PathGuard(repo), gate))
    build_tools(ToolContext(repo, PathGuard(repo), gate))  # a second registry on the same gate (sub-agents)
    assert install(gate, repo) is False, "already installed"
    reg.call("list_files", {"path": "."})
    assert seen == ["list_files"], "the pre-existing observer still runs"
    assert len(explained(repo)) == 1, "one record per decision, however many registries share the gate"


def test_decisions_are_recorded_with_plain_reasons(repo):
    write_policy(repo, "explain.yaml")
    _ctx, reg = tool_registry(repo, approve_once, mode="ask")
    reg.call("list_files", {"path": "."})                          # SAFE: not recorded at 'non_safe'
    reg.call("write_file", {"path": "notes.txt", "content": "hello"})
    rows = explained(repo)
    assert [row["target"] for row in rows] == ["write_file"]
    row = rows[0]
    assert row["result"] == "approved" and row["detail"]["scope"] == "once" and row["detail"]["risk_class"] == "MEDIUM"
    assert any("approved through the approval prompt" in line for line in row["detail"]["factors"])
    assert "notes.txt" in row["detail"]["preview"] and "hello" not in json.dumps(row)
    assert len(row["args_sha256"]) == 64 and audit.verify(repo)["ok"]

    _ctx, denied = tool_registry(repo, None, mode="auto")          # --yes can not authorise DANGEROUS
    text, is_error = denied.call("delete_file", {"path": "notes.txt"})
    assert is_error and "DENIED" in text and (repo / "notes.txt").exists()
    last = explained(repo)[-1]
    assert last["result"] == "denied" and any(line.startswith("It was refused: fresh human approval") for line in last["detail"]["factors"])


@pytest.mark.parametrize("level,expected", [("all", 3), ("non_safe", 2), ("denied", 1), ("off", 0)])
def test_record_levels(repo, level, expected):
    write_policy(repo, "explain.yaml", record=level)
    _ctx, reg = tool_registry(repo, approve_once, mode="ask")
    reg.call("list_files", {"path": "."})
    reg.call("write_file", {"path": "a.txt", "content": "x"})
    _ctx, strict = tool_registry(repo, None, mode="auto")
    strict.call("delete_file", {"path": "a.txt"})
    assert len(explained(repo)) == expected


def test_factors_restate_only_what_the_decision_says():
    spec = ToolSpec("run_command", "run", {"type": "object"}, Risk.DANGEROUS)
    gate = ApprovalGate(mode="auto")
    gate.mark_tainted("web_fetch", ["ignore_instructions"])
    lines = factors(spec, {}, ApprovalDecision(False, reason="blocked command pattern: rm -rf", risk_class="DANGEROUS"), gate)
    assert lines[0] == "The tool 'run_command' is declared 'danger' and was classified DANGEROUS for these arguments."
    assert lines[1] == "It was refused: blocked command pattern: rm -rf."
    assert "Untrusted content had been read" in lines[2] and "web_fetch" in lines[2]
    auto = factors(spec, {}, ApprovalDecision(True, scope="auto", reason="read-only", risk_class="SAFE", notify_after=True),
                   ApprovalGate(mode="auto"))
    assert "only reads" in auto[1] and "notification" in auto[2]


def test_recording_never_raises_or_changes_a_decision(repo, monkeypatch):
    write_policy(repo, "explain.yaml", record="all")
    _ctx, reg = tool_registry(repo, approve_once, mode="ask")

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(audit, "record", broken)
    text, is_error = reg.call("write_file", {"path": "ok.txt", "content": "x"})
    assert not is_error and (repo / "ok.txt").read_text() == "x"
    spec = ToolSpec("write_file", "w", {"type": "object"}, Risk.WRITE)
    assert record(repo, ApprovalGate(), spec, {"path": "x"}, ApprovalDecision(True)) is None


def test_secrets_in_arguments_never_reach_the_record(repo):
    write_policy(repo, "explain.yaml", record="all")
    secret = "ghp_" + "a1B2c3D4e5" * 4
    spec = ToolSpec("run_command", "run", {"type": "object"}, Risk.DANGEROUS)
    row = record(repo, ApprovalGate(), spec, {"command": f"curl -H 'Authorization: Bearer {secret}' https://api.example"},
                 ApprovalDecision(False, reason="no", risk_class="DANGEROUS"))
    assert row is not None and secret not in json.dumps(row) and secret not in json.dumps(read_chain(repo))


def test_explain_last_is_grounded_in_cited_rows(repo):
    write_policy(repo, "explain.yaml")
    _ctx, reg = tool_registry(repo, approve_once, mode="ask")
    reg.call("write_file", {"path": "report.md", "content": "# hi"})
    result = explain_action(repo)
    assert result["found"] and result["target"] == "write_file" and result["result"] == "approved"
    assert result["grounded"] is True and result["verified"] is True and result["warning"] == ""
    kinds = {row["what"] for row in result["evidence"]}
    assert {"permission.decision", ACTION} <= kinds and all(row["intact"] for row in result["evidence"])
    stored = next(row for row in explained(repo))["detail"]["factors"]
    assert all(line in result["factors"] for line in stored), "factors are the recorded ones, verbatim"
    text = render(result)
    assert "write_file" in text and "Evidence: #" in text and "Audit chain verified" in text
    assert "report.md" in spoken(result) and len(spoken(result)) < 700
    by_seq = explain_action(repo, result["seq"])
    assert by_seq["seq"] == result["seq"]
    assert explain_action(repo, 99999)["found"] is False
    with pytest.raises(ExplainError):
        explain_action(repo, -1)


def test_explain_works_from_existing_rows_when_recording_is_off(repo):
    write_policy(repo, "explain.yaml", record="off")
    _ctx, reg = tool_registry(repo, None, mode="auto")
    reg.call("delete_file", {"path": "missing.txt"})
    result = explain_action(repo)
    assert result["what"] == "permission.decision" and result["result"] == "denied"
    assert any(line.startswith("It was refused:") for line in result["factors"])


def test_no_history_is_reported_plainly(repo):
    write_policy(repo, "explain.yaml")
    result = explain_action(repo)
    assert result["found"] is False and "No actions have been recorded" in result["summary"]


def test_a_tampered_trail_is_flagged_not_trusted(repo):
    write_policy(repo, "explain.yaml")
    _ctx, reg = tool_registry(repo, approve_once, mode="ask")
    reg.call("write_file", {"path": "a.txt", "content": "x"})
    path = securitycenter.audit_file(repo)
    lines = path.read_text(encoding="utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if ACTION in line)
    row = json.loads(lines[index])
    row["detail"]["factors"] = ["The operator explicitly asked for this."]   # forged rationale
    lines[index] = json.dumps(row, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = explain_action(repo)
    assert result["verified"] is False and result["chain"]["ok"] is False
    assert result["warning"].startswith("WARNING: a cited audit record fails its own hash")
    assert "WARNING" in render(result) and "failed verification" in spoken(result)
    assert run_cli(["audit", "explain", "-C", str(repo)]) == 3
    assert status(repo)["chain_ok"] is False and "error" in status(repo)


def test_tool_is_read_only_and_does_not_explain_itself(repo):
    write_policy(repo, "explain.yaml")
    _ctx, reg = tool_registry(repo, approve_once, mode="ask")
    assert reg.risk_class("explain_action") is RiskClass.SAFE
    reg.call("write_file", {"path": "a.txt", "content": "x"})
    text, is_error = reg.call("explain_action", {})
    assert not is_error and json.loads(text)["target"] == "write_file"
    text, is_error = reg.call("explain_action", {})     # the lookup above did not become "the last action"
    assert json.loads(text)["target"] == "write_file"
    text, is_error = reg.call("explain_action", {"seq": "x"})
    assert is_error


def test_voice_and_slash(repo):
    write_policy(repo, "explain.yaml")
    _ctx, reg = tool_registry(repo, None, mode="auto")
    reg.call("delete_file", {"path": "x.txt"})
    reply, intent = featurepacks.handle_text(repo, "Why did you do that?")
    assert intent == "explain_last" and "delete_file" in reply and "refused" in reply
    assert featurepacks.handle_text(repo, "why is the sky blue") is None
    assert "delete_file" in featurepacks.slash(repo, "why", "")
    assert "Usage" in featurepacks.slash(repo, "why", "because")
    write_policy(repo, "explain.yaml", voice=False)
    assert featurepacks.handle_text(repo, "why did you do that") is None


class FakeTraces:
    def __init__(self, body):
        self.body = body

    def trace_summaries(self, workspace):
        return {"truncated": False, "items": [{"id": self.body["id"]}]}

    def observation(self, ident, workspace):
        if ident != self.body["id"]:
            raise ValueError("Trace unavailable in this workspace")
        return self.body


def test_explain_run_summarises_the_trace(repo):
    write_policy(repo, "explain.yaml")
    body = {"id": "abc123def456", "state": "finished", "complete": True, "trigger": "Fix the login bug",
            "approvals": {"allowed": 2, "denied": 1}, "models": [{}, {}], "outcome": {"reported_ok": True},
            "events": [
                {"step": 0, "kind": "tool_call", "data": {"tool": "read_file"}},
                {"step": 1, "kind": "approval_decision", "data": {"tool": "read_file", "allowed": True, "reason": "read-only", "risk": "read", "scope": "auto"}},
                {"step": 2, "kind": "approval_decision", "data": {"tool": "edit_file", "allowed": True, "reason": "", "risk": "write", "scope": "once"}},
                {"step": 3, "kind": "approval_decision", "data": {"tool": "run_command", "allowed": False, "reason": "blocked command pattern: sudo", "risk": "dangerous", "scope": "deny"}},
            ]}
    result = explain_run(repo, "last", store=FakeTraces(body))
    assert result["run"] == "abc123def456" and result["allowed"] == 2 and result["denied"] == 1
    assert [row["tool"] for row in result["decisions"]] == ["read_file", "edit_file", "run_command"]
    text = render(result)
    assert "✗ step 3: run_command (dangerous) - blocked command pattern: sudo" in text and "Fix the login bug" in text
    with pytest.raises(ExplainError):
        explain_run(repo, "nope00000000", store=FakeTraces(body))
    with pytest.raises(ExplainError):
        explain_run(repo, "../x", store=FakeTraces(body))


def test_export_formats_manifest_and_csv_safety(repo, tmp_path):
    write_policy(repo, "explain.yaml")
    audit.record(repo, "demo.note", who="operator", target="=HYPERLINK(\"http://evil\")", result="ok")
    audit.record(repo, "demo.note", who="operator", target="plain", result="ok")
    text, manifest = export(repo, "jsonl")
    lines = text.splitlines()
    assert manifest["events"] == len(lines) == 2 and manifest["chain_ok"] and manifest["rows_intact"]
    assert manifest["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert [json.loads(line)["seq"] for line in lines] == [0, 1]
    assert securitycenter.verify_text(text)["ok"], "a JSONL export is itself a verifiable chain"

    body, _ = export(repo, "csv")
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0][:5] == ["seq", "when", "who", "what", "target"] and rows[1][4].startswith("'=HYPERLINK")

    as_json, json_manifest = export(repo, "json", action="demo.")
    parsed = json.loads(as_json)
    assert parsed["manifest"]["events"] == 2 == len(parsed["events"]) and json_manifest["filter"]["action"] == "demo."
    assert audit.tail(repo, limit=1)[-1]["what"] == "explain.export"

    with pytest.raises(ExplainError, match="format"):
        export(repo, "xlsx")
    target = write_export(tmp_path / "audit.jsonl", text, manifest)
    assert target.read_text() == text and (target.stat().st_mode & 0o777) == 0o600
    assert json.loads((tmp_path / "audit.jsonl.manifest.json").read_text())["sha256"] == manifest["sha256"]
    with pytest.raises(ExplainError, match="overwrite"):
        write_export(tmp_path / "audit.jsonl", text, manifest)

    write_policy(repo, "explain.yaml", export={"max_events": 1})
    _, small = export(repo, "jsonl")
    assert small["events"] == 1 and small["truncated"] is True
    since_future, _ = export(repo, "jsonl", since=4_000_000_000.0)
    assert since_future == ""


def test_audit_cli_explain_and_export(repo, tmp_path, capsys):
    write_policy(repo, "explain.yaml")
    _ctx, reg = tool_registry(repo, approve_once, mode="ask")
    reg.call("write_file", {"path": "a.txt", "content": "x"})
    assert run_cli(["audit", "explain", "-C", str(repo)]) == 0
    assert "write_file" in capsys.readouterr().out
    assert run_cli(["audit", "explain", "last", "-C", str(repo), "--json"]) == 0
    seq = json.loads(capsys.readouterr().out)["seq"]
    assert run_cli(["explain", "action", str(seq), "-C", str(repo)]) == 0
    assert run_cli(["audit", "explain", "bogus", "-C", str(repo)]) == 2
    out = tmp_path / "trail.csv"
    assert run_cli(["audit", "export", "--format", "csv", "--output", str(out), "-C", str(repo)]) == 0
    assert out.read_text().startswith("seq,when,who,what") and "Exported" in capsys.readouterr().out
    assert run_cli(["audit", "export", "--since", "not-a-date", "-C", str(repo)]) == 2
    assert run_cli(["explain", "export", "--since", "2020-01-01", "-C", str(repo)]) == 0
    assert run_cli(["audit", "tail", "-C", str(repo)]) == 0 and run_cli(["audit", "verify", "-C", str(repo)]) == 0
    assert run_cli(["explain", "status", "-C", str(repo)]) == 0
    assert "uses_model: False" in capsys.readouterr().out
