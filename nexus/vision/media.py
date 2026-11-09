"""Bounded in-memory image/PDF decoding. Page numbers and regions are evidence IDs."""
from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path

from .policy import VisionError

IMAGES = {'.png', '.jpg', '.jpeg', '.webp'}
TEXT = {'.txt', '.md', '.log', '.csv', '.json'}
SUFFIXES = IMAGES | TEXT | {'.pdf'}


def pillow():
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise VisionError('Images require pip install "nexus-agent[vision-documents]"') from None
    return Image, ImageOps


def region(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(v) not in (int, float) or not 0 <= v <= 1 or not math.isfinite(v) for v in value)
            or value[0] >= value[2] or value[1] >= value[3]):
        raise VisionError('Region must be normalized [left, top, right, bottom] in 0..1')
    return list(value)


@dataclass
class Page:
    number: int
    image: bytes = b''
    text: str = ''
    bounds: tuple = (0.0, 0.0, 1.0, 1.0)

    def reference(self):
        return {'page': self.number, 'region': list(self.bounds)}


def encode_image(image, *, max_pixels=16_000_000, crop=None):
    _, ops = pillow()
    if image.width * image.height > max_pixels or min(image.size) <= 0:
        raise VisionError('Image pixel budget exceeded')
    image = ops.exif_transpose(image).convert('RGB')
    if crop is not None:
        box = region(crop)
        width, height = image.size
        image = image.crop((int(box[0] * width), int(box[1] * height),
                            max(int(box[2] * width), int(box[0] * width) + 1),
                            max(int(box[3] * height), int(box[1] * height) + 1)))
    image.thumbnail((1600, 1600))
    out = io.BytesIO()
    image.save(out, 'JPEG', quality=85)  # Reencode; never forward EXIF/embedded metadata.
    if len(out.getvalue()) > 2_000_000:
        raise VisionError('Encoded image budget exceeded')
    return out.getvalue()


def decode_image(data, *, max_pixels=16_000_000, crop=None):
    Image, _ = pillow()
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ('PNG', 'JPEG', 'WEBP') or getattr(image, 'n_frames', 1) != 1:
                raise VisionError('Only single-frame PNG, JPEG or WebP images are accepted')
            return encode_image(image, max_pixels=max_pixels, crop=crop)
    except (OSError, ValueError, Image.DecompressionBombError):
        raise VisionError('Invalid/oversized image or crop; no image was sent') from None


def pages_from_bytes(name, data, cfg, *, pages=None, crop=None):
    limits = cfg['files']
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > limits['max_bytes']:
        raise VisionError('Upload exceeds the configured byte budget or is empty')
    if not isinstance(name, str) or len(name) > 250:
        raise VisionError('Bounded upload filename required')
    suffix = Path(name).suffix.lower()
    if suffix not in SUFFIXES:
        raise VisionError('Supported uploads: PNG/JPEG/WebP, PDF and plain-text notes')
    if crop is not None:
        crop = region(crop)
    if pages is not None and (not isinstance(pages, list) or not pages or len(pages) > limits['max_pages']
            or any(type(n) is not int or n < 1 or n > 500 for n in pages) or len(set(pages)) != len(pages)):
        raise VisionError('Unique bounded one-based PDF page numbers required')
    if suffix in IMAGES:
        if pages not in (None, [1]):
            raise VisionError('Images have only page 1')
        return [Page(1, decode_image(data, max_pixels=limits['max_pixels'], crop=crop), bounds=tuple(crop or (0, 0, 1, 1)))]
    if suffix in TEXT:
        if crop is not None or pages not in (None, [1]):
            raise VisionError('Text notes have only page 1 and no crop')
        try:
            text = data.decode('utf-8')
        except UnicodeError:
            raise VisionError('Notes must be UTF-8') from None
        if len(text) > 50_000:
            raise VisionError('Text evidence exceeds 50,000 characters')
        return [Page(1, text=text)]
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise VisionError('PDF rendering requires pip install "nexus-agent[vision-documents]"') from None
    # PDFium is a native decoder, not a script/attachment/action executor. No JS forms initialization.
    try:
        document = pdfium.PdfDocument(bytes(data))
        try:
            count = len(document)
            selected = pages if pages is not None else list(range(1, min(count, limits['max_pages']) + 1))
            if count > 500 or not count or (pages is None and count > limits['max_pages']):
                raise VisionError('PDF page budget exceeded; select explicit pages')
            if any(n > count for n in selected):
                raise VisionError('Requested PDF page does not exist')
            result = []
            for number in selected:
                page = document[number - 1]
                try:
                    width, height = page.get_size()
                    if (not all(math.isfinite(v) and v > 0 for v in (width, height))
                            or width * height > limits['max_pixels']):
                        raise VisionError('PDF page dimensions exceed budget')
                    scale = min(2.0, 1600 / max(width, height), math.sqrt(limits['max_pixels'] / (width * height)))
                    bitmap = page.render(scale=scale)
                    try:
                        payload = encode_image(bitmap.to_pil(), max_pixels=limits['max_pixels'], crop=crop)
                    finally:
                        bitmap.close()
                    result.append(Page(number, payload, bounds=tuple(crop or (0, 0, 1, 1))))
                finally:
                    page.close()
            return result
        finally:
            document.close()
    except VisionError:
        raise
    except Exception:
        raise VisionError('PDF decoding failed; encrypted, corrupt or unsupported document') from None


def face_module():
    try:
        import face_recognition
    except ImportError:
        raise VisionError('Local face checks require pip install "nexus-agent[vision-faces]"; no cloud fallback') from None
    return face_recognition


def assert_no_faces(pages):
    """Cloud fail-closed preflight; camera/enrollment never enter a cloud path.

    Detection is best effort, NOT a guarantee against small/occluded faces. Operators
    must also attest the upload contains no people; use local Ollama if uncertain.
    """
    recognition = None
    for page in pages:
        if page.image:
            try:
                recognition = recognition or face_module()
                image = recognition.load_image_file(io.BytesIO(page.image))
                if recognition.face_locations(image, model='hog'):
                    raise VisionError('Faces detected: cloud upload refused; use local vision')
            except VisionError:
                raise
            except Exception:
                raise VisionError('Local face preflight failed; cloud upload refused') from None
