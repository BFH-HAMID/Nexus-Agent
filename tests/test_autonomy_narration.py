"""Offline tests for TASK 6: autonomy risk classes, voice 2FA approvals, live status narration, grounded Q&A, and notification routing."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from nexus.autonomy import (
    RiskClass,
    classify_action_risk,
    classify_tool_risk,
    load_autonomy_config,
    load_autonomy_dict,
)
from nexus.chief import (
    _ACTIVE_RUNTIMES,
    ChiefOfStaff,
    Mission,
    MissionStep,
    _MissionRuntime,
    _runtime_key,
    answer_status_question,
    save_mission,
)
from nexus.cli import main
from nexus.config import load_config
from nexus.notifications import (
    NotificationRouter,
    clear_user_presence,
    is_user_present,
    mark_user_present,
)
from nexus.permissions import Permissions
from nexus.tools import (
    ApprovalGate,
    PathGuard,
    ToolContext,
    build_tools,
)
from nexus.types import EventType, Risk, ToolSpec
from nexus.voice.approvals import SpokenApprovalIntent, SpokenApprovalManager
from nexus.voice.audio_io import AudioClip, AudioOutputPlayer, float32_to_pcm16
from nexus.voice.config import TurnDetectionConfig, VADConfig, VoiceConfig, WakeConfig
from nexus.voice.pipeline import VoicePipeline
from nexus.voice.stt import TranscriptResult
from nexus.voice.tts import TTSProvider
from nexus.voice.vad import EnergyVAD

from .conftest import FakeProvider, fake_registry, finish_completion


@pytest.fixture(autouse=True)
def _isolated_nexus_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "nexus-home"))


def _write_chief(repo: Path, **overrides) -> None:
    data = {
        "schema": 1,
        "enabled": True,
        "concurrency": 2,
        "max_retries": 0,
        "retry_backoff_seconds": 0.0,
        "max_cost_usd": 5.0,
        "critic_enabled": False,
        "auto_start_autonomy_levels": ["L2", "L3"],
        "require_approval_for_high_risk": True,
        **overrides,
    }
    (repo / "chief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _make_fake_registry(repo: Path, completions: list):
    cfg = load_config(repo)
    cfg.settings.workspace = repo
    cfg.settings.prefer_local = False
    providers = fake_registry(
        FakeProvider("gemini", list(completions)),
        FakeProvider("hermes", list(completions)),
        FakeProvider("hermes-big", list(completions)),
        FakeProvider("deepseek", list(completions)),
        FakeProvider("gemini-big", list(completions)),
        FakeProvider("deepseek-pro", list(completions)),
        config=cfg,
    )
    return cfg, providers


# --------------------------------------------------------------------------- #
# 1. Autonomy YAML policy, risk classification & unknown tool defaults
# --------------------------------------------------------------------------- #


def test_shipped_autonomy_yaml_defaults_keep_new_runtime_capabilities_off():
    repo_root = Path(__file__).resolve().parent.parent
    cfg = load_autonomy_config(repo_root)
    assert cfg.risk_classes.SAFE == "automatic"
    assert cfg.risk_classes.MEDIUM == "automatic_notify"
    assert cfg.risk_classes.DANGEROUS == "fresh_approval"
    assert cfg.status_narration.enabled is False
    assert cfg.notifications.enabled is False


def test_autonomy_yaml_rejects_dangerous_downgrade_secrets_and_unknown_keys(repo: Path):
    (repo / "autonomy.yaml").write_text(
        yaml.safe_dump({"schema": 1, "risk_classes": {"DANGEROUS": "automatic"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="fresh_approval"):
        load_autonomy_dict(repo)

    (repo / "autonomy.yaml").write_text(
        yaml.safe_dump({"schema": 1, "status_narration": {"enabled": True, "verbosity": "loud"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="verbosity"):
        load_autonomy_dict(repo)

    (repo / "autonomy.yaml").write_text(
        yaml.safe_dump({"schema": 1, "notifications": {"token": "xoxb-real-inline-secret"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_autonomy_dict(repo)

    (repo / "autonomy.yaml").write_text(
        yaml.safe_dump({"schema": 1, "unknown_key": True}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_autonomy_dict(repo)


def test_every_registered_tool_declares_risk_and_unknown_defaults_to_dangerous(repo: Path):
    ctx = ToolContext(repo, PathGuard(repo), ApprovalGate(mode="auto"))
    reg = build_tools(ctx)

    for name in reg.names():
        spec = reg._tools[name].spec
        assert spec.risk_declared is True
        assert isinstance(spec.risk_class, RiskClass)

    # Unknown ToolSpec / unknown name / unregistered registry lookup -> DANGEROUS
    unknown_spec = ToolSpec(
        name="launch_satellites",
        description="unknown",
        input_schema={"type": "object"},
    )
    assert unknown_spec.risk is Risk.DANGEROUS
    assert unknown_spec.risk_declared is False
    assert unknown_spec.risk_class is RiskClass.DANGEROUS
    assert classify_tool_risk("launch_satellites") is RiskClass.DANGEROUS
    assert reg.risk_class("launch_satellites") is RiskClass.DANGEROUS


def test_action_risk_classification_matrix(repo: Path):
    ctx = ToolContext(repo, PathGuard(repo), ApprovalGate(mode="auto"))
    reg = build_tools(ctx)

    assert reg.risk_class("read_file", {"path": "README.md"}) is RiskClass.SAFE
    assert reg.risk_class("grep", {"pattern": "TODO"}) is RiskClass.SAFE
    assert reg.risk_class("git", {"args": ["status"]}) is RiskClass.SAFE
    assert reg.risk_class("gh", {"args": ["pr", "list"]}) is RiskClass.SAFE

    assert reg.risk_class("write_file", {"path": "app/main.py", "content": "x = 1\n"}) is RiskClass.MEDIUM
    assert reg.risk_class("edit_file", {"path": "app/main.py", "old_text": "a", "new_text": "b"}) is RiskClass.MEDIUM
    assert reg.risk_class("run_command", {"command": "pytest -q"}) is RiskClass.MEDIUM
    assert reg.risk_class("git", {"args": ["checkout", "-b", "feat/task6"]}) is RiskClass.MEDIUM
    assert reg.risk_class("git_prepare", {"task": "Add helper", "push": False}) is RiskClass.MEDIUM
    assert reg.risk_class("gh", {"args": ["pr", "create", "--draft"]}) is RiskClass.MEDIUM

    assert reg.risk_class("delete_file", {"path": "app/main.py"}) is RiskClass.DANGEROUS
    assert reg.risk_class("gh_merge_pr", {"number": 5}) is RiskClass.DANGEROUS
    assert reg.risk_class("git", {"args": ["push", "origin", "main"]}) is RiskClass.DANGEROUS
    assert reg.risk_class("git", {"args": ["branch", "-D", "main"]}) is RiskClass.DANGEROUS
    assert reg.risk_class("git_prepare", {"task": "Ship", "push": True}) is RiskClass.DANGEROUS
    assert reg.risk_class("run_command", {"command": "pytest -q && rm -rf /"}) is RiskClass.DANGEROUS
    assert reg.risk_class("write_file", {"path": "permissions.yaml", "content": "default: AUTO_ALLOW"}) is RiskClass.DANGEROUS
    assert reg.risk_class("write_file", {"path": "autonomy.yaml", "content": "level: L3"}) is RiskClass.DANGEROUS
    assert reg.risk_class("mcp_github_issue", {}) is RiskClass.DANGEROUS

    draft_spec = ToolSpec(name="draft_summary", description="draft", input_schema={"type": "object"}, risk=Risk.READ)
    assert classify_action_risk(draft_spec, {}) is RiskClass.SAFE
    assert classify_action_risk("deploy_service", {}) is RiskClass.DANGEROUS

    # Operator control prevents model tools from editing autonomy.yaml even under AUTO_ALLOW
    (repo / "permissions.yaml").write_text("schema: 1\ndefault: AUTO_ALLOW\n", encoding="utf-8")
    op_gate = ApprovalGate(mode="auto")
    op_gate.permissions = Permissions(repo)
    op_reg = build_tools(ToolContext(repo, PathGuard(repo), op_gate))
    out, err = op_reg.call("write_file", {"path": "autonomy.yaml", "content": "level: L3\n"})
    assert err and not (repo / "autonomy.yaml").exists()


def test_approval_gate_enforces_risk_classes_and_medium_post_notifications(repo: Path):
    (repo / "permissions.yaml").write_text("schema: 1\ndefault: AUTO_ALLOW\n", encoding="utf-8")
    (repo / "autonomy.yaml").write_text(
        yaml.safe_dump({
            "schema": 1,
            "level": "L2",
            "risk_classes": {"SAFE": "automatic", "MEDIUM": "automatic_notify", "DANGEROUS": "fresh_approval"},
        }),
        encoding="utf-8",
    )

    events = []
    medium_notices: list[str] = []
    gate = ApprovalGate(
        mode="auto",
        allow_dangerous=True,
        always_allow=["delete_file", "unknown_danger_tool"],
        emit=events.append,
        medium_notify_callback=lambda spec, _args, msg: medium_notices.append(f"{spec.name}:{msg}"),
    )
    gate.permissions = Permissions(repo)
    reg = build_tools(ToolContext(repo, PathGuard(repo), gate, emit=events.append))

    # SAFE: runs automatically with no approval prompt and no medium notice
    out, err = reg.call("read_file", {"path": "README.md"})
    assert not err
    assert not medium_notices

    # MEDIUM: runs automatically under AUTO_ALLOW and emits post-action notification
    out, err = reg.call("write_file", {"path": "app/note.py", "content": "X = 1\n"})
    assert not err and (repo / "app/note.py").exists()
    assert len(medium_notices) == 1
    assert "write_file" in medium_notices[0]
    assert any(
        e.type is EventType.STATUS and e.data.get("kind") == "medium_action"
        for e in events
    )

    # DANGEROUS / unknown tool: refuses automatic execution without fresh approval callback
    reg.register(
        "unknown_danger_tool",
        "undeclared custom tool",
        {"type": "object"},
        lambda _a: "should never run",
    )
    out, err = reg.call("unknown_danger_tool", {})
    assert err and "fresh human approval required" in out

    # Tightening MEDIUM -> fresh_approval in autonomy.yaml blocks auto-execution
    (repo / "autonomy.yaml").write_text(
        yaml.safe_dump({
            "schema": 1,
            "risk_classes": {"SAFE": "automatic", "MEDIUM": "fresh_approval", "DANGEROUS": "fresh_approval"},
        }),
        encoding="utf-8",
    )
    out, err = reg.call("write_file", {"path": "app/blocked.py", "content": "Y = 2\n"})
    assert err and "fresh human approval required" in out
    assert not (repo / "app/blocked.py").exists()


# --------------------------------------------------------------------------- #
# 2. Voice approvals: exact action description, wait, & Task 2 second-factor
# --------------------------------------------------------------------------- #


def test_spoken_approval_states_exact_action_handles_wait_and_enforces_2fa_on_dangerous(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("NEXUS_APPROVAL_PIN", "948271")
    (repo / "permissions.yaml").write_text(
        yaml.safe_dump({
            "schema": 1,
            "default": "ASK_EVERY_TIME",
            "second_factor": {
                "enabled": True,
                "method": "pin",
                "pin_env": "NEXUS_APPROVAL_PIN",
                "risks": ["dangerous"],
            },
        }),
        encoding="utf-8",
    )

    prompts: list[str] = []
    replies = iter([
        ("wait", None),
        ("yes do it", None),
    ])
    mgr = SpokenApprovalManager(
        workspace=repo,
        speak=lambda txt, _lang: prompts.append(txt),
        listen_for_reply=lambda: next(replies),
        pin_callback=lambda _msg: "000000",  # wrong PIN first!
    )

    danger_spec = ToolSpec(
        name="delete_file",
        description="Delete a workspace file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        risk=Risk.DANGEROUS,
    )
    denied = mgr.request_approval(danger_spec, {"path": "app/secret.py"}, Risk.DANGEROUS, "app/secret.py")
    assert denied.approved is False
    assert any("DANGEROUS" in p and "app/secret.py" in p and "wait" in p.lower() for p in prompts)
    assert any("waiting" in p.lower() for p in prompts)
    assert any("second-factor" in p.lower() for p in prompts)

    # Now supply correct PIN -> single-use approval granted on DANGEROUS action
    ok_replies = iter([("yes do it", None)])
    mgr_ok = SpokenApprovalManager(
        workspace=repo,
        speak=lambda txt, _lang: prompts.append(txt),
        listen_for_reply=lambda: next(ok_replies),
        pin_callback=lambda _msg: "948271",
    )
    approved = mgr_ok.request_approval(danger_spec, {"path": "app/secret.py"}, Risk.DANGEROUS, "app/secret.py")
    assert approved.approved is True
    assert approved.scope == "once"
    assert approved.second_factor_verified is True
    assert "pin verified" in approved.reason.lower()

    # MEDIUM action does NOT require DANGEROUS-only PIN
    med_replies = iter([("yes do it", None)])
    mgr_med = SpokenApprovalManager(
        workspace=repo,
        speak=lambda txt, _lang: prompts.append(txt),
        listen_for_reply=lambda: next(med_replies),
        pin_callback=lambda _msg: (_ for _ in ()).throw(AssertionError("PIN should not be requested for MEDIUM")),
    )
    med_spec = ToolSpec(
        name="edit_file",
        description="Edit file",
        input_schema={"type": "object"},
        risk=Risk.WRITE,
    )
    med_ok = mgr_med.request_approval(med_spec, {"path": "app/main.py"}, Risk.WRITE, "app/main.py")
    assert med_ok.approved is True
    assert med_ok.risk_class == "MEDIUM"

    # VoicePipeline wires workspace into SpokenApprovalManager and keeps 'wait' pending cleanly
    pipeline = VoicePipeline(VoiceConfig(enabled=False), workspace=repo)
    assert pipeline.approval_manager.workspace == repo
    pipeline.approval_manager.pin_callback = lambda _msg: "948271"
    from nexus.voice.approvals import PendingSpokenApproval
    pipeline.approval_manager.pending = PendingSpokenApproval(
        spec=danger_spec,
        args={"path": "app/secret.py"},
        risk=Risk.DANGEROUS,
        preview="app/secret.py",
    )
    intent_wait, _ = pipeline.approval_manager.submit_intent("wait")
    assert intent_wait is SpokenApprovalIntent.WAIT
    assert pipeline.approval_manager.pending is not None
    intent_go, _ = pipeline.approval_manager.submit_intent("yes do it")
    assert intent_go is SpokenApprovalIntent.APPROVE


# --------------------------------------------------------------------------- #
# 3. Live status narration verbosity (silent / milestones / chatty)
# --------------------------------------------------------------------------- #


def test_chief_narration_respects_verbosity_levels_and_redacts_secrets(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_chief(repo)
    (repo / "permissions.yaml").write_text("schema: 1\ndefault: AUTO_ALLOW\n", encoding="utf-8")
    (repo / "autonomy.yaml").write_text(
        yaml.safe_dump({
            "schema": 1,
            "level": "L2",
            "status_narration": {"enabled": True, "verbosity": "milestones", "cooldown_seconds": 0.0},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-narration-value-999999999")

    steps = [
        {"id": "design", "role": "design", "read_only": True, "risk_level": "low", "instruction": "design"},
        {"id": "qa", "role": "qa", "depends_on": ["design"], "read_only": True, "risk_level": "low", "instruction": "qa"},
    ]

    # 1. silent: zero narration lines
    silent_spoken: list[str] = []
    silent_events: list = []
    cfg, providers = _make_fake_registry(
        repo,
        [finish_completion("Design complete."), finish_completion("QA complete.")],
    )
    chief_silent = ChiefOfStaff(
        repo,
        config=cfg,
        providers=providers,
        speak=silent_spoken.append,
        emit=silent_events.append,
        verbosity="silent",
    )
    m_silent = chief_silent.run("Build module", steps=steps, auto_approve=True)
    assert m_silent.status == "done"
    assert not any(
        e.type is EventType.STATUS and e.data.get("chief_event") == "narration"
        for e in silent_events
    )
    # Only the mandatory plan summary may be spoken when silent
    assert all("Done " not in line and "Starting " not in line for line in silent_spoken)

    # 2. milestones: step completions + mission start/finish, NO detail chatter
    mile_spoken: list[str] = []
    mile_events: list = []
    cfg2, providers2 = _make_fake_registry(
        repo,
        [finish_completion("Design complete."), finish_completion("QA complete.")],
    )
    chief_mile = ChiefOfStaff(
        repo,
        config=cfg2,
        providers=providers2,
        speak=mile_spoken.append,
        emit=mile_events.append,
    )
    m_mile = chief_mile.run("Build module", steps=steps, auto_approve=True)
    assert m_mile.status == "done"
    narr_mile = [e.data for e in mile_events if e.type is EventType.STATUS and e.data.get("chief_event") == "narration"]
    assert len(narr_mile) >= 4
    assert all(item["level"] == "milestone" for item in narr_mile)
    assert any("Done design" in item["message"] for item in narr_mile)
    assert not any("Starting design (" in item["message"] for item in narr_mile)

    # 3. chatty: emits BOTH milestone AND detail lines at step boundaries
    chatty_events: list = []
    cfg3, providers3 = _make_fake_registry(
        repo,
        [finish_completion("Design complete."), finish_completion("QA complete.")],
    )
    chief_chatty = ChiefOfStaff(
        repo,
        config=cfg3,
        providers=providers3,
        emit=chatty_events.append,
        verbosity="chatty",
    )
    m_chatty = chief_chatty.run("Build module", steps=steps, auto_approve=True)
    assert m_chatty.status == "done"
    narr_chatty = [e.data for e in chatty_events if e.type is EventType.STATUS and e.data.get("chief_event") == "narration"]
    assert any(item["level"] == "detail" and "Starting design" in item["message"] for item in narr_chatty)
    assert any(item["level"] == "milestone" and "Done design" in item["message"] for item in narr_chatty)
    assert all("sk-secret-narration-value" not in item["message"] for item in narr_chatty)


# --------------------------------------------------------------------------- #
# 4. Grounded live status Q&A (live memory + logs, never guessed)
# --------------------------------------------------------------------------- #


def test_answer_status_question_reads_live_state_and_logs_never_guesses(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
):
    assert "No active or recent missions" in answer_status_question(repo, "what is the status?")

    # Seed a failed mission with concrete error & logs
    failed = Mission(
        id="chief-deadbeef01",
        goal="Deploy checkout service",
        success_criteria=["Tests pass"],
        steps=[
            MissionStep(
                id="design",
                title="design",
                role="design",
                agent="gemini",
                status="done",
                attempts=1,
            ),
            MissionStep(
                id="qa",
                title="QA",
                role="qa",
                agent="deepseek",
                status="escalated",
                attempts=3,
                error="pytest failed: AssertionError in test_checkout.py",
            ),
        ],
        status="escalated",
        total_cost_usd=0.042,
        cost_cap_usd=2.0,
        error="pytest failed: AssertionError in test_checkout.py",
    )
    failed.log("step.escalated", step_id="qa", error="pytest failed: AssertionError in test_checkout.py")
    save_mission(repo, failed)

    why = answer_status_question(repo, "why did that fail?")
    assert "chief-deadbeef01" in why
    assert "qa" in why and "AssertionError in test_checkout.py" in why

    status_ans = answer_status_question(repo, "what is the status?")
    assert "chief-deadbeef01" in status_ans
    assert "escalated" in status_ans
    assert "1/2 steps done" in status_ans

    # Live in-memory active runtime supersedes disk snapshot when a step is actively running
    live = Mission(
        id="chief-live000001",
        goal="Implement feature X",
        success_criteria=["Clean code"],
        steps=[
            MissionStep(id="backend", title="backend", role="backend", agent="gemini", status="running"),
            MissionStep(id="qa", title="QA", role="qa", agent="deepseek", status="pending", depends_on=["backend"]),
        ],
        status="running",
        total_cost_usd=0.015,
        cost_cap_usd=2.0,
    )
    rt = _MissionRuntime(live.id)
    rt.mission = live
    key = _runtime_key(repo, live.id)
    _ACTIVE_RUNTIMES[key] = rt
    try:
        doing = answer_status_question(repo, "what are you doing?")
        assert "chief-live000001" in doing
        assert "backend" in doing and "running" in doing
        no_fail = answer_status_question(repo, "why did that fail?", mission_id=live.id)
        assert "no recorded failures" in no_fail.lower()
    finally:
        _ACTIVE_RUNTIMES.pop(key, None)

    # CLI `nexus chief ask`
    rc = main(["chief", "-C", str(repo), "ask", "why did that fail?", "--id", "chief-deadbeef01", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "AssertionError in test_checkout.py" in payload["answer"]


class _FakeVoiceTTS(TTSProvider):
    name = "fake-tts"

    def __init__(self, spoken: list[str], *, workspace: Path | None = None):
        super().__init__(workspace=workspace)
        self.spoken = spoken

    def _synthesize_clean(self, text: str, *, language: str, rate: float) -> AudioClip:
        self.spoken.append(text)
        return AudioClip(float32_to_pcm16([0.1] * 160), "pcm")


class _SilentPlayer(AudioOutputPlayer):
    def play(self, clip: AudioClip, interrupt=None) -> bool:
        return bool(interrupt is not None and interrupt.is_set())


def test_voice_pipeline_handles_live_status_questions_and_marks_presence(repo: Path):
    _write_chief(repo)
    m = Mission(
        id="chief-aabbccdd11",
        goal="Refactor auth",
        success_criteria=["Tests green"],
        steps=[
            MissionStep(id="backend", title="backend", role="backend", agent="gemini", status="failed", error="SyntaxError in auth.py")
        ],
        status="failed",
        error="SyntaxError in auth.py",
    )
    m.log("step.exception", step_id="backend", error="SyntaxError in auth.py")
    save_mission(repo, m)

    spoken: list[str] = []
    vad = EnergyVAD()
    stt = SimpleNamespace(transcribe_stream=lambda *_a, **_k: TranscriptResult(text="why did that fail?", language="en"))
    tts = _FakeVoiceTTS(spoken, workspace=repo)
    player = _SilentPlayer()

    cfg = VoiceConfig(
        enabled=True,
        wake=WakeConfig(enabled=False, engine="disabled"),
        vad=VADConfig(engine="energy", threshold=0.2, min_speech_ms=32, min_silence_ms=60),
        turn_detection=TurnDetectionConfig(semantic_eot=False, adaptive_silence=False),
    )
    pipeline = VoicePipeline(
        cfg,
        workspace=repo,
        vad=vad,
        stt=stt,
        tts=tts,
        player=player,
        text_handler=lambda _tr: (_ for _ in ()).throw(AssertionError("Status Q&A must not call model text_handler")),
    )
    speech_chunk = float32_to_pcm16([0.5 if i % 2 == 0 else -0.5 for i in range(512)])
    frames = [speech_chunk] * 3 + [b"\x00\x00" * 512] * 4
    res = pipeline.process_frames(frames, skip_wake=True)
    assert "SyntaxError in auth.py" in res.response_text
    assert spoken and "SyntaxError in auth.py" in spoken[0]
    assert is_user_present(repo) is True


# --------------------------------------------------------------------------- #
# 5. Notification routing by importance & user presence (off by default)
# --------------------------------------------------------------------------- #


def test_notification_router_routes_by_presence_importance_and_masks_secrets(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999999:AABBCCDDEEFFGGHHIIJJKKLLMMNNOO")

    # Off by default -> disabled
    router = NotificationRouter(repo, clock=lambda: 1000.0)
    rec_off = router.notify(importance="high", title="Status", message="Step done")
    assert rec_off.delivered is False and rec_off.channel == "disabled"

    # Enable notifications in autonomy.yaml
    (repo / "autonomy.yaml").write_text(
        yaml.safe_dump({
            "schema": 1,
            "notifications": {
                "enabled": True,
                "presence": "auto",
                "present_ttl_seconds": 300,
                "away_channel": "auto",
            },
        }),
        encoding="utf-8",
    )

    voice_log: list[str] = []
    tg_log: list[tuple[str, str]] = []
    desktop_log: list[tuple[str, str]] = []
    now = [1000.0]
    router = NotificationRouter(
        repo,
        voice_speak=voice_log.append,
        telegram_sender=lambda t, m: tg_log.append((t, m)) or True,
        desktop_notifier=lambda t, m: desktop_log.append((t, m)) or {"ok": True},
        clock=lambda: now[0],
    )

    # Present -> routes via voice (and masks secret!)
    mark_user_present(repo, channel="voice", at=now[0])
    rec_present = router.notify(
        importance="high",
        title="Done",
        message="Finished step using 999999:AABBCCDDEEFFGGHHIIJJKKLLMMNNOO",
    )
    assert rec_present.delivered is True and rec_present.channel == "voice"
    assert len(voice_log) == 1
    assert "AABBCCDD" not in voice_log[0]
    assert not tg_log

    # TTL expires / cleared -> away -> routes via Telegram
    clear_user_presence(repo)
    assert is_user_present(repo, now=now[0]) is False
    rec_away_tg = router.notify(importance="high", title="Paused", message="Mission waiting")
    assert rec_away_tg.delivered is True and rec_away_tg.channel == "telegram"
    assert len(tg_log) == 1

    # Telegram unavailable -> falls back to desktop notification
    router_desktop = NotificationRouter(
        repo,
        voice_speak=voice_log.append,
        telegram_sender=lambda _t, _m: False,
        desktop_notifier=lambda t, m: desktop_log.append((t, m)) or {"ok": True},
        clock=lambda: now[0],
    )
    rec_away_desk = router_desktop.notify(importance="critical", title="Failed", message="Step aborted")
    assert rec_away_desk.delivered is True and rec_away_desk.channel == "desktop"
    assert len(desktop_log) == 1

    # End-to-end MEDIUM tool call emits NotificationRouter notification when notifications.enabled is true
    (repo / "permissions.yaml").write_text("schema: 1\ndefault: AUTO_ALLOW\n", encoding="utf-8")
    (repo / "autonomy.yaml").write_text(
        yaml.safe_dump({
            "schema": 1,
            "level": "L2",
            "notifications": {
                "enabled": True,
                "presence": "present",
            },
        }),
        encoding="utf-8",
    )
    (repo / "voice.yaml").write_text(yaml.safe_dump({"schema": 1, "enabled": True}), encoding="utf-8")
    spoken_medium: list[str] = []
    gate = ApprovalGate(mode="auto")
    gate.permissions = Permissions(repo)
    reg = build_tools(ToolContext(repo, PathGuard(repo), gate))
    from unittest.mock import patch

    with patch("nexus.notifications.NotificationRouter._send_voice", side_effect=lambda txt: (spoken_medium.append(txt), (True, "voice"))[1]):
        out, err = reg.call("write_file", {"path": "app/notified.py", "content": "Z = 3\n"})
        assert not err and spoken_medium and "write_file" in spoken_medium[0]


def test_cli_chief_verbosity_override_emits_narration(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch):
    _write_chief(repo)
    (repo / "permissions.yaml").write_text("schema: 1\ndefault: AUTO_ALLOW\n", encoding="utf-8")
    (repo / "autonomy.yaml").write_text("schema: 1\nlevel: L2\n", encoding="utf-8")

    from .conftest import tool_completion

    script = [
        tool_completion([("write_file", {"path": "app/service.py", "content": "OK = True\n"})]),
        finish_completion("Done."),
        tool_completion([("read_file", {"path": "app/service.py"})]),
        finish_completion("Tested."),
    ]
    cfg, providers = _make_fake_registry(repo, script)
    monkeypatch.setattr("nexus.chief.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nexus.chief.build_providers", lambda *_a, **_k: providers)

    rc = main(["chief", "-C", str(repo), "Build service", "--no-critic", "--yes", "--confirm", "GO", "--verbosity", "chatty"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Starting " in out and "Done " in out
