"""One no-tool question-answering boundary for CLI, HUD, Telegram and voice."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from .. import audit
from ..context.core import Context, clean
from ..jarvis.sources import bounded
from ..permissions import Permissions
from ..untrusted import flag_names
from .media import Page, pages_from_bytes
from .policy import VisionError, approve, digest, recheck, require
from .state import check_running, control
from .vlm import VLMAdapter

SCREEN_REQUEST = re.compile(
    r"(?i)^(?:please\s+)?(?:what(?:'s| is) on my screen|explain this chart|fix this error|"
    r"(?:describe|explain|look at) (?:my|this) (?:screen|window)|"
    r"আমার স্ক্রিনে কী আছে|এই চার্ট ব্যাখ্যা করো|এই এরর ঠিক করো)[.!?]?\s*$"
)
LOOK_REQUEST = re.compile(r'(?i)^(?:look at this|এই ছবিটি দেখো)(?:\s+(.+))?[.!?]?$')


class VisionService:
    def __init__(self, workspace, *, adapter_factory=None):
        self.root = Path(workspace).resolve()
        self.adapter_factory = adapter_factory or VLMAdapter

    def _answer(self, pages, question, cfg, gate, *, cloud=False, no_people=False, source_digest='', extra_guard=None):
        require(self.root, 'vlm')
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise VisionError('A question of 1–4000 characters is required')
        if type(cloud) is not bool or type(no_people) is not bool:
            raise VisionError('Explicit boolean cloud/no-people choices required')
        adapter = self.adapter_factory(self.root, cfg)
        if adapter.cloud != cloud or (cloud and not cfg['vlm']['allow_cloud']):
            raise VisionError('Cloud vision needs explicit YAML enable AND a per-request cloud choice; no fallback')
        epoch = control(self.root)['epoch']
        def guard():
            check_running(self.root, epoch)
            recheck(self.root, cfg)
            if extra_guard:
                extra_guard()
        guard()
        if cloud:
            if not no_people:
                raise VisionError('Cloud images require an explicit no-people attestation; use local vision if uncertain')
            from .media import assert_no_faces
            assert_no_faces(pages)
        fingerprints = [hashlib.sha256(p.image or p.text.encode()).hexdigest() for p in pages]
        args = {'endpoint': adapter.endpoint, 'model': cfg['vlm']['model'], 'cloud': cloud,
                'pages': [p.reference() for p in pages], 'sha256': source_digest,
                'evidence_sha256': fingerprints, 'question_sha256': hashlib.sha256(clean(question, self.root).encode()).hexdigest(),
                'policy': digest(cfg), 'no_people': no_people,
                'warning': 'Untrusted evidence; cloud may incur cost. Face/OCR detection is best effort.'}
        checked_gate = approve(self.root, gate, 'vision_cloud_upload' if cloud else 'vision_analyze', args,
                               readonly=not cloud)
        guard()
        from .. import costcenter, limits
        try:
            costcenter.enforce(self.root)
            limits.check_daily_caps(self.root)
        except (costcenter.BudgetExceeded, limits.LimitExceeded):
            raise VisionError('Configured daily/cost budget refuses vision analysis') from None
        audit.record(self.root, 'vision.analyze', who='operator', target='cloud' if cloud else 'local',
                     result='requested', detail={'pages': [p.number for p in pages], 'sha256': source_digest})
        result = adapter.answer(pages, clean(question, self.root), guard=guard)
        guard()
        flags = sorted(set(result['injection_flags']) | {flag for p in pages for flag in flag_names(p.text)})
        checked_gate.mark_tainted('vision:answer', flags)
        result.update(injection_flags=flags, uploaded=cloud, local=not cloud, sha256=source_digest)
        return result

    def ask_bytes(self, name, data, question, *, gate=None, channel='cli', cloud=False, no_people=False,
                  pages=None, crop=None):
        cfg = require(self.root, 'files', channel=channel)
        epoch = control(self.root)['epoch']
        decoded = pages_from_bytes(name, data, cfg, pages=pages, crop=crop)
        try:
            return self._answer(decoded, question, cfg, gate, cloud=cloud, no_people=no_people,
                                source_digest=hashlib.sha256(data).hexdigest(),
                                extra_guard=lambda: check_running(self.root, epoch))
        finally:
            decoded.clear()

    def ask_path(self, path, question, **kwargs):
        cfg = require(self.root, 'files', channel=kwargs.get('channel', 'cli'))
        source = Permissions(self.root).path(str(path))
        # Check the lexical path too: resolving before testing symlinks hides aliases.
        lexical = self.root / path
        if lexical.is_symlink() or source.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp', '.pdf', '.txt', '.md', '.log', '.csv', '.json'}:
            raise VisionError('Use an explicit regular workspace image, PDF or note')
        data = bounded(source, cfg['files']['max_bytes'])
        recheck(self.root, cfg)
        return self.ask_bytes(source.name, data, question, **kwargs)

    def screen(self, question, *, gate=None, channel='cli', cloud=False, no_people=False, context=None):
        cfg = require(self.root, 'screen', channel=channel)
        require(self.root, 'vlm')
        from ..context.vision import capture
        ctx = context or Context(self.root)
        if ctx.root != self.root:
            raise VisionError('Screen context workspace mismatch')
        context_policy = ctx.digest()
        epoch = control(self.root)['epoch']
        from ..context.core import settings as context_settings
        expires = time.monotonic() + context_settings(self.root)['ttl_seconds']
        window = ctx.desktop()
        def guard():
            check_running(self.root, epoch)
            if time.monotonic() >= expires or ctx.desktop() != window:
                raise VisionError('Screen reference expired or foreground window changed')
            if ctx.digest() != context_policy:
                raise VisionError('Screen privacy policy changed')
        recheck(self.root, cfg)
        payload = capture(ctx, gate)
        try:
            return self._answer([Page(1, bytes(payload))], question, cfg, gate, cloud=cloud,
                                no_people=no_people, source_digest=hashlib.sha256(payload).hexdigest(),
                                extra_guard=guard)
        finally:
            payload[:] = b'\x00' * len(payload)
            payload.clear()

    def look(self, utterance, *, gate=None, attachment=None, channel='voice'):
        """Literal user intent only. OCR/VLM results never re-enter this dispatcher."""
        if not isinstance(utterance, str):
            return None
        match = LOOK_REQUEST.fullmatch(utterance.strip())
        if match:
            require(self.root, 'files', channel=channel)
            if attachment is not None:
                name, data = attachment
                return self.ask_bytes(name, data, 'Explain this image or document with references.', gate=gate, channel=channel)
            if match.group(1):
                path = match.group(1).strip().strip('"\'')
                return self.ask_path(path, 'Explain this image or document with references.', gate=gate, channel=channel)
            return {'answer': 'Which image or PDF do you mean? Say look at this followed by its workspace filename, or ask what is on my screen.',
                    'clarification': True}
        if SCREEN_REQUEST.search(utterance):
            return self.screen(utterance, gate=gate, channel=channel)
        return None
