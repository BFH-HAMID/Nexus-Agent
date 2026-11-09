"""VLM document QA plus explicit local camera, YOLO and face adapters."""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import Any

from .common import AssistantError, atomic_bytes, clean_text

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log"}
DOCUMENT_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx"}
MAX_UPLOAD = 10_000_000


def save_upload(workspace: Path | str, name: str, data: bytes) -> Path:
    """Save one explicitly uploaded bounded file without trusting its filename."""
    from .common import assistant_dir

    suffix = Path(name).suffix.casefold()
    if suffix not in IMAGE_SUFFIXES | TEXT_SUFFIXES | DOCUMENT_SUFFIXES:
        raise AssistantError("Unsupported upload type")
    if not data or len(data) > MAX_UPLOAD:
        raise AssistantError("Upload must contain 1–10,000,000 bytes")
    root = assistant_dir(workspace) / "uploads"
    target = root / (uuid.uuid4().hex + suffix)
    atomic_bytes(target, data)
    return target


def extract_text(path: Path | str) -> str:
    source = Path(path).resolve()
    if not source.is_file() or source.is_symlink() or source.stat().st_size > MAX_UPLOAD:
        raise AssistantError("Document must be a bounded regular file")
    suffix = source.suffix.casefold()
    try:
        if suffix in TEXT_SUFFIXES:
            text = source.read_text(encoding="utf-8")
        elif suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise AssistantError('PDF support requires pip install -e ".[vision]"') from exc
            reader = PdfReader(str(source))
            if len(reader.pages) > 50:
                raise AssistantError("PDF question answering is limited to 50 pages")
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        elif suffix == ".docx":
            from docx import Document

            text = "\n".join(paragraph.text for paragraph in Document(str(source)).paragraphs)
        elif suffix == ".pptx":
            from pptx import Presentation

            presentation = Presentation(str(source))
            text = "\n".join(shape.text for slide in presentation.slides for shape in slide.shapes
                             if hasattr(shape, "text"))
        elif suffix == ".xlsx":
            from openpyxl import load_workbook

            workbook = load_workbook(str(source), read_only=True, data_only=True)
            rows: list[str] = []
            cells = 0
            for sheet in workbook.worksheets:
                rows.append(f"# Sheet: {sheet.title}")
                for row in sheet.iter_rows(values_only=True):
                    cells += len(row)
                    if cells > 20_000:
                        raise AssistantError("Spreadsheet cell budget exceeded")
                    rows.append("\t".join("" if value is None else str(value) for value in row))
            workbook.close()
            text = "\n".join(rows)
        else:
            raise AssistantError("This file needs image/VLM analysis, not text extraction")
    except AssistantError:
        raise
    except Exception as exc:
        raise AssistantError(f"Document extraction failed ({type(exc).__name__})") from exc
    return clean_text(text, 100_000, lines=True)


def _image_payload(path: Path | str) -> bytes:
    source = Path(path).resolve()
    if source.suffix.casefold() not in IMAGE_SUFFIXES or not source.is_file() or source.stat().st_size > MAX_UPLOAD:
        raise AssistantError("A bounded PNG/JPEG/WebP image is required")
    try:
        from PIL import Image

        with Image.open(source) as opened:
            opened = opened.convert("RGB")
            if opened.width * opened.height > 25_000_000:
                raise AssistantError("Image pixel budget exceeded")
            opened.thumbnail((1600, 1600))
            buffer = io.BytesIO()
            opened.save(buffer, "JPEG", quality=85)
    except AssistantError:
        raise
    except Exception as exc:
        raise AssistantError(f"Image decoding failed ({type(exc).__name__})") from exc
    if len(buffer.getvalue()) > 2_000_000:
        raise AssistantError("Encoded image budget exceeded")
    return buffer.getvalue()


class VisionQuestioner:
    """Compatibility facade over the operator-owned, no-fallback vision service."""

    def __init__(self, config, providers):
        self.config, self.providers = config, providers

    def ask(self, path: Path | str, question: str, *, agent: str | None = None,
            gate=None, cloud: bool = False, no_people: bool = False) -> dict[str, Any]:
        from ..jarvis.sources import bounded
        from ..vision.policy import require
        from ..vision.service import VisionService
        root = Path(self.config.settings.workspace or Path.cwd()).resolve()
        cfg = require(root, 'files')
        if agent not in (None, '', cfg['vlm']['model']):
            raise AssistantError('Choose the vision model in operator-owned vision.yaml; API agents cannot override it')
        service = VisionService(root)
        # Legacy explicitly uploaded files remain readable, but no arbitrary private/runtime paths.
        from .common import assistant_dir
        source = Path(path)
        upload_dir = assistant_dir(root) / 'uploads'
        if source.absolute().parent == upload_dir and not source.is_symlink() and source.resolve().parent == upload_dir:
            return service.ask_bytes(source.name, bounded(source, cfg['files']['max_bytes']), question,
                                     gate=gate, cloud=cloud, no_people=no_people)
        return service.ask_path(path, question, gate=gate, cloud=cloud, no_people=no_people)

    def screen(self, context, gate, *, question: str = "Describe visible errors briefly.",
               cloud_agent: str | None = None, no_people: bool = False) -> dict[str, Any]:
        from ..context.vision import describe
        return describe(context, gate, self.config, self.providers, question=clean_text(question, 1000),
                        cloud_agent=cloud_agent, no_people=no_people)


# Public compatibility names are retained. The old plaintext face store is not read.
from ..vision.camera import YoloDetector as YoloDetector  # noqa: E402
from ..vision.faces import FaceRegistry as FaceRegistry  # noqa: E402


def camera_once(*, detector: YoloDetector | None = None, faces: FaceRegistry | None = None,
                camera_index: int = 0, workspace=None, gate=None) -> dict[str, Any]:
    from ..vision.camera import CameraPipeline
    from ..vision.policy import require
    root = Path(workspace or (faces.root if faces else Path.cwd())).resolve()
    cfg = require(root, 'camera')
    if camera_index != cfg['camera']['device']:
        raise AssistantError('Camera selection must match operator-owned vision.yaml')
    if detector is not None and detector.weights.resolve() != (root / cfg['camera']['weights']).resolve():
        raise AssistantError('Detector weights must match operator-owned vision.yaml')
    pipeline = CameraPipeline(root, detector_factory=(lambda *a, **k: detector) if detector else None,
                              faces_factory=(lambda *a, **k: faces) if faces else None)
    return pipeline.run(gate=gate, frames=1)
