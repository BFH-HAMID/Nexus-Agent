"""Pure-Python hand-pose classifier over 21 landmarks (MediaPipe Hands order).

Index: 0 wrist; thumb 1-4; index 5-8; middle 9-12; ring 13-16; pinky 17-20
(each finger: base joint first, tip last).

Two sets of points describe one hand:

* ``landmarks`` - where the joints are in the picture: x to the right, y
  downwards, **the same unit for both**.  MediaPipe gives x and y as fractions
  of the frame's width and height; the camera loop multiplies x by
  width / height first, otherwise a thumb that sticks out sideways looks
  shorter than it is on every frame that is not square.
* ``world`` (optional) - the same joints in three dimensions (MediaPipe's
  "world landmarks", metres, same orientation as the picture).

Whether a finger is extended is a ratio of distances, so it does not depend on
hand size, distance from the camera, handedness or mirroring.  The picture
alone misjudges a hand that points at the camera (its lengths are
foreshortened); the three-dimensional estimate misjudges other views.  With
both, a finger counts as clearly folded or extended when the two agree and one
of them is clear; when they disagree it is uncertain.  Without ``world`` the
picture decides alone.

Only ``thumbs_up`` / ``thumbs_down`` / ``point_up`` look at a direction, and
they take it from the picture: up is what the person sees as up.  A thumb or
finger that points mostly at the camera has no such direction and matches none
of them.  ``open_palm`` needs the palm or the back of the hand to face the
camera (``world`` only), so a hand that lies flat in view is not the pose.

``classify`` returns ``(pose, confidence)``; ``("none", 0.0)`` when nothing fits.
Confidence is the weakest of the margins that define the pose, in 0..1.

The thresholds were set on synthetic hands and then checked against MediaPipe
landmarks of 65 photographs of hands (``tests/data/gesture_hands.json``).
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import NamedTuple

FINGERS = {"index": (5, 6, 8), "middle": (9, 10, 12), "ring": (13, 14, 16), "pinky": (17, 18, 20)}


class Hand(NamedTuple):
    """One detected hand as the camera loop hands it over: picture points, and world points when there are any."""

    image: list[tuple[float, float]]
    world: list[tuple[float, float, float]] | None = None


def _points(landmarks: object, size: int = 2) -> list[tuple[float, ...]] | None:
    """21 points with ``size`` finite coordinates each, or ``None`` when the input is anything else."""
    if not isinstance(landmarks, Sequence) or isinstance(landmarks, (str, bytes)) or len(landmarks) != 21:
        return None
    points = []
    for item in landmarks:
        if isinstance(item, dict):
            item = tuple(item.get(axis) for axis in "xyz"[:size])
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) < size:
            return None
        values = tuple(item[:size])
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
            return None
        points.append(tuple(float(value) for value in values))
    return points


def _clip(value: float) -> float:
    return max(-1.0, min(1.0, value))


def _unit(value: float) -> float:
    return max(0.0, min(1.0, value))


def _measure(points: list[tuple[float, ...]]) -> dict[str, float] | None:
    """Signed score per finger in one space (picture or world): +1 clearly extended, -1 clearly folded."""
    wrist = points[0]
    width = math.dist(points[5], points[17])            # across the knuckles
    if math.dist(wrist, points[9]) < 1e-6 or width < 1e-6:
        return None
    scores = {}
    for name, (_base, middle, tip) in FINGERS.items():
        reach = math.dist(points[middle], wrist)
        # an extended finger's tip is well beyond its middle joint, seen from the wrist; a folded one comes back
        scores[name] = _clip((math.dist(points[tip], wrist) / reach - 1.1) / 0.25) if reach > 1e-6 else 0.0
    # The thumb barely curls. What changes is where its tip is: over the palm or the folded fingers (within about
    # 1.25 knuckle-widths of the little finger's knuckle), or out beyond the index finger (1.75 and more).
    scores["thumb"] = _clip((math.dist(points[4], points[17]) / width - 1.5) / 0.25)
    return scores


def _agree(flat: float, deep: float) -> float:
    """One score from the picture's and the world's: the clearer one when they agree, their mean when they do not."""
    if flat * deep > 0:
        return flat if abs(flat) >= abs(deep) else deep
    return (flat + deep) / 2


def _both(landmarks: object, world: object) -> tuple[list | None, list | None]:
    if isinstance(landmarks, Hand):
        landmarks, world = landmarks.image, landmarks.world
    return _points(landmarks), (_points(world, 3) if world is not None else None)


def finger_scores(landmarks: object, world: object = None) -> dict[str, float] | None:
    """Signed extension score per finger: +1 clearly extended, -1 clearly folded, near 0 uncertain."""
    points, deep = _both(landmarks, world)
    if points is None:
        return None
    scores = _measure(points)
    if scores is None or deep is None:
        return scores
    other = _measure(deep)
    return scores if other is None else {name: _agree(value, other[name]) for name, value in scores.items()}


def _upward(points: list[tuple[float, ...]], start: int, end: int) -> float:
    """+1 when the segment points straight up in the picture, -1 straight down."""
    dx, dy = points[end][0] - points[start][0], points[end][1] - points[start][1]
    length = math.hypot(dx, dy)
    return -dy / length if length > 1e-6 else 0.0


def _in_picture(deep: list[tuple[float, ...]] | None, start: int, end: int) -> float:
    """How much of a segment lies in the picture plane: 1 = all of it, 0 = it points at the camera."""
    if deep is None:
        return 1.0
    length = math.dist(deep[start], deep[end])
    return math.hypot(deep[end][0] - deep[start][0], deep[end][1] - deep[start][1]) / length if length > 1e-9 else 0.0


def _facing(deep: list[tuple[float, ...]] | None) -> float:
    """How squarely the palm (or the back of the hand) faces the camera: 1 = square on, 0 = seen from its edge."""
    if deep is None:
        return 1.0
    a = [deep[5][axis] - deep[0][axis] for axis in range(3)]
    b = [deep[17][axis] - deep[0][axis] for axis in range(3)]
    normal = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
    length = math.sqrt(sum(value * value for value in normal))
    return abs(normal[2]) / length if length > 1e-12 else 0.0


def classify(landmarks: object, world: object = None) -> tuple[str, float]:
    """``(pose, confidence)`` for one hand. ``landmarks`` may be a :class:`Hand`."""
    points, deep = _both(landmarks, world)
    scores = finger_scores(points, deep) if points is not None else None
    if scores is None:
        return "none", 0.0
    assert points is not None
    thumb, index, middle, ring, pinky = (scores[name] for name in ("thumb", "index", "middle", "ring", "pinky"))
    folded = min(-index, -middle, -ring, -pinky)          # > 0 when all four fingers are folded

    def points_up(start: int, end: int, sign: float = 1.0) -> float:
        """1 when the segment points up (``sign`` -1: down) within about 35 degrees, 0 beyond 60 degrees."""
        seen = _unit((_in_picture(deep, start, end) - 0.3) / 0.3)
        return min(_unit((sign * _upward(points, start, end) - 0.5) / 0.3), seen)

    candidates: list[tuple[str, float]] = [
        ("open_palm", min(thumb, index, middle, ring, pinky, _unit((_facing(deep) - 0.4) / 0.3))),
        ("fist", min(-thumb, folded)),
        ("thumbs_up", min(thumb, folded, points_up(2, 4))),
        ("thumbs_down", min(thumb, folded, points_up(2, 4, -1.0))),
        ("peace", min(index, middle, -ring, -pinky)),
        ("point_up", min(index, -middle, -ring, -pinky, points_up(5, 8))),
    ]
    pose, confidence = max(candidates, key=lambda item: item[1])
    return (pose, round(confidence, 3)) if confidence > 0 else ("none", 0.0)
