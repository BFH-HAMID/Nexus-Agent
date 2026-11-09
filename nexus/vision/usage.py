"""Metadata-only model receipts in the existing cost/session observation ledger.

Never record the question, image, OCR, filename, answer, credentials or embeddings.
Direct VLM dollar pricing is unknown, not zero; existing unknown-price budget
policy can block further calls. Provider-reported token counts feed daily caps.
"""
from __future__ import annotations

from ..evaluation.trace import Trace
from ..sessions import SessionStore
from ..types import Completion
from .policy import VisionError


class _PendingReceipt:
    def save_observation(self, *unused):
        # Cost Center imports each observation ID once. Do not publish an empty
        # in-progress model list that it could import before the response arrives.
        pass


class UsageReceipt:
    def __init__(self, workspace, model, endpoint):
        self.model, self.endpoint = model, endpoint
        self.workspace, self.store = workspace, SessionStore()
        self.trace = Trace(_PendingReceipt(), workspace, 'Vision analysis; evidence and answer withheld', lambda: {})
        self.trace.body['kind'] = 'vision_analysis'
        self.trace.request('vision', '[VISION_CONTENT_OMITTED]', [], [])
        self._check()

    def _check(self):
        if not self.trace.body['complete']:
            raise VisionError('Vision usage journal unavailable; no unaccounted retry')

    def wire(self):
        self.trace.wire('vision', {'model': self.model, 'endpoint': self.endpoint, 'content': '[OMITTED]'})
        self._check()

    def response(self, value):
        usage = value.get('usage') if isinstance(value, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        counts = [usage.get('prompt_tokens'), usage.get('completion_tokens')]
        known = all(type(v) is int and 0 <= v < 10**9 for v in counts)
        bounded_usage = {'prompt_tokens': counts[0], 'completion_tokens': counts[1]} if known else {}
        self.trace.response('vision', Completion(model=self.model, input_tokens=counts[0] if known else 0,
            output_tokens=counts[1] if known else 0, raw={'usage': bounded_usage}, content=[]))
        self._check()
        return {'input_tokens': counts[0] if known else None, 'output_tokens': counts[1] if known else None,
                'estimated_cost_usd': None, 'actual_cost_usd': None}

    def finish(self, *, failed=False):
        self.trace.finish(error='Vision request failed/revoked; charges may be uncertain' if failed else None)
        try:
            self.store.save_observation(self.trace.id, self.workspace, self.trace.body)
            from .. import costcenter
            costcenter.sync(self.workspace, force=True)
        except Exception:
            raise VisionError('Vision usage receipt unavailable; request may have incurred charges. Do not retry automatically.') from None
