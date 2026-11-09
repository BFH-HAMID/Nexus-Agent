"""Consented local face embeddings, always encrypted; never an authentication factor."""
from __future__ import annotations

import hashlib
import io
import json
import math
import time
from pathlib import Path

from filelock import FileLock

from .. import audit
from ..context.core import clean
from ..jarvis.sources import bounded
from ..permissions import Permissions
from ..studio_crypto import atomic_write, os_key, seal, unseal
from ..untrusted import flag_names
from .media import face_module, pages_from_bytes
from .policy import VisionError, approve, recheck, require
from .state import check_running, control, directory


class FaceRegistry:
    def __init__(self, workspace, *, tolerance=None):
        self.root = Path(workspace).resolve()
        self.path = directory(self.root) / 'faces.enc'
        self.tolerance = tolerance  # Compatibility argument may only tighten operator policy.

    def _module(self):
        return face_module()

    def _encodings(self, image):
        try:
            return self._module().face_encodings(image)
        except VisionError:
            raise
        except Exception:
            # Native binding errors can include an ndarray repr. Never log pixels.
            raise VisionError('Local face encoding failed; no cloud fallback') from None

    def _lock(self):
        root = directory(self.root, create=True)
        lock = root / 'faces.lock'
        if lock.is_symlink() or self.path.is_symlink():
            raise VisionError('Unsafe face store/lock')
        return FileLock(str(lock), timeout=5)

    def _load(self):
        if not self.path.exists() and not self.path.is_symlink():
            return {'schema': 1, 'people': {}}
        # Authenticated ciphertext only. Legacy faces.json is deliberately never read/migrated.
        try:
            data = json.loads(unseal(bounded(self.path, 1_000_000), os_key(self.path)))
            if (not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('people'), dict)
                    or len(data['people']) > 100):
                raise VisionError('Invalid face store')
            for name, row in data['people'].items():
                self._name(name)
                if (not isinstance(row, dict) or not isinstance(row.get('consented_at'), (int, float))
                        or not isinstance(row.get('embeddings'), list) or not 1 <= len(row['embeddings']) <= 5):
                    raise VisionError('Face templates require enrollment consent metadata')
                for vector in row['embeddings']:
                    self._vector(vector)
            return data
        except (ValueError, OSError, KeyError, TypeError):
            raise VisionError('Encrypted face store unavailable, corrupt or keyring locked; no plaintext fallback') from None

    def _save(self, data):
        if self.path.is_symlink() or (self.path.exists() and self.path.stat().st_nlink != 1):
            raise VisionError('Unsafe encrypted face store')
        payload = json.dumps(data, allow_nan=False).encode()
        if len(payload) > 900_000:
            raise VisionError('Face store size limit reached')
        atomic_write(self.path, seal(payload, os_key(self.path, create=not self.path.exists())))

    def _name(self, value):
        if (not isinstance(value, str) or not value.strip() or len(value) > 80
                or any(ord(c) < 32 for c in value) or clean(value, self.root) != value or flag_names(value)):
            raise VisionError('Use a bounded non-sensitive display name')
        return value.strip()

    @staticmethod
    def _vector(value):
        try:
            if len(value) != 128 or any(isinstance(v, bool) or not math.isfinite(float(v)) or abs(float(v)) > 10 for v in value):
                raise ValueError
            return [float(v) for v in value]
        except (TypeError, ValueError, OverflowError):
            raise VisionError('Invalid local face embedding') from None

    @staticmethod
    def _image_digest(image):
        if not hasattr(image, 'tobytes') or not hasattr(image, 'shape'):
            raise VisionError('Face operations require a decoded local RGB image')
        if (len(image.shape) != 3 or image.shape[2] != 3 or image.shape[0] * image.shape[1] > 16_000_000
                or str(image.dtype) != 'uint8'):
            raise VisionError('Invalid bounded RGB face image')
        return hashlib.sha256(image.tobytes()).hexdigest()

    def load_image(self, path):
        cfg = require(self.root, 'faces')
        source = Permissions(self.root).path(str(path))
        if (self.root / path).is_symlink():
            raise VisionError('Face enrollment image cannot be a symlink')
        data = bounded(source, cfg['files']['max_bytes'])
        if source.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}:
            raise VisionError('Face enrollment requires a local image, not a document')
        pages = pages_from_bytes(source.name, data, cfg)
        try:
            return self._module().load_image_file(io.BytesIO(pages[0].image))
        except VisionError:
            raise
        except Exception:
            raise VisionError('Local face image decoding failed') from None

    def list(self):
        require(self.root, 'faces')
        with self._lock():
            data = self._load()
        return [{'name': name, 'templates': len(row['embeddings']), 'consented_at': row['consented_at']}
                for name, row in sorted(data['people'].items())]

    def enroll(self, name, image, *, consent=False, gate=None):
        cfg = require(self.root, 'faces')
        epoch = control(self.root)['epoch']
        if consent is not True:
            raise VisionError('Explicit consent from the person is required before face enrollment')
        name = self._name(name)
        fingerprint = self._image_digest(image)
        approve(self.root, gate, 'vision_face_enroll', {'person_sha256': hashlib.sha256(name.encode()).hexdigest(), 'image_sha256': fingerprint,
                'consent': 'Operator attests the person freely consented; local encrypted embeddings only'})
        check_running(self.root, epoch)
        recheck(self.root, cfg)
        if self._image_digest(image) != fingerprint:
            raise VisionError('Face image changed during approval')
        encodings = self._encodings(image)
        if len(encodings) != 1:
            raise VisionError('Enrollment requires exactly one visible face')
        vector = self._vector(encodings[0])
        check_running(self.root, epoch)
        recheck(self.root, cfg)
        with self._lock():
            data = self._load()
            people = data['people']
            if name not in people and len(people) >= cfg['faces']['max_people']:
                raise VisionError('Face enrollment capacity reached')
            row = people.setdefault(name, {'embeddings': [], 'consented_at': time.time()})
            if len(row['embeddings']) >= 5:
                raise VisionError('At most five templates per person')
            row['embeddings'].append(vector)
            row['consented_at'] = time.time()
            check_running(self.root, epoch)
            recheck(self.root, cfg)
            audit.record(self.root, 'vision.face.enroll', who='operator', target='local-encrypted',
                         detail={'person_sha256': hashlib.sha256(name.encode()).hexdigest()})
            self._save(data)
        return {'enrolled': name, 'templates': len(row['embeddings']), 'image_retained': False, 'encrypted': True}

    def recognize(self, image):
        cfg = require(self.root, 'faces')
        self._image_digest(image)
        tolerance = cfg['faces']['tolerance']
        if self.tolerance is not None:
            if type(self.tolerance) not in (int, float) or not 0.3 <= self.tolerance <= tolerance or not math.isfinite(self.tolerance):
                raise VisionError('Face tolerance may only tighten operator policy')
            tolerance = self.tolerance
        with self._lock():
            data = self._load()
        encodings = self._encodings(image)
        if len(encodings) > 20:
            raise VisionError('Face count exceeds bounded local recognition budget')
        result = []
        for encoding in encodings:
            vector = self._vector(encoding)
            scores = [(math.sqrt(sum((x - y) ** 2 for x, y in zip(vector, saved, strict=True))), name)
                      for name, row in data['people'].items() for saved in row['embeddings']]
            distance, name = min(scores, default=(None, 'unknown'))
            matched = distance is not None and distance <= tolerance
            result.append({'name': name if matched else 'unknown', 'matched': matched,
                           'distance': distance, 'tolerance': tolerance, 'authentication': False})
        recheck(self.root, cfg)
        return result

    def forget(self, name, *, gate=None):
        # Revocation is allowed even with recognition disabled; it never collects a face.
        name = self._name(name)
        approve(self.root, gate, 'vision_face_delete', {'person_sha256': hashlib.sha256(name.encode()).hexdigest(), 'effect': 'delete encrypted templates; no secure-erasure guarantee'})
        with self._lock():
            data = self._load()
            if name not in data['people']:
                return False
            del data['people'][name]
            audit.record(self.root, 'vision.face.delete', who='operator', target='local-encrypted',
                         detail={'person_sha256': hashlib.sha256(name.encode()).hexdigest()})
            self._save(data)
        return True
