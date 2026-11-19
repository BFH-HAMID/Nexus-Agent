"""Task 23 - gesture control: pure-Python pose classifier, hold/arming/cooldown, closed actions."""
from __future__ import annotations

import importlib.metadata
import itertools
import json
import math
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from nexus import audit, computer_control
from nexus.gesture import camera as camera_module
from nexus.gesture import policy, status
from nexus.gesture.camera import GestureWatcher, missing
from nexus.gesture.classify import Hand, classify, finger_scores
from nexus.gesture.controller import GestureController, GestureError, Sinks
from nexus.policyfile import PolicyError
from nexus.tools import ApprovalDecision
from nexus.types import Risk, ToolSpec
from nexus.voice.approvals import PendingSpokenApproval, SpokenApprovalManager

from .featurepack_helpers import run_cli, tool_registry, write_policy


# -- synthetic hands ------------------------------------------------------------------- #
def hand(*, thumb="out", index=True, middle=True, ring=True, pinky=True, rotate=0.0, scale=1.0, mirror=False):
    """21 landmarks of an upright right hand (y grows downwards), optionally rotated/scaled/mirrored."""
    points = [(0.0, 0.0)] * 21
    points[0] = (0.0, 0.0)
    bases = {"index": (5, -0.25), "middle": (9, -0.08), "ring": (13, 0.08), "pinky": (17, 0.24)}
    for name, extended in (("index", index), ("middle", middle), ("ring", ring), ("pinky", pinky)):
        start, x = bases[name]
        points[start] = (x, -1.0)
        if extended:
            points[start + 1], points[start + 2], points[start + 3] = (x, -1.35), (x, -1.65), (x, -1.9)
        else:  # curled: the tip comes back towards the palm
            points[start + 1], points[start + 2], points[start + 3] = (x, -1.25), (x, -1.05), (x, -0.8)
    points[1] = (-0.3, -0.25)
    if thumb == "out":      # spread diagonally, as in an open hand
        points[2], points[3], points[4] = (-0.5, -0.5), (-0.75, -0.7), (-0.95, -0.9)
    elif thumb == "side":   # straight out from the side of a closed hand (thumbs-up once the hand is turned)
        points[2], points[3], points[4] = (-0.55, -0.45), (-0.85, -0.5), (-1.15, -0.5)
    else:                   # tucked across the palm
        points[2], points[3], points[4] = (-0.5, -0.5), (-0.35, -0.75), (-0.2, -0.9)
    out = []
    for x, y in points:
        if mirror:
            x = -x
        xr = x * math.cos(rotate) - y * math.sin(rotate)
        yr = x * math.sin(rotate) + y * math.cos(rotate)
        out.append((0.5 + xr * 0.1 * scale, 0.6 + yr * 0.1 * scale))
    return out


OPEN = hand()
FIST = hand(thumb="in", index=False, middle=False, ring=False, pinky=False)
# A real thumbs-up is a closed hand turned on its side, so the thumb (which leaves the hand sideways) points up.
THUMBS_UP = hand(thumb="side", index=False, middle=False, ring=False, pinky=False, rotate=math.pi / 2)
THUMBS_DOWN = hand(thumb="side", index=False, middle=False, ring=False, pinky=False, rotate=-math.pi / 2)
PEACE = hand(thumb="in", ring=False, pinky=False)
POINT = hand(thumb="in", middle=False, ring=False, pinky=False)


def flip(points):
    return [(x, 1.2 - y) for x, y in points]


@pytest.mark.parametrize("landmarks,expected", [
    (OPEN, "open_palm"), (FIST, "fist"), (THUMBS_UP, "thumbs_up"), (THUMBS_DOWN, "thumbs_down"),
    (flip(THUMBS_UP), "thumbs_down"), (PEACE, "peace"), (POINT, "point_up"),
])
def test_classifier_recognises_each_pose(landmarks, expected):
    pose, confidence = classify(landmarks)
    assert pose == expected and confidence >= 0.7


def test_classifier_is_scale_mirror_and_tilt_invariant():
    for kwargs in ({"scale": 0.3}, {"scale": 3.0}, {"mirror": True}, {"rotate": 0.3}, {"rotate": -0.3}):
        assert classify(hand(**kwargs))[0] == "open_palm", kwargs
        assert classify(hand(thumb="in", index=False, middle=False, ring=False, pinky=False, **kwargs))[0] == "fist", kwargs
    assert classify([{"x": x, "y": y, "z": 0.0} for x, y in PEACE])[0] == "peace"


def test_ambiguous_and_malformed_input_is_not_a_pose():
    sideways_thumb = hand(thumb="side", index=False, middle=False, ring=False, pinky=False)
    assert classify(sideways_thumb) == ("none", 0.0), "a thumb pointing sideways is neither up nor down"
    assert classify(hand(mirror=True, thumb="side", index=False, middle=False, ring=False, pinky=False,
                         rotate=-math.pi / 2))[0] == "thumbs_up", "left hand"
    assert classify(flip(POINT))[0] != "point_up", "pointing down is not point_up"
    three = hand(thumb="in", pinky=False)
    assert classify(three) == ("none", 0.0)
    for bad in (None, [], OPEN[:20], [(0.0, 0.0)] * 21, [("a", "b")] * 21, [(float("nan"), 0.0)] * 21, "x" * 21):
        assert classify(bad) == ("none", 0.0)
    assert finger_scores(OPEN)["index"] > 0.9 and finger_scores(FIST)["index"] < -0.9


# -- controller ----------------------------------------------------------------------------- #
class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


# -- landmarks of photographed hands --------------------------------------------------------------- #
HANDS = json.loads((Path(__file__).parent / "data" / "gesture_hands.json").read_text(encoding="utf-8"))["hands"]
POSES = ("open_palm", "fist", "thumbs_up", "thumbs_down", "peace", "point_up")


def photographed(row, world=True) -> Hand:
    """A hand of the data file as the camera loop hands it to the classifier."""
    width, height = row["size"]
    return camera_module.to_hand([SimpleNamespace(x=x, y=y) for x, y in row["image"]],
                                 [SimpleNamespace(x=x, y=y, z=z) for x, y, z in row["world"]] if world else None,
                                 width, height)


def test_the_data_file_holds_what_the_tests_assume():
    shown = [row["shows"] for row in HANDS]
    assert len(HANDS) == 65 and {pose: shown.count(pose) for pose in POSES} == {
        "open_palm": 8, "fist": 8, "thumbs_up": 7, "thumbs_down": 8, "peace": 7, "point_up": 8}
    assert sum(pose not in POSES for pose in shown) == 19
    assert all(len(row["image"]) == 21 and len(row["world"]) == 21 and len(row["size"]) == 2 for row in HANDS)


def test_photographed_hands_are_recognised():
    """Every gesture is named right, and nearly all of them surely enough to count at the default threshold."""
    floor = policy.DEFAULTS["min_confidence"]
    seen = {row["photo"]: classify(photographed(row)) for row in HANDS if row["shows"] in POSES}
    wrong = {photo: seen[photo] for photo, row in ((row["photo"], row) for row in HANDS)
             if row["shows"] in POSES and seen[photo][0] != row["shows"]}
    assert wrong == {}
    unsure = sorted(photo for photo, (_pose, confidence) in seen.items() if confidence < floor)
    assert unsure == [42]                      # a thumbs-up with the fingers curled towards the camera
    assert len(seen) == 46


def test_other_hand_shapes_are_not_taken_for_a_gesture():
    """An OK sign, three or four fingers, a shaka, a hand at rest: none of them is one of the six poses."""
    for row in HANDS:
        if row["shows"] not in POSES:
            assert classify(photographed(row)) == ("none", 0.0), (row["photo"], row["shows"])


def test_no_photographed_hand_fires_the_wrong_gesture_at_any_threshold_an_operator_can_set():
    for row in HANDS:
        for world in (True, False):
            pose, confidence = classify(photographed(row, world))
            if pose != "none" and pose != row["shows"]:
                # the picture alone cannot see that a flat hand is edge-on; with world landmarks nothing is left
                assert not world and confidence < 0.95, (row["photo"], row["shows"], pose, confidence)
                assert (row["shows"], pose) == ("relaxed", "open_palm")


def test_a_fist_is_never_a_thumbs_up():
    """The gesture that stops and the gesture that may approve must not be confused, in either direction."""
    for row in HANDS:
        for world in (True, False):
            pose, confidence = classify(photographed(row, world))
            if row["shows"] == "fist":
                assert pose in ("fist", "none") and (pose == "fist" or confidence == 0.0), row["photo"]
            if row["shows"] in ("thumbs_up", "thumbs_down"):
                assert pose in (row["shows"], "none"), row["photo"]
    fists = [classify(photographed(row))[1] for row in HANDS if row["shows"] == "fist"]
    assert min(fists) >= 0.9


def test_the_frame_shape_matters_for_what_is_measured():
    """Fractions of width and height are two units. Read as one, a wide frame shortens a thumb that points sideways."""
    wide = [row for row in HANDS if row["shows"] == "open_palm" and row["size"][0] > 1.3 * row["size"][1]]
    assert len(wide) >= 3
    for row in wide:
        uncorrected = Hand([(x, y) for x, y in row["image"]])
        corrected = photographed(row, world=False)
        assert finger_scores(corrected)["thumb"] == 1.0
        assert finger_scores(corrected)["thumb"] >= finger_scores(uncorrected)["thumb"]
    assert sum(classify(photographed(row, world=False))[1] >= 0.7 for row in wide) == len(wide)
    square = camera_module.to_hand([SimpleNamespace(x=0.5, y=0.25)], None, 0, 0)
    assert square == Hand([(0.5, 0.25)], None)                          # an unknown size changes nothing


def test_world_landmarks_tell_what_the_picture_cannot():
    by_photo = {row["photo"]: row for row in HANDS}
    # an open hand seen from its edge has five extended fingers in the picture; it is not a palm shown to the camera
    edge_on = next(row for row in HANDS if row.get("view") == "palm up, seen from the side")
    assert classify(photographed(edge_on, world=False))[0] == "open_palm"
    assert classify(photographed(edge_on)) == ("none", 0.0)
    # fists far from the camera: the picture alone is unsure about the thumb, both together are not
    far = [row for row in HANDS if row["shows"] == "fist" and "small in the frame" in row.get("view", "")]
    assert len(far) >= 5
    assert min(classify(photographed(row))[1] for row in far) >= 0.9
    assert min(classify(photographed(row, world=False))[1] for row in far) < 0.5
    assert by_photo[1]["shows"] == "fist"


def test_bad_world_landmarks_are_ignored_not_trusted():
    row = next(row for row in HANDS if row["shows"] == "peace")
    hand = photographed(row)
    assert classify(hand.image, "nonsense") == classify(hand.image) == classify(hand.image, hand.world[:20])
    assert classify(hand.image, [(0.0, 0.0, 0.0)] * 21) == classify(hand.image)       # a collapsed estimate
    assert finger_scores(hand.image, [(float("nan"),) * 3] * 21) == finger_scores(hand.image)
    assert classify(Hand(hand.image, hand.world)) == classify(hand.image, hand.world) == classify(hand)


def enable(repo, bindings=None, **settings):
    write_policy(repo, "gesture.yaml", **settings)
    write_policy(repo, "gestures.yaml", bindings={"fist": "emergency_stop", "thumbs_down": "deny", **(bindings or {})})


def hold(controller, clock, pose, seconds=0.7, confidence=1.0):
    """Show a pose for ``seconds`` at 10 fps, then drop the hand; returns the events produced."""
    events = []
    for _ in range(int(seconds * 10) + 1):
        event = controller.feed(pose, confidence)
        if event:
            events.append(event)
        clock.now += 0.1
    controller.feed(None, 0.0)
    return events


@pytest.fixture()
def latch(monkeypatch, tmp_path):
    """An emergency-stop latch in a temp NEXUS_HOME (the repo fixture already isolates it; assert that)."""
    stop = computer_control.EmergencyStop()
    assert str(tmp_path) in str(stop.root) or "pytest" in str(stop.root)
    return stop


def test_gesture_is_off_by_default(repo):
    assert status(repo)["enabled"] is False
    with pytest.raises(PolicyError, match="OFF"):
        GestureController(repo)
    assert run_cli(["gesture", "bindings", "-C", str(repo)]) == 2
    assert run_cli(["gesture", "status", "-C", str(repo)]) == 0
    from nexus.gesture.runtime import attach

    assert attach(repo, object(), threading.Event()) is None


@pytest.mark.parametrize("name,settings,message", [
    ("gesture.yaml", {"approve": {"max_risk": "DANGEROUS"}}, "one of"),
    ("gesture.yaml", {"hold_ms": 10}, "between"),
    ("gesture.yaml", {"camera": {"save_frames": True}}, "unknown setting"),
    ("gestures.yaml", {"bindings": {"fist": "run_command"}}, "one of"),
    ("gestures.yaml", {"bindings": {"wave": "deny"}}, "unknown setting"),
])
def test_policies_are_strict(repo, name, settings, message):
    write_policy(repo, name, **settings)
    with pytest.raises(PolicyError, match=message):
        (policy.POLICY if name == "gesture.yaml" else policy.BINDINGS).load(repo)


def test_the_arming_pose_cannot_be_bound(repo):
    enable(repo, bindings={"open_palm": "deny"})
    with pytest.raises(PolicyError, match="arming pose"):
        GestureController(repo)
    assert "arming pose" in status(repo)["error"]


def test_no_resume_action_exists():
    assert not [name for name in policy.ACTIONS if "resume" in name or "unlatch" in name or "clear" in name]


def test_hold_release_and_confidence(repo, latch):
    enable(repo, arming={"required": False}, bindings={"peace": "focus_on"})
    clock = Clock()
    controller = GestureController(repo, clock=clock, dry_run=True)
    assert hold(controller, clock, "peace", seconds=0.3) == [], "too short"
    assert hold(controller, clock, "peace", confidence=0.4) == [], "below min_confidence"
    events = hold(controller, clock, "peace", seconds=3.0)
    assert [event["action"] for event in events] == ["focus_on"], "a held pose fires once, not every frame"
    clock.now += 5
    assert len(hold(controller, clock, "peace")) == 1, "fires again after release"
    assert controller.feed("jazz_hands", 1.0) is None


def test_arming_is_required_and_spent_by_one_command(repo, latch):
    enable(repo, bindings={"peace": "focus_on"}, cooldown_ms=0)
    clock = Clock()
    controller = GestureController(repo, clock=clock, dry_run=True)
    refused = hold(controller, clock, "peace")
    assert refused[0]["ok"] is False and "not armed" in refused[0]["detail"]
    assert hold(controller, clock, "open_palm")[0]["action"] == "arm" and controller.armed
    assert hold(controller, clock, "peace")[0]["ok"] is True
    assert hold(controller, clock, "peace")[0]["ok"] is False, "arming covers exactly one command"
    hold(controller, clock, "open_palm")
    clock.now += 6
    assert hold(controller, clock, "peace")[0]["ok"] is False, "the arming window expired"


def test_cooldown_and_rate_limit(repo, latch):
    enable(repo, arming={"required": False}, bindings={"peace": "focus_on"}, cooldown_ms=3000, max_per_minute=2)
    clock = Clock()
    controller = GestureController(repo, clock=clock, dry_run=True)
    assert hold(controller, clock, "peace")[0]["ok"]
    assert hold(controller, clock, "peace")[0]["detail"] == "cooldown"
    clock.now += 4
    assert hold(controller, clock, "peace")[0]["ok"]
    clock.now += 4
    assert hold(controller, clock, "peace")[0]["detail"] == "rate limit"
    clock.now += 61
    assert hold(controller, clock, "peace")[0]["ok"]


def test_emergency_stop_bypasses_arming_cooldown_and_rate(repo, latch):
    enable(repo, bindings={"peace": "focus_on"}, cooldown_ms=60_000, max_per_minute=1)
    clock = Clock()
    controller = GestureController(repo, clock=clock)
    assert latch.stopped() is False
    event = hold(controller, clock, "fist")[0]
    assert event["action"] == "emergency_stop" and event["ok"] and latch.stopped() is True
    assert "nexus resume" in event["detail"]
    for pose in policy.POSES:                       # nothing a hand can show clears the latch
        hold(controller, clock, pose)
    assert latch.stopped() is True
    rows = [row for row in audit.tail(repo, limit=20) if row["what"] == "gesture.action"]
    assert rows and rows[0]["target"] == "emergency_stop" and rows[0]["risk"] == "high"
    latch.set(False)


def test_dry_run_changes_nothing(repo, latch):
    enable(repo)
    clock = Clock()
    event = hold(GestureController(repo, clock=clock, dry_run=True), clock, "fist")[0]
    assert event["dry_run"] and event["detail"] == "would run emergency_stop" and latch.stopped() is False


# -- approvals ---------------------------------------------------------------------------------- #
def pending(manager, name="write_file", risk=Risk.WRITE, args=None):
    item = PendingSpokenApproval(spec=ToolSpec(name, name, {"type": "object"}, risk), args=args or {"path": "a.txt"},
                                 risk=risk, preview="a.txt")
    manager.pending = item
    return item


def approving(repo, clock, manager, **sinks):
    return GestureController(repo, clock=clock, sinks=Sinks(approvals=manager, **sinks))


def test_gesture_can_deny_a_pending_approval(repo, latch):
    enable(repo, arming={"required": False})
    clock, manager = Clock(), SpokenApprovalManager(workspace=repo)
    item = pending(manager, "delete_file", Risk.DANGEROUS)
    event = hold(approving(repo, clock, manager), clock, "thumbs_down")[0]
    assert event["ok"] and event["detail"] == "denied"
    assert item.decision.approved is False and manager.pending is None and item.event.is_set()
    clock.now += 5
    nothing = hold(approving(repo, clock, manager), clock, "thumbs_down")[0]
    assert nothing["ok"] is False and "nothing is waiting" in nothing["detail"]


def test_approve_is_off_unless_the_operator_enables_it(repo, latch):
    enable(repo, arming={"required": False}, bindings={"thumbs_up": "approve"})
    clock, manager = Clock(), SpokenApprovalManager(workspace=repo)
    item = pending(manager)
    event = hold(approving(repo, clock, manager), clock, "thumbs_up")[0]
    assert event["ok"] is False and "approve.enabled" in event["detail"]
    assert item.decision is None and manager.pending is item


def test_gesture_approves_medium_once_and_never_dangerous(repo, latch):
    enable(repo, arming={"required": False}, bindings={"thumbs_up": "approve"}, approve={"enabled": True}, cooldown_ms=0)
    clock, manager = Clock(), SpokenApprovalManager(workspace=repo)
    item = pending(manager)
    event = hold(approving(repo, clock, manager), clock, "thumbs_up")[0]
    assert event["ok"] and event["detail"] == "approved write_file (MEDIUM)"
    assert isinstance(item.decision, ApprovalDecision) and item.decision.approved and item.decision.scope == "once"
    assert "gesture" in item.decision.reason and manager.pending is None

    for name, risk, args in (("delete_file", Risk.DANGEROUS, {"path": "a.txt"}),
                             ("run_command", Risk.DANGEROUS, {"command": "rm -rf build"}),
                             ("write_file", Risk.WRITE, {"path": "permissions.yaml", "content": "x"})):
        danger = pending(manager, name, risk, args)
        refused = hold(approving(repo, clock, manager), clock, "thumbs_up")[0]
        assert refused["ok"] is False and "DANGEROUS actions can not be approved by gesture" in refused["detail"], name
        assert danger.decision is None and manager.pending is danger


def test_gesture_never_approves_when_tainted_verified_or_stopped(repo, latch):
    enable(repo, arming={"required": False}, bindings={"thumbs_up": "approve"}, approve={"enabled": True}, cooldown_ms=0)
    clock, manager = Clock(), SpokenApprovalManager(workspace=repo)
    item = pending(manager)
    event = hold(approving(repo, clock, manager, tainted=lambda: True), clock, "thumbs_up")[0]
    assert event["ok"] is False and "untrusted content" in event["detail"] and item.decision is None

    strict = SpokenApprovalManager(workspace=repo, verify_speaker=lambda audio: (True, ""))
    item = pending(strict)
    event = hold(approving(repo, clock, strict), clock, "thumbs_up")[0]
    assert event["ok"] is False and "speaker or second-factor" in event["detail"] and item.decision is None

    latch.set(True)
    item = pending(manager)
    event = hold(approving(repo, clock, manager), clock, "thumbs_up")[0]
    assert event["ok"] is False and "emergency stop" in event["detail"] and item.decision is None
    latch.set(False)

    safe_only = write_policy(repo, "gesture.yaml", arming={"required": False}, approve={"enabled": True, "max_risk": "SAFE"})
    assert safe_only.exists()
    item = pending(manager)
    event = hold(approving(repo, clock, manager), clock, "thumbs_up")[0]
    assert event["ok"] is False and "MEDIUM actions can not be approved" in event["detail"]


def test_a_stale_approval_is_not_resolved(repo):
    manager = SpokenApprovalManager(workspace=repo)
    item = pending(manager)
    other = pending(manager)
    assert manager.decide_pending(item, True, reason="x") is False and item.decision is None
    other.expires_at = 0.0
    assert manager.decide_pending(other, True, reason="x") is False and other.decision is None


def test_runtime_fails_closed_without_a_gate(repo, monkeypatch):
    """The voice glue treats an unknown session as tainted, so it can deny but not approve."""
    import nexus.gesture.camera as camera
    from nexus.gesture import runtime

    enable(repo)
    started = {}

    class FakeWatcher:
        def __init__(self, controller, stop):
            started["controller"] = controller

        def start(self):
            started["started"] = True

    monkeypatch.setattr(camera, "missing", lambda: [])
    monkeypatch.setattr(camera, "GestureWatcher", FakeWatcher)

    class Pipeline:
        approval_manager = SpokenApprovalManager(workspace=repo)

        def interrupt_speech(self):
            return []

    pipeline = Pipeline()
    assert runtime.attach(repo, pipeline, threading.Event()) is not None and started["started"]
    sinks = started["controller"].sinks
    assert sinks.tainted() is True, "no gate known yet"
    pipeline.active_gate = type("Gate", (), {"tainted": False})()
    assert sinks.tainted() is False
    pipeline.active_gate.tainted = True
    assert sinks.tainted() is True


# -- camera loop, tools, CLI --------------------------------------------------------------------- #
def test_watcher_feeds_landmarks_and_stores_no_frames(repo, latch, tmp_path):
    enable(repo, hold_ms=200)
    seen = []
    controller = GestureController(repo, dry_run=True)
    ticks = iter(range(1000))
    controller.clock = lambda: next(ticks) * 0.1
    watcher = GestureWatcher(controller, frames=[None] + [FIST] * 6 + [None], on_event=seen.append)
    watcher.run()
    assert watcher.error == "" and watcher.seen == 8
    assert [event["action"] for event in seen] == ["emergency_stop"]
    rows = [row for row in audit.tail(repo, limit=20) if row["what"] == "gesture.camera"]
    assert [row["result"] for row in rows] == ["on", "off"]
    # the trail says that the camera ran and for how many frames; no landmark, pose or picture is in it or on disk
    assert [row["target"] for row in rows] == ["camera", "camera"] and rows[1]["detail"] == {"frames": 8, "error": ""}
    assert not rows[0]["detail"] and not list(repo.rglob("*.jpg")) and not list(repo.rglob("*.png"))


def test_missing_dependencies_are_reported_not_crashed(repo):
    enable(repo)
    absent = missing()
    assert set(absent) <= {"opencv-python", "mediapipe"}
    if absent:
        controller = GestureController(repo, dry_run=True)
        watcher = GestureWatcher(controller)
        watcher.run()
        assert "nexus-agent[gesture]" in watcher.error
        assert status(repo)["missing"] == absent
        assert run_cli(["gesture", "watch", "--seconds", "1", "--dry-run", "-C", str(repo)]) == 2


# -- the real camera loop, with stand-ins for OpenCV and MediaPipe -------------------------------- #
SHAPE = (240, 320, 3)      # rows, columns, channels of a stand-in frame: 4:3, like most cameras


def frames_named(*names):
    return [SimpleNamespace(name=name, shape=SHAPE) for name in names]


def as_mediapipe(landmarks):
    """Synthetic picture points as MediaPipe would report them for a 320x240 frame: x as a fraction of the width."""
    return [SimpleNamespace(x=x * SHAPE[0] / SHAPE[1], y=y, z=0.0) for x, y in landmarks]


def as_world(landmarks):
    return [SimpleNamespace(x=x * 0.1, y=y * 0.1, z=0.0) for x, y in landmarks]


def flat(rows):
    """Points as one list of numbers, for a comparison that allows for rounding."""
    return pytest.approx([value for row in rows for value in row])


class Camera:
    """What the loop uses of ``cv2``. After the scripted frames it sets ``stop``, as a person would."""

    COLOR_BGR2RGB, CAP_PROP_FRAME_WIDTH, CAP_PROP_FRAME_HEIGHT, CAP_PROP_BUFFERSIZE = 4, 3, 4, 38

    def __init__(self, names, stop=None, opens=True):
        self.frames, self.stop, self.opens = frames_named(*names), stop, opens
        self.opened_device, self.settings, self.released, self.converted = None, {}, 0, []

    def VideoCapture(self, device):
        self.opened_device = device
        return self

    def isOpened(self):
        return self.opens

    def set(self, key, value):
        self.settings[key] = value

    def read(self):
        if not self.frames:
            return False, None
        frame = self.frames.pop(0)
        if not self.frames and self.stop is not None:
            self.stop.set()
        return True, frame

    def release(self):
        self.released += 1

    def cvtColor(self, frame, code):
        self.converted.append((frame.name, code))
        return f"rgb:{frame.name}"


def old_mediapipe(hands_by_frame, world=True):
    """MediaPipe 0.10.21 and older: ``solutions.hands`` with a built-in model."""
    made = SimpleNamespace(options=None, closed=0, seen=[])

    class Hands:
        def __init__(self, **options):
            made.options = options

        def process(self, rgb):
            made.seen.append(rgb)
            found = hands_by_frame.get(rgb)
            if not found:
                return SimpleNamespace(multi_hand_landmarks=None, multi_hand_world_landmarks=None)
            result = SimpleNamespace(multi_hand_landmarks=[SimpleNamespace(landmark=as_mediapipe(found))])
            if world:
                result.multi_hand_world_landmarks = [SimpleNamespace(landmark=as_world(found))]
            return result

        def close(self):
            made.closed += 1

    return SimpleNamespace(solutions=SimpleNamespace(hands=SimpleNamespace(Hands=Hands)), tasks=None), made


def new_mediapipe(hands_by_frame, fails=None):
    """MediaPipe 0.10.30 and newer: the Tasks API only, and no model in the package."""
    made = SimpleNamespace(options=None, closed=0, seen=[], stamps=[])

    class Landmarker:
        def detect_for_video(self, image, stamp):
            made.seen.append((image.image_format, image.data))
            made.stamps.append(stamp)
            found = hands_by_frame.get(image.data)
            return SimpleNamespace(hand_landmarks=[as_mediapipe(found)] if found else [],
                                   hand_world_landmarks=[as_world(found)] if found else [])

        def close(self):
            made.closed += 1

    def create_from_options(options):
        made.options = options
        if fails is not None:
            raise fails
        return Landmarker()

    vision = SimpleNamespace(HandLandmarkerOptions=lambda **options: SimpleNamespace(**options),
                             RunningMode=SimpleNamespace(IMAGE="image", VIDEO="video"),
                             HandLandmarker=SimpleNamespace(create_from_options=create_from_options))
    tasks = SimpleNamespace(vision=vision, BaseOptions=lambda **options: SimpleNamespace(**options))
    module = SimpleNamespace(tasks=tasks, Image=lambda image_format, data: SimpleNamespace(image_format=image_format, data=data),
                             ImageFormat=SimpleNamespace(SRGB="srgb"))
    return module, made


def installed(monkeypatch, cv2, mediapipe):
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    monkeypatch.setitem(sys.modules, "mediapipe", mediapipe)
    monkeypatch.setattr(camera_module, "missing", lambda: [])


CAMERA = {"device": 2, "width": 320, "height": 240, "fps": 30, "hand_model": ""}


def model_file(tmp_path: Path) -> Path:
    path = tmp_path / "models" / "hand_landmarker.task"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"model")
    return path


def test_the_loop_with_a_current_mediapipe_and_the_operators_model(repo, tmp_path, monkeypatch):
    stop = threading.Event()
    cv2 = Camera(["f1", "f2", "f3"], stop)
    mediapipe, made = new_mediapipe({"rgb:f1": FIST, "rgb:f3": OPEN})
    installed(monkeypatch, cv2, mediapipe)
    model = model_file(tmp_path)
    found = list(camera_module.landmark_frames({**CAMERA, "hand_model": str(model)}, stop, repo))
    assert [classify(row)[0] if row else None for row in found] == ["fist", None, "open_palm"]
    # the picture points come back in one unit for x and y, with the world points beside them
    assert isinstance(found[0], Hand) and [v for row in found[0].image for v in row] == flat(FIST)
    assert [v for row in found[0].world for v in row] == flat([(x * 0.1, y * 0.1, 0.0) for x, y in FIST])
    assert (made.options.base_options.model_asset_path, made.options.running_mode, made.options.num_hands) == (
        str(model), "video", 1)
    # every frame is converted and handed over once; the stamps only grow
    assert cv2.converted == [("f1", 4), ("f2", 4), ("f3", 4)] and made.seen == [("srgb", f"rgb:{name}") for name in ("f1", "f2", "f3")]
    assert made.stamps == sorted(set(made.stamps)) and len(made.stamps) == 3
    assert (cv2.opened_device, cv2.settings) == (2, {3: 320, 4: 240, 38: 1})
    assert cv2.released == 1 and made.closed == 1


def test_the_loop_with_an_old_mediapipe_and_its_built_in_model(repo, monkeypatch):
    stop = threading.Event()
    cv2 = Camera(["f1", "f2"], stop)
    mediapipe, made = old_mediapipe({"rgb:f2": PEACE})
    installed(monkeypatch, cv2, mediapipe)
    found = list(camera_module.landmark_frames(CAMERA, stop, repo))
    assert [classify(row)[0] if row else None for row in found] == [None, "peace"]
    assert [v for row in found[1].image for v in row] == flat(PEACE) and found[1].world is not None
    assert made.options == {"max_num_hands": 1, "min_detection_confidence": 0.6, "min_tracking_confidence": 0.5}
    assert made.seen == ["rgb:f1", "rgb:f2"] and cv2.released == 1 and made.closed == 1
    # a MediaPipe so old that it has no world landmarks: the picture decides alone
    stop = threading.Event()
    older, _made = old_mediapipe({"rgb:f1": FIST}, world=False)
    installed(monkeypatch, Camera(["f1", "f2"], stop), older)
    first = next(camera_module.landmark_frames(CAMERA, stop, repo))
    assert first.world is None and classify(first)[0] == "fist"


def test_leaving_the_loop_early_frees_the_camera(repo, monkeypatch):
    cv2 = Camera(["f1", "f2", "f3", "f4"])
    mediapipe, made = old_mediapipe({})
    installed(monkeypatch, cv2, mediapipe)
    frames = camera_module.landmark_frames(CAMERA, threading.Event(), repo)
    assert list(itertools.islice(frames, 2)) == [None, None] and cv2.released == 0
    frames.close()
    assert cv2.released == 1 and made.closed == 1
    # a camera that stops delivering is an error, and is freed too
    cv2 = Camera(["f1"])
    installed(monkeypatch, cv2, mediapipe)
    with pytest.raises(GestureError, match="Camera stopped delivering frames"):
        list(camera_module.landmark_frames(CAMERA, threading.Event(), repo))
    assert cv2.released == 1


def test_a_setup_that_cannot_work_is_refused_before_the_camera_is_opened(repo, tmp_path, monkeypatch):
    cv2 = Camera(["f1"])
    mediapipe, made = new_mediapipe({})
    installed(monkeypatch, cv2, mediapipe)
    model = model_file(tmp_path)

    def loop(**camera):
        return list(camera_module.landmark_frames({**CAMERA, **camera}, threading.Event(), repo))

    # a current MediaPipe has no model of its own
    with pytest.raises(GestureError, match="This MediaPipe version has no built-in hand model: download hand_landmarker.task"):
        loop()
    # the model decides which gesture is seen: it must be where the agent cannot write
    inside = repo / "hand_landmarker.task"
    inside.write_bytes(b"model")
    link = tmp_path / "link.task"
    link.symlink_to(model)
    (tmp_path / "empty.task").write_bytes(b"")
    with (tmp_path / "huge.task").open("wb") as handle:
        handle.truncate(camera_module.MAX_MODEL_BYTES + 1)
    for named, message in ((str(inside), "must lie outside the workspace"),
                           (str(repo / "app" / ".." / "hand_landmarker.task"), "must lie outside the workspace"),
                           ("models/hand_landmarker.task", "must be an absolute path"),
                           (str(link), "not a regular file"),
                           (str(tmp_path / "gone.task"), "not a regular file"),
                           (str(tmp_path), "not a regular file"),
                           (str(tmp_path / "empty.task"), "is empty or larger than 64 MB"),
                           (str(tmp_path / "huge.task"), "is empty or larger than 64 MB")):
        with pytest.raises(GestureError, match=message):
            loop(hand_model=named)
    # a link in the path that leads back into the workspace does not hide it
    (tmp_path / "way").symlink_to(repo, target_is_directory=True)
    with pytest.raises(GestureError, match="must lie outside the workspace"):
        loop(hand_model=str(tmp_path / "way" / "hand_landmarker.task"))
    # a file MediaPipe cannot use
    broken, _made = new_mediapipe({}, fails=ValueError("Unable to open file"))
    installed(monkeypatch, cv2, broken)
    with pytest.raises(GestureError, match=r"MediaPipe could not start the hand detector \(ValueError: Unable to open file\)"):
        loop(hand_model=str(model))
    # an old MediaPipe without the Tasks API, asked to use a model file
    old, _made = old_mediapipe({})
    installed(monkeypatch, cv2, old)
    with pytest.raises(GestureError, match="has no HandLandmarker"):
        loop(hand_model=str(model))
    assert cv2.opened_device is None and cv2.released == 0


def test_packages_that_are_installed_but_do_not_load_are_named(repo, monkeypatch):
    """On a machine without a desktop OpenCV may be there and still not import (libGL)."""
    monkeypatch.setattr(camera_module, "missing", lambda: [])
    monkeypatch.setitem(sys.modules, "cv2", None)          # ``import cv2`` now raises ImportError
    with pytest.raises(GestureError, match="OpenCV or MediaPipe is installed but does not load"):
        list(camera_module.landmark_frames(CAMERA, threading.Event(), repo))


def test_a_camera_that_does_not_open_leaves_nothing_behind(repo, monkeypatch):
    cv2 = Camera([], opens=False)
    mediapipe, made = old_mediapipe({})
    installed(monkeypatch, cv2, mediapipe)
    with pytest.raises(GestureError, match="Camera 2 could not be opened"):
        list(camera_module.landmark_frames(CAMERA, threading.Event(), repo))
    assert cv2.released == 1 and made.closed == 1


def test_cli_watch_can_show_what_the_camera_sees(repo, latch, tmp_path, monkeypatch, capsys):
    """``--poses`` is for tuning: one line whenever the pose changes, and whether it is sure enough to count."""
    enable(repo, hold_ms=200, min_confidence=0.9, camera={"hand_model": str(model_file(tmp_path)), "fps": 30})
    unsure = next(row for row in HANDS if row["shows"] == "thumbs_up" and "curled" in row.get("view", ""))
    by_frame = {"rgb:f1": FIST, "rgb:f2": FIST, "rgb:f4": OPEN}

    class Landmarker:
        def detect_for_video(self, image, stamp):
            if image.data == "rgb:f3":          # a photographed hand, as MediaPipe reported it
                return SimpleNamespace(
                    hand_landmarks=[[SimpleNamespace(x=x, y=y, z=0.0) for x, y in unsure["image"]]],
                    hand_world_landmarks=[[SimpleNamespace(x=x, y=y, z=z) for x, y, z in unsure["world"]]])
            found = by_frame.get(image.data)
            return SimpleNamespace(hand_landmarks=[as_mediapipe(found)] if found else [],
                                   hand_world_landmarks=[as_world(found)] if found else [])

        def close(self):
            pass

    mediapipe, _made = new_mediapipe({})
    mediapipe.tasks.vision.HandLandmarker = SimpleNamespace(create_from_options=lambda options: Landmarker())
    installed(monkeypatch, Camera(["f0", "f1", "f2", "f3", "f4", "f5"]), mediapipe)
    # the stand-in camera runs out of frames, which ends the watch with an error
    assert run_cli(["gesture", "watch", "--dry-run", "--poses", "--seconds", "5", "-C", str(repo)]) == 2
    captured = capsys.readouterr()
    assert "Camera stopped delivering frames" in captured.err
    lines = [line for line in captured.err.splitlines() if line.startswith("sees ")]
    assert lines[:2] == ["sees none (0.00)", "sees fist (1.00)"]
    assert lines[2].startswith("sees thumbs_up (0.") and lines[2].endswith(", below min_confidence)")
    assert lines[3].startswith("sees open_palm (") and lines[4] == "sees none (0.00)" and len(lines) == 5


def test_the_watcher_runs_the_real_loop(repo, latch, tmp_path, monkeypatch):
    enable(repo, hold_ms=200, camera={"hand_model": str(model_file(tmp_path)), "fps": 30})
    stop = threading.Event()
    cv2 = Camera([f"f{number}" for number in range(8)], stop)
    mediapipe, made = new_mediapipe({f"rgb:f{number}": FIST for number in range(1, 7)})
    installed(monkeypatch, cv2, mediapipe)
    controller = GestureController(repo, dry_run=True)
    ticks = iter(range(1000))
    controller.clock = lambda: next(ticks) * 0.1
    seen = []
    watcher = GestureWatcher(controller, stop, on_event=seen.append)
    watcher.run()
    # (the stand-in camera raises the stop flag with its last frame, so that one is not looked at any more)
    assert watcher.error == "" and watcher.seen == 7 and [event["action"] for event in seen] == ["emergency_stop"]
    assert cv2.released == 1 and made.closed == 1 and made.options.base_options.model_asset_path.endswith(".task")
    # a setup that cannot work shows in the watcher's error and in the audit trail
    write_policy(repo, "gesture.yaml", camera={"hand_model": str(repo / "model.task")})
    refused = GestureWatcher(GestureController(repo, dry_run=True), threading.Event())
    refused.run()
    assert "must lie outside the workspace" in refused.error
    assert [row["result"] for row in audit.tail(repo, limit=20) if row["what"] == "gesture.camera"] == [
        "on", "off", "on", "error"]


@pytest.mark.parametrize(("version", "needed"), [
    ("0.10.9", False), ("0.10.21", False), ("0.10.30", True), ("0.10.35", True), ("1.0.0", True), ("1.2.3", True),
    ("1.3.0rc1", True), ("0.9.3.0", False), ("2", True), ("not a version", False)])
def test_which_mediapipe_needs_a_model_file(monkeypatch, version, needed):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: version)
    assert camera_module.needs_model() is needed


def test_status_says_what_is_missing_before_the_camera_is_started(repo, tmp_path, monkeypatch):
    def absent(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", absent)
    assert camera_module.needs_model() is False
    enable(repo)
    info = status(repo)
    assert "error" not in info and info["hand_model"] == "built into MediaPipe"
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.2.3")
    assert status(repo)["error"] == camera_module.NO_MODEL
    enable(repo, camera={"hand_model": str(repo / "model.task")})
    assert "must lie outside the workspace" in status(repo)["error"]
    enable(repo, camera={"hand_model": str(model_file(tmp_path))})
    info = status(repo)
    assert "error" not in info and info["hand_model"] == "operator file" and str(tmp_path) not in json.dumps(info)
    from nexus import featurepacks

    assert next(row for row in featurepacks.doctor_rows(repo) if row[0] == "opt:gesture")[1] is True
    enable(repo)
    row = next(row for row in featurepacks.doctor_rows(repo) if row[0] == "opt:gesture")
    assert row[1] is False and "no built-in hand model" in row[2]


def test_the_shipped_policies_are_off_and_say_what_the_code_assumes():
    root = Path(__file__).resolve().parents[1]
    assert yaml.safe_load((root / "gesture.yaml").read_text(encoding="utf-8")) == policy.DEFAULTS
    assert policy.DEFAULTS["enabled"] is False and policy.DEFAULTS["camera"]["hand_model"] == ""
    # gestures.yaml ships two example bindings, switched off
    shipped = yaml.safe_load((root / "gestures.yaml").read_text(encoding="utf-8"))
    assert shipped["enabled"] is False and set(shipped["bindings"]) == set(policy.POSES)
    assert set(shipped["bindings"].values()) <= set(policy.ACTIONS) and "approve" not in shipped["bindings"].values()


def test_no_agent_tools_are_registered(repo):
    enable(repo, bindings={"thumbs_up": "approve"}, approve={"enabled": True})
    _ctx, reg = tool_registry(repo)
    assert not [name for name in reg.names() if "gesture" in name]


def test_cli_classify_and_bindings(repo, capsys):
    enable(repo)
    (repo / "hand.json").write_text(json.dumps(FIST))
    assert run_cli(["gesture", "classify", "hand.json", "-C", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out)["pose"] == "fist"
    # a file may hold MediaPipe's world landmarks next to the picture points
    edge_on = photographed(next(row for row in HANDS if row.get("view") == "palm up, seen from the side"))
    (repo / "flat.json").write_text(json.dumps({"image": edge_on.image}))
    assert run_cli(["gesture", "classify", "flat.json", "-C", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out)["pose"] == "open_palm"
    (repo / "both.json").write_text(json.dumps({"image": edge_on.image, "world": edge_on.world}))
    assert run_cli(["gesture", "classify", "both.json", "-C", str(repo)]) == 1
    shown = json.loads(capsys.readouterr().out)
    assert shown["pose"] == "none" and set(shown["fingers"]) == {"thumb", "index", "middle", "ring", "pinky"}
    (repo / "odd.json").write_text(json.dumps({"image": "a hand"}))
    assert run_cli(["gesture", "classify", "odd.json", "-C", str(repo)]) == 1
    assert json.loads(capsys.readouterr().out) == {"pose": "none", "confidence": 0.0, "fingers": None}
    (repo / "blank.json").write_text(json.dumps([[0, 0]] * 21))
    assert run_cli(["gesture", "classify", "blank.json", "-C", str(repo)]) == 1
    assert run_cli(["gesture", "classify", "../outside.json", "-C", str(repo)]) == 2
    capsys.readouterr()
    assert run_cli(["gesture", "bindings", "-C", str(repo)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["bindings"] == {"fist": "emergency_stop", "thumbs_down": "deny"} and shown["approve"]["enabled"] is False
    info = status(repo)
    assert info["approve_by_gesture"] is False and info["frames_stored"] is False
