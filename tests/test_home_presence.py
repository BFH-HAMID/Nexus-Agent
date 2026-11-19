"""Task 22 - home presence: coarse signals, debounced state machine, closed rule actions."""
from __future__ import annotations

import json
import time

import pytest

from nexus import audit, featurepacks
from nexus.policyfile import PolicyError
from nexus.presence import hooks, policy, signals, status
from nexus.presence.engine import BRIEF_TEXT, PresenceEngine, PresenceError
from nexus.types import RiskClass

from .featurepack_helpers import run_cli, tool_registry, write_policy

T0 = 1_800_000_000.0


class World:
    """Injectable clock + signal sources."""

    def __init__(self):
        self.now = T0
        self.idle: float | None = 0.0
        self.macs: set[str] | None = set()
        self.done: list[tuple[str, str]] = []

    def engine(self, repo, **kwargs):
        actions = {name: (lambda rule, name=name: self.done.append((rule["id"], name)) or "ok") for name in policy.ACTIONS}
        return PresenceEngine(repo, clock=lambda: self.now, idle_probe=lambda: self.idle,
                              neighbor_reader=lambda: self.macs, actions=kwargs.pop("actions", actions),
                              localtime=kwargs.pop("localtime", time.gmtime), **kwargs)


@pytest.fixture()
def world():
    return World()


def enable(repo, rules=None, **settings):
    write_policy(repo, "presence.yaml", **{"debounce_seconds": 0, **settings})
    if rules is not None:
        write_policy(repo, "presence_rules.yaml", rules=rules)


def test_presence_is_off_by_default(repo):
    assert status(repo)["enabled"] is False
    with pytest.raises(PolicyError, match="OFF"):
        PresenceEngine(repo)
    _ctx, reg = tool_registry(repo)
    assert "presence_status" not in reg.names()
    assert featurepacks.handle_text(repo, "I'm home") is None
    assert "OFF" in featurepacks.slash(repo, "presence", "")
    assert hooks.daemon_tick(repo) is None and hooks.vision_event(repo, {"kind": "owner_greeting"}) is False
    assert run_cli(["presence", "tick", "-C", str(repo)]) == 2


@pytest.mark.parametrize("settings,message", [
    ({"idle_after_seconds": 900, "away_after_seconds": 600}, "greater than"),
    ({"lan": {"devices": ["aa:bb:cc:dd:ee:ff"]}}, "never raw addresses"),
    ({"signals": {"gps": True}}, "unknown setting"),
    ({"actions": {"max_per_hour": 999}}, "between"),
])
def test_policy_is_strict(repo, settings, message):
    write_policy(repo, "presence.yaml", **settings)
    with pytest.raises(PolicyError, match=message):
        policy.load(repo)


@pytest.mark.parametrize("rule,message", [
    ({"id": "a", "when": "arrive", "do": "run_command", "text": "rm -rf /"}, "do must be one of"),
    ({"id": "a", "when": "sunset", "do": "notify", "text": "x"}, "when must be one of"),
    ({"id": "a", "when": "arrive", "do": "notify"}, "needs text"),
    ({"id": "a", "when": "arrive", "do": "brief", "tool": "write_file"}, "unknown setting"),
    ({"id": "A B", "when": "arrive", "do": "brief"}, "unique id"),
    ({"id": "a", "when": "arrive", "do": "brief", "between": "25:00-26:00"}, "between must look like"),
    ({"id": "a", "when": "arrive", "do": "brief", "cooldown_minutes": -1}, "between 0 and 1440"),
])
def test_rules_accept_only_the_closed_action_set(repo, rule, message):
    enable(repo, rules=[rule])
    with pytest.raises(PolicyError, match=message):
        policy.rules(repo)


def test_input_idle_drives_present_idle_away(repo, world):
    enable(repo, signals={"input_idle": True}, idle_after_seconds=300, away_after_seconds=1800)
    engine = world.engine(repo)
    assert engine.status()["state"] == "unknown"
    assert engine.tick()["state"] == "present"
    world.now += 400
    world.idle = 400.0
    assert engine.tick()["state"] == "idle"
    world.now += 1500
    world.idle = 1900.0
    assert engine.tick()["state"] == "away"
    world.now += 10
    world.idle = 1.0
    assert engine.tick()["state"] == "present"
    assert [(row["from"], row["to"]) for row in engine.history()] == [
        ("unknown", "present"), ("present", "idle"), ("idle", "away"), ("away", "present")]
    assert set(engine.history()[0]) == {"at", "from", "to", "source"}, "history is content-free"


def test_debounce_ignores_a_brief_flicker(repo, world):
    enable(repo, signals={"input_idle": True}, debounce_seconds=20)
    engine = world.engine(repo)
    engine.tick()
    world.now += 20
    assert engine.tick()["state"] == "present"       # unknown -> present after the hold
    world.now += 400
    world.idle = 400.0
    assert engine.tick()["state"] == "present" and engine.status()["pending"] == "idle"
    world.now += 5
    world.idle = 0.0                                  # the user moved the mouse again
    assert engine.tick()["state"] == "present" and engine.status()["pending"] is None
    world.now += 400
    world.idle = 400.0
    engine.tick()
    world.now += 25
    world.idle = 425.0
    assert engine.tick()["state"] == "idle"


def test_unavailable_signal_is_reported_never_read_as_away(repo, world):
    enable(repo, signals={"input_idle": True})
    world.idle = None
    engine = world.engine(repo)
    view = engine.tick()
    assert view["state"] == "unknown" and view["unavailable"] == ["input_idle"]
    assert "unavailable on this machine" in status(repo)["error"]


def test_lan_devices_are_hashed_and_matched_passively(repo, world):
    enable(repo)
    mac = "AA-BB-CC-0D-EE-F1"
    digest = signals.device_hash(repo, mac)
    assert len(digest) == 32 and digest == signals.device_hash(repo, "aa:bb:cc:d:ee:f1")
    enable(repo, signals={"lan_devices": True}, lan={"devices": [digest]})
    engine = world.engine(repo)
    world.macs = {"11:22:33:44:55:66"}
    assert engine.tick()["state"] == "unknown"
    world.macs = {"aa:bb:cc:0d:ee:f1"}
    assert engine.tick()["state"] == "idle", "a phone on the network means around, not at the keyboard"
    world.macs = set()
    world.now += 1900
    assert engine.tick()["state"] == "away"
    stored = json.dumps(json.loads(engine.path.read_text())) + (repo / "presence.yaml").read_text()
    assert "aa:bb:cc" not in stored.lower() and "AA-BB" not in stored
    arp = "IP address  HW type Flags HW address Mask Device\n192.168.1.7 0x1 0x2 aa:bb:cc:0d:ee:f1 * wlan0\n192.168.1.9 0x1 0x0 00:00:00:00:00:00 * wlan0\n"
    assert signals.parse_macs(arp) == {"aa:bb:cc:0d:ee:f1"}
    with pytest.raises(ValueError):
        signals.device_hash(repo, "not-a-mac")


def test_manual_marks_are_immediate_and_expire(repo, world):
    enable(repo, manual_hold_minutes=60, debounce_seconds=30)
    engine = world.engine(repo)
    assert engine.set_manual("away")["state"] == "away"
    assert engine.set_manual("home")["state"] == "present"
    world.now += 3700                                   # the mark expired; the home mark is now old evidence
    assert engine.tick()["manual"] == "auto"
    with pytest.raises(PresenceError):
        engine.set_manual("vacation")
    write_policy(repo, "presence.yaml", signals={"manual": False})
    with pytest.raises(PresenceError, match="signals.manual"):
        world.engine(repo).set_manual("home")


def test_rules_fire_closed_actions_with_conditions(repo, world):
    enable(repo, rules=[
        {"id": "welcome", "when": "arrive", "do": "brief", "after_away_minutes": 30, "cooldown_minutes": 0},
        {"id": "phone", "when": "leave", "do": "mark_away", "cooldown_minutes": 0},
        {"id": "desk", "when": "arrive", "do": "mark_present", "cooldown_minutes": 0},
        {"id": "night", "when": "arrive", "do": "notify", "text": "Lights are off.", "between": "22:00-06:00",
         "cooldown_minutes": 0},
    ])
    engine = world.engine(repo)
    engine.set_manual("home")
    assert world.done == [], "unknown -> present at start-up is not an arrival"
    engine.set_manual("away")
    assert world.done == [("phone", "mark_away")]
    world.now += 10 * 60
    fired = engine.set_manual("home")["fired"]
    assert ("desk", "mark_present") in world.done and ("welcome", "brief") not in world.done
    assert next(row for row in fired if row["rule"] == "welcome")["detail"] == "away for less than after_away_minutes"
    assert next(row for row in fired if row["rule"] == "night")["detail"] == "outside the rule's time window"
    engine.set_manual("away")
    world.now += 45 * 60
    engine.set_manual("home")
    assert ("welcome", "brief") in world.done
    actions = [row["what"] for row in audit.tail(repo, limit=40)]
    assert "presence.action" in actions and "presence.transition" in actions and audit.verify(repo)["ok"]


def test_time_window_wraps_midnight(repo, world):
    enable(repo, rules=[{"id": "night", "when": "arrive", "do": "propose", "text": "Lock the door?",
                         "between": "22:00-06:00", "cooldown_minutes": 0}])
    late = time.struct_time((2027, 1, 15, 23, 30, 0, 4, 15, 0))
    engine = world.engine(repo, localtime=lambda _now: late, actions={})
    engine.set_manual("away")
    engine.set_manual("home")
    proposals = engine.proposals()
    assert [row["text"] for row in proposals] == ["Lock the door?"] and engine.status()["proposals"] == 1
    assert engine.dismiss(proposals[0]["id"]) == 1 and engine.proposals() == []


def test_cooldown_hourly_cap_and_emergency_stop(repo, world, monkeypatch):
    enable(repo, actions={"max_per_hour": 2}, rules=[
        {"id": "ping", "when": "leave", "do": "mark_away", "cooldown_minutes": 0},
        {"id": "slow", "when": "leave", "do": "dnd_on", "cooldown_minutes": 600}])
    engine = world.engine(repo)
    engine.set_manual("home")
    engine.set_manual("away")
    assert world.done == [("ping", "mark_away"), ("slow", "dnd_on")]
    engine.set_manual("home")
    world.now += 60
    fired = engine.set_manual("away")["fired"]
    assert {row["rule"]: row["detail"] for row in fired} == {"ping": "hourly action limit reached", "slow": "rule cooldown"}
    assert len(world.done) == 2
    world.now += 3700
    engine.set_manual("home")
    from nexus import computer_control

    monkeypatch.setattr(computer_control.EmergencyStop, "stopped", lambda self: True)
    fired = engine.set_manual("away")["fired"]
    assert fired[0]["detail"] == "emergency stop latched" and len(world.done) == 2
    assert engine.status()["state"] == "away", "the state is still tracked while actions are stopped"


def test_real_actions_touch_only_notification_routing(repo, world):
    from nexus.notifications import presence_state

    enable(repo, rules=[
        {"id": "here", "when": "arrive", "do": "mark_present", "cooldown_minutes": 0},
        {"id": "gone", "when": "leave", "do": "mark_away", "cooldown_minutes": 0},
        {"id": "hello", "when": "arrive", "do": "brief", "cooldown_minutes": 0}])
    engine = world.engine(repo, actions={})
    engine.set_manual("home")
    engine.set_manual("away")
    assert presence_state(repo, now=world.now)["present"] is False
    fired = engine.set_manual("home")["fired"]
    assert presence_state(repo, now=world.now)["present"] is True
    hello = next(row for row in fired if row["rule"] == "hello")
    assert hello["ok"] and "disabled" in hello["detail"], "notifications stay governed by autonomy.yaml"
    assert BRIEF_TEXT.startswith("Welcome back")


def test_a_failing_action_does_not_stop_tracking(repo, world):
    enable(repo, rules=[{"id": "boom", "when": "leave", "do": "dnd_on", "cooldown_minutes": 0}])

    def explode(_rule):
        raise RuntimeError("dnd backend down")

    engine = world.engine(repo, actions={"dnd_on": explode})
    engine.set_manual("home")
    fired = engine.set_manual("away")["fired"]
    assert fired == [{"rule": "boom", "action": "dnd_on", "ok": False, "detail": "RuntimeError: dnd backend down"}]
    assert engine.status()["state"] == "away"


def test_invalid_rules_file_fires_nothing(repo, world):
    enable(repo, rules=[{"id": "x", "when": "leave", "do": "format_disk"}])
    engine = world.engine(repo)
    engine.set_manual("home")
    fired = engine.set_manual("away")["fired"]
    assert fired[0]["ok"] is False and "presence_rules.yaml invalid" in fired[0]["detail"] and world.done == []


def test_vision_hook_and_daemon_tick(repo, world):
    enable(repo, signals={"vision": True})
    assert hooks.vision_event(repo, {"kind": "object", "label": "cup"}) is False
    assert hooks.vision_event(repo, {"kind": "unknown_person"}) is False
    assert hooks.vision_event(repo, {"kind": "owner_greeting", "location": "desk"}) is True
    assert hooks.vision_event(repo, {"kind": "object", "label": "person"}) is True
    tick = hooks.daemon_tick(repo)
    assert tick == {"state": "present", "fired": []}
    assert "desk" not in PresenceEngine(repo).path.read_text(), "no camera detail is stored"
    write_policy(repo, "presence.yaml", signals={"vision": False})
    assert hooks.vision_event(repo, {"kind": "owner_greeting"}) is False


def test_history_is_bounded_and_forgettable(repo, world):
    enable(repo, history={"keep_events": 10, "keep_days": 1})
    engine = world.engine(repo)
    for index in range(30):
        world.now += 60
        engine.set_manual("home" if index % 2 else "away")
    assert len(engine.history(100)) == 10
    world.now += 3 * 86_400
    engine.set_manual("home")
    assert len(engine.history(100)) <= 1, "events older than keep_days are dropped"
    engine.forget()
    assert engine.history() == [] and engine.status()["state"] == "present"


def test_tool_is_read_only_and_coarse(repo, world):
    enable(repo)
    world.engine(repo).set_manual("home")
    _ctx, reg = tool_registry(repo)
    assert reg.risk_class("presence_status") is RiskClass.SAFE
    text, is_error = reg.call("presence_status", {})
    assert not is_error and set(json.loads(text)) == {"state", "for_seconds", "manual"}
    assert not [name for name in reg.names() if name.startswith("presence_") and name != "presence_status"]


def test_voice_commands(repo):
    enable(repo)
    reply, intent = featurepacks.handle_text(repo, "I'm leaving", {"source": "voice"})
    assert intent == "presence_set" and "away" in reply
    reply, intent = featurepacks.handle_text(repo, "Nexus, I am home")
    assert reply.startswith("Welcome home.")
    reply, intent = featurepacks.handle_text(repo, "am I home?")
    assert intent == "presence_status" and "present" in reply
    reply, intent = featurepacks.handle_text(repo, "I'm leaving", {"speaker_verified": False})
    assert intent == "presence_refused"
    reply, intent = featurepacks.handle_text(repo, "I'm leaving", {"tainted": True})
    assert intent == "presence_refused"
    assert featurepacks.handle_text(repo, "I'm home alone and bored, tell me a joke") is None
    assert "Presence: present" in featurepacks.slash(repo, "presence", "home")
    assert "Usage" in featurepacks.slash(repo, "presence", "dance")


def test_cli(repo, capsys):
    enable(repo)
    assert run_cli(["presence", "set", "away", "-C", str(repo)]) == 0
    capsys.readouterr()
    assert run_cli(["presence", "status", "-C", str(repo), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["state"] == "away" and shown["manual"] == "away" and shown["rules_enabled"] is False
    assert run_cli(["presence", "tick", "-C", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "away"
    assert run_cli(["presence", "watch", "--count", "1", "--interval", "1", "-C", str(repo)]) == 0
    assert run_cli(["presence", "history", "-C", str(repo)]) == 0
    assert "unknown -> away" in capsys.readouterr().out
    assert run_cli(["presence", "device-hash", "aa:bb:cc:dd:ee:ff", "-C", str(repo)]) == 0
    assert len(capsys.readouterr().out.strip()) == 32
    assert run_cli(["presence", "device-hash", "nope", "-C", str(repo)]) == 2
    assert run_cli(["presence", "set", "moon", "-C", str(repo)]) == 2
    assert run_cli(["presence", "rules", "-C", str(repo)]) == 0
    assert run_cli(["presence", "forget", "-C", str(repo)]) == 0
