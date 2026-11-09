"""Explicit bounded local camera sessions; no capture at import/startup time."""
from __future__ import annotations

import hashlib
import math
import os
import stat
import time
from pathlib import Path

from .policy import VisionError, approve, digest, recheck, require
from .state import CaptureLease, control, hard_off, store_frame


def weights_identity(path):
    path = Path(path).expanduser()
    if path.is_symlink() or path.resolve() != path.absolute() or not path.is_file():
        raise VisionError('Use an existing regular local YOLOv8 .pt file; no downloads or symlinks')
    def identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    before = path.stat()
    if before.st_nlink != 1 or not 0 < before.st_size <= 250_000_000 or path.suffix != '.pt':
        raise VisionError('YOLO weights exceed the local file/type budget')
    digest_value = hashlib.sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)), 'rb') as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or identity(opened) != identity(before):
            raise VisionError('Local detector weights changed')
        remaining = before.st_size
        while remaining:
            block = source.read(min(remaining, 1024 * 1024))
            if not block:
                raise VisionError('Truncated local detector weights')
            digest_value.update(block)
            remaining -= len(block)
        if identity(os.fstat(source.fileno())) != identity(before) or identity(path.stat()) != identity(before):
            raise VisionError('Local detector weights changed')
    return digest_value.hexdigest()


class YoloDetector:
    def __init__(self, weights, *, classes=None, confidence=0.6):
        self.weights = Path(weights).expanduser().absolute()
        weights_identity(self.weights)
        self.classes, self.confidence = classes, confidence
        # No dependency auto-install or HUB/telemetry calls; operator installs local weights.
        os.environ['YOLO_AUTOINSTALL'] = 'false'
        os.environ['YOLO_OFFLINE'] = 'true'
        try:
            from ultralytics import YOLO
        except ImportError:
            raise VisionError('Camera detection requires pip install "nexus-agent[vision]" and local YOLOv8 weights') from None
        from ultralytics import settings
        settings.update({'sync': False})
        self.model = YOLO(str(self.weights), task='detect')

    def detect(self, frame):
        names = self.model.names
        selected = [i for i, name in names.items() if self.classes is None or name in self.classes]
        if self.classes is not None and set(self.classes) - {names[i] for i in selected}:
            raise VisionError('Configured classes are not present in these YOLO weights')
        try:
            results = self.model.predict(frame, verbose=False, save=False, save_txt=False, save_crop=False,
                                         save_conf=False, show=False, visualize=False, stream=False, device='cpu',
                                         classes=selected, conf=self.confidence, max_det=100, imgsz=640)
            return [{'label': str(result.names[int(box.cls[0])]), 'confidence': float(box.conf[0]),
                     'box': [float(v) for v in box.xyxy[0].tolist()]}
                    for result in results[:1] for box in result.boxes[:100]]
        except Exception:
            raise VisionError('Local YOLO inference failed; no remote detector fallback') from None


class OpenCVCamera:
    def __init__(self, device, width, height):
        try:
            import cv2
        except ImportError:
            raise VisionError('Camera capture requires pip install "nexus-agent[vision]"') from None
        self.camera = cv2.VideoCapture(device)
        try:
            if not self.camera.isOpened():
                raise VisionError('Camera unavailable')
            self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            self.camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except BaseException:
            self.camera.release()
            raise

    def read(self):
        try:
            ok, frame = self.camera.read()
        except Exception:
            raise VisionError('Native camera read failed') from None
        if not ok or frame is None:
            raise VisionError('Camera did not produce a frame')
        return frame

    def close(self):
        self.camera.release()


def detections(rows, cfg, width, height):
    """Reject malformed detector output, never use labels as tasks or commands."""
    if not isinstance(rows, list) or len(rows) > 100:
        raise VisionError('Detector output exceeds bounded schema')
    accepted = []
    for row in rows:
        if not isinstance(row, dict):
            raise VisionError('Invalid detector record')
        score, label, box = row.get('confidence'), row.get('label'), row.get('box')
        if (type(score) not in (int, float) or not 0 <= score <= 1 or not math.isfinite(score)
                or not isinstance(label, str) or not isinstance(box, (tuple, list)) or len(box) != 4
                or any(type(v) not in (int, float) or not -10_000_000 <= v <= 10_000_000 or not math.isfinite(v) for v in box)
                or not 0 <= box[0] < box[2] <= width or not 0 <= box[1] < box[3] <= height):
            raise VisionError('Invalid detector class/confidence/region')
        if label in cfg['classes'] and score >= cfg['confidence']:
            accepted.append({'label': label, 'confidence': score, 'box': list(box)})
    return accepted


class CameraPipeline:
    def __init__(self, workspace, *, camera_factory=None, detector_factory=None, faces_factory=None,
                 event_sink=None, clock=None):
        self.root = Path(workspace).resolve()
        self.camera_factory = camera_factory or OpenCVCamera
        self.detector_factory = detector_factory or YoloDetector
        self.faces_factory = faces_factory
        self.event_sink = event_sink
        self.clock = clock or time.monotonic

    def run(self, *, gate=None, frames=None, seconds=None):
        cfg = require(self.root, 'camera')
        epoch = control(self.root)['epoch']
        camcfg = cfg['camera']
        frames = camcfg['max_frames'] if frames is None else frames
        seconds = camcfg['max_seconds'] if seconds is None else seconds
        if (type(frames) is not int or not 1 <= frames <= camcfg['max_frames']
                or type(seconds) not in (int, float) or not 0 < seconds <= camcfg['max_seconds'] or not math.isfinite(seconds)):
            raise VisionError('Camera session must fit operator frame/time limits')
        weights = self.root / Path(camcfg['weights']).expanduser()
        identity = weights_identity(weights)
        approve(self.root, gate, 'vision_camera_start', {'device': camcfg['device'], 'frames': frames,
                'seconds': seconds, 'fps': camcfg['fps'], 'classes': camcfg['classes'],
                'weights_sha256': identity, 'policy': digest(cfg), 'local_faces': cfg['faces']['enabled'],
                'store_frames': camcfg['store_frames'], 'events': camcfg['events']})
        recheck(self.root, cfg)
        if weights_identity(weights) != identity:
            raise VisionError('Detector weights changed after approval')
        count, retained, last = 0, 0, {}
        with CaptureLease(self.root, 'camera', epoch=epoch) as lease:
            camera = None
            frame = None
            try:
                detector = self.detector_factory(weights, classes=camcfg['classes'], confidence=camcfg['confidence'])
                faces = None
                if cfg['faces']['enabled']:
                    from .faces import FaceRegistry
                    faces = (self.faces_factory or FaceRegistry)(self.root)
                    faces.list()  # Fail closed on locked/corrupt encryption BEFORE opening camera.
                recheck(self.root, cfg)
                lease.check()
                camera = self.camera_factory(camcfg['device'], camcfg['width'], camcfg['height'])
                started, next_frame = self.clock(), self.clock()
                while count < frames and self.clock() - started < seconds:
                    # Wait using an interruptible event; provider retry sleeps do not control this loop.
                    while self.clock() < next_frame:
                        lease.check()
                        recheck(self.root, cfg)
                        lease.heartbeat()
                        lease.cancel.wait(min(0.25, max(0, next_frame - self.clock())))
                    if self.clock() - started >= seconds:
                        break
                    recheck(self.root, cfg)
                    lease.check()
                    with lease.frame():
                        frame = camera.read()
                    count += 1
                    if (not hasattr(frame, 'shape') or len(frame.shape) != 3 or frame.shape[2] != 3
                            or min(frame.shape[:2]) < 1 or frame.shape[0] * frame.shape[1] > 2_073_600
                            or str(frame.dtype) != 'uint8'):
                        raise VisionError('Camera frame exceeds pixel/type budget')
                    height, width = frame.shape[:2]
                    recheck(self.root, cfg)
                    lease.heartbeat()
                    objects = detections(detector.detect(frame), camcfg, width, height)
                    matches = faces.recognize(frame[:, :, ::-1].copy()) if faces else []
                    recheck(self.root, cfg)
                    lease.heartbeat()
                    events = []
                    if camcfg['events']:
                        if self.event_sink is None:
                            from ..jarvis.proactive import ProactiveEngine
                            sink = ProactiveEngine(self.root).receive_vision_event
                        else:
                            sink = self.event_sink
                        candidates = [{'kind': 'object', 'label': obj['label'], 'location': camcfg['location']}
                                      for obj in objects]
                        for face in matches:
                            if not face['matched']:
                                candidates.append({'kind': 'unknown_person', 'location': camcfg['location']})
                            elif face['name'] == cfg['faces']['owner']:
                                candidates.append({'kind': 'owner_greeting', 'location': camcfg['location']})
                        for event in {str(row): row for row in candidates}.values():
                            recheck(self.root, cfg)
                            lease.check()
                            events.append(sink(event))
                    if camcfg['store_frames']:
                        from .media import encode_image, pillow
                        Image, _ = pillow()
                        payload = encode_image(Image.fromarray(frame[:, :, ::-1]))
                        lease.check()
                        retained += int(store_frame(self.root, payload, cfg))
                    last = {'observed_at': time.time(), 'width': width, 'height': height,
                            'objects': objects, 'faces': matches, 'events': events, 'untrusted': True}
                    frame = None
                    next_frame = self.clock() + 1 / camcfg['fps']  # No bursts/catch-up after slow inference.
            finally:
                frame = None
                if camera is not None:
                    try:
                        camera.close()
                    except BaseException:
                        lease.uncertain = True
                        hard_off(self.root)
                        raise VisionError('Camera release failed; hard off latched. Check physical/OS camera controls.') from None
        return {**last, 'frames_captured': count, 'frames_retained': retained, 'frame_retained': bool(retained),
                'local_only': True, 'authentication': False}
