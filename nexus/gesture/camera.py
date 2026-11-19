"""Camera loop: frames -> 21 landmarks -> pose -> controller. Frames never leave memory.

OpenCV and MediaPipe are imported only when the camera is opened
(``pip install "nexus-agent[gesture]"``).  A frame is held just long enough to
extract landmarks and is then overwritten; nothing is written to disk, logged
or sent over the network, and only the pose name reaches the controller.

MediaPipe has two hand APIs, and which one an installation has depends on its
version:

* ``solutions.hands`` with a built-in model: MediaPipe 0.10.21 and older
  (wheels for Python 3.12 and older only);
* the Tasks API (``HandLandmarker``): every 0.10 and 1.x release. It needs a
  model file, ``hand_landmarker.task``, that the operator downloads once and
  names in ``gesture.yaml`` (``camera.hand_model``).

The model decides which gesture is seen, so it is trusted like the policy
file: it has to lie outside the workspace, where the agent cannot write.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .. import policyfile
from .classify import Hand, classify
from .controller import GestureController, GestureError

REQUIRED = {"cv2": "opencv-python", "mediapipe": "mediapipe"}
MAX_MODEL_BYTES = 64_000_000
LAST_WITH_BUILT_IN_MODEL = (0, 10, 21)
NO_MODEL = ("this MediaPipe version has no built-in hand model: download hand_landmarker.task (see docs/gesture.md) "
            "and set camera.hand_model in gesture.yaml")


def missing() -> list[str]:
    """Pip names of the optional dependencies that are not installed."""
    out = []
    for module, package in REQUIRED.items():
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            out.append(package)
    return out


def needs_model() -> bool:
    """True when the installed MediaPipe is known to come without a hand model. Does not import MediaPipe."""
    try:
        text = importlib.metadata.version("mediapipe")
    except importlib.metadata.PackageNotFoundError:
        return False
    numbers = []
    for part in text.split(".")[:3]:
        digits = "".join(ch for ch in part[:8] if ch.isdigit())
        numbers.append(int(digits) if digits else 0)
    return tuple(numbers) > LAST_WITH_BUILT_IN_MODEL


def hand_model(workspace: Path | str, text: str) -> Path:
    """The model file the operator named in ``camera.hand_model``, checked before MediaPipe opens it."""
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise GestureError("gesture.yaml: camera.hand_model must be an absolute path")
    try:
        resolved = path.resolve()
        if resolved.is_relative_to(Path(workspace).resolve()):
            raise GestureError("gesture.yaml: camera.hand_model must lie outside the workspace: the agent can write "
                               "there, and the model decides which gesture is seen")
        if path.is_symlink() or not resolved.is_file():
            raise GestureError(f"gesture.yaml: camera.hand_model is not a regular file ({path.name[:80]})")
        if not 0 < resolved.stat().st_size <= MAX_MODEL_BYTES:
            raise GestureError(f"gesture.yaml: camera.hand_model is empty or larger than {MAX_MODEL_BYTES // 1_000_000} MB")
    except OSError as exc:
        raise GestureError(f"gesture.yaml: camera.hand_model could not be read ({type(exc).__name__})") from None
    return resolved


def to_hand(image: Any, world: Any, width: int, height: int) -> Hand:
    """MediaPipe's landmarks of one hand as the classifier takes them.

    MediaPipe gives x and y as fractions of the frame's width and height. On a
    frame that is not square these are two different units, and a distance
    measured across them is wrong; x is put into the unit of y here.
    """
    aspect = width / height if width > 0 and height > 0 else 1.0
    return Hand([(float(p.x) * aspect, float(p.y)) for p in image],
                [(float(p.x), float(p.y), float(p.z)) for p in world] if world else None)


class _BuiltIn:
    """``solutions.hands``: MediaPipe 0.10.21 and older, with the model that ships inside the package."""

    def __init__(self, solutions: Any) -> None:
        self._hands = solutions.hands.Hands(max_num_hands=1, min_detection_confidence=0.6, min_tracking_confidence=0.5)

    def find(self, rgb: Any, at_ms: int, width: int, height: int) -> Hand | None:
        result = self._hands.process(rgb)
        found = getattr(result, "multi_hand_landmarks", None)
        if not found:
            return None
        world = getattr(result, "multi_hand_world_landmarks", None)
        return to_hand(found[0].landmark, world[0].landmark if world else None, width, height)

    def close(self) -> None:
        self._hands.close()


class _Tasks:
    """The Tasks API (``HandLandmarker``) with the model file the operator installed."""

    def __init__(self, mediapipe: Any, model: Path) -> None:
        tasks = mediapipe.tasks
        options = tasks.vision.HandLandmarkerOptions(
            base_options=tasks.BaseOptions(model_asset_path=str(model)),
            running_mode=tasks.vision.RunningMode.VIDEO, num_hands=1,
            min_hand_detection_confidence=0.6, min_hand_presence_confidence=0.5, min_tracking_confidence=0.5)
        self._mediapipe = mediapipe
        self._last = -1
        try:
            self._landmarker = tasks.vision.HandLandmarker.create_from_options(options)
        except Exception as exc:  # noqa: BLE001 - a file it cannot use, or a system library it does not find
            raise GestureError(f"MediaPipe could not start the hand detector ({type(exc).__name__}: {str(exc)[:160]}). "
                               "Check that camera.hand_model is the file hand_landmarker.task") from None

    def find(self, rgb: Any, at_ms: int, width: int, height: int) -> Hand | None:
        self._last = max(int(at_ms), self._last + 1)       # MediaPipe takes timestamps that only grow
        image = self._mediapipe.Image(image_format=self._mediapipe.ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect_for_video(image, self._last)
        if not result.hand_landmarks:
            return None
        world = getattr(result, "hand_world_landmarks", None)
        return to_hand(result.hand_landmarks[0], world[0] if world else None, width, height)

    def close(self) -> None:
        self._landmarker.close()


def detector(mediapipe: Any, camera: dict[str, Any], workspace: Path | str) -> Any:
    """The hand detector this MediaPipe installation and this policy allow."""
    named = str(camera.get("hand_model") or "").strip()
    tasks = getattr(mediapipe, "tasks", None)
    if named:
        if tasks is None or not hasattr(tasks, "vision") or not hasattr(tasks.vision, "HandLandmarker"):
            raise GestureError("This MediaPipe build has no HandLandmarker; install mediapipe 0.10 or newer")
        return _Tasks(mediapipe, hand_model(workspace, named))
    solutions = getattr(mediapipe, "solutions", None)
    if solutions is not None and hasattr(solutions, "hands"):
        return _BuiltIn(solutions)
    raise GestureError(NO_MODEL[0].upper() + NO_MODEL[1:])


def landmark_frames(camera: dict[str, Any], stop: threading.Event, workspace: Path | str = ".") -> Iterator[Hand | None]:
    """Yield the hand found in each captured frame (``None`` when no hand is visible)."""
    absent = missing()
    if absent:
        raise GestureError('Gesture control needs: pip install "nexus-agent[gesture]" (missing: ' + ", ".join(absent) + ")")
    try:
        import cv2  # type: ignore[import-not-found]
        import mediapipe  # type: ignore[import-not-found]
    except ImportError as exc:  # installed, but a system library is missing (libGL on a machine without a desktop)
        raise GestureError(f"OpenCV or MediaPipe is installed but does not load: {str(exc)[:160]}") from None

    hands = detector(mediapipe, camera, workspace)       # before the camera is opened: a wrong setup lights no camera
    capture = None
    try:
        capture = cv2.VideoCapture(camera["device"])
        if not capture.isOpened():
            raise GestureError(f"Camera {camera['device']} could not be opened")
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, camera["width"])
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, camera["height"])
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        interval = 1.0 / camera["fps"]
        began = time.monotonic()
        while not stop.is_set():
            started = time.monotonic()
            ok, frame = capture.read()
            if not ok:
                raise GestureError("Camera stopped delivering frames")
            height, width = frame.shape[:2]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frame = None  # noqa: F841 - drop the pixels before yielding
            found = hands.find(rgb, int((started - began) * 1000), width, height)
            rgb = None  # noqa: F841
            yield found
            stop.wait(max(0.0, interval - (time.monotonic() - started)))
    finally:
        if capture is not None:
            capture.release()
        hands.close()


class GestureWatcher(threading.Thread):
    """Background loop feeding a :class:`GestureController`. ``frames`` is injectable for tests."""

    def __init__(self, controller: GestureController, stop: threading.Event | None = None, *,
                 frames: Iterable[Any] | None = None, on_event=None, on_pose=None) -> None:
        super().__init__(name="nexus-gesture", daemon=True)
        self.controller = controller
        self.stop_event = stop or threading.Event()
        self.frames = frames
        self.on_event = on_event
        self.on_pose = on_pose
        self.error = ""
        self.seen = 0

    def run(self) -> None:
        workspace = self.controller.workspace
        policyfile.audit(workspace, "gesture.camera", who="gesture", target="camera", result="on", risk="medium")
        try:
            source = self.frames if self.frames is not None else landmark_frames(
                self.controller.cfg["camera"], self.stop_event, workspace)
            for landmarks in source:
                if self.stop_event.is_set():
                    break
                self.seen += 1
                pose, confidence = classify(landmarks) if landmarks is not None else ("none", 0.0)
                if self.on_pose is not None:
                    self.on_pose(pose, confidence)
                event = self.controller.feed(pose, confidence)
                if event is not None and self.on_event is not None:
                    self.on_event(event)
        except Exception as exc:  # noqa: BLE001 - surfaced through .error and the audit trail
            self.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            policyfile.audit(workspace, "gesture.camera", who="gesture", target="camera",
                             result="error" if self.error else "off", risk="medium",
                             detail={"frames": self.seen, "error": self.error[:160]})
