"""First-class spoken approval intents ('yes do it', 'no cancel', 'wait') layered on ApprovalGate."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..autonomy import RiskClass, classify_action_risk
from ..tools import ApprovalDecision
from ..types import Risk, ToolSpec
from .persona import enforce_one_question
from .tts import sanitize_tts_text


class SpokenApprovalIntent(str, Enum):
    APPROVE = "approve"
    DENY = "deny"
    WAIT = "wait"
    NONE = "none"


_APPROVE_PATTERNS = (
    re.compile(r"\b(?:yes\s+do\s+it|yes\s+please\s+do\s+it|yes\s+go\s+ahead|yes\s+approve|yes\s+run\s+it|do\s+it\s+now|approve\s+it|confirm)\b", re.I),
    re.compile(r"^(?:yes|yeah|yep|go ahead|approved?)$", re.I),
    re.compile(r"(?:হ্যাঁ\s*করো|হ্যাঁ\s*চালাও|অনুমোদন\s*দিলাম|অনুমোদন)"),
)
_DENY_PATTERNS = (
    re.compile(r"\b(?:no\s+cancel|cancel\s+it|cancel\s+that|don'?t\s+do\s+it|do\s+not\s+do\s+it|stop\s+it|abort|deny)\b", re.I),
    re.compile(r"^(?:no|nope|cancel|stop|never\s*mind)$", re.I),
    re.compile(r"(?:না\s*বাতিল|বাতিল\s*করো|থামো|বাদ\s*দাও)"),
)
_WAIT_PATTERNS = (
    re.compile(r"\b(?:wait|hold\s+on|hold\s+up|pause|hang\s+on|one\s+second|give\s+me\s+a\s+second|let\s+me\s+check)\b", re.I),
    re.compile(r"(?:দাঁড়াও|অপেক্ষা\s*করো|একটু\s*থামো)"),
)


def parse_approval_intent(text: str) -> SpokenApprovalIntent:
    """Classify spoken user text into approve / deny / wait / none (strict against contradiction)."""
    cleaned = " ".join((text or "").split()).strip().rstrip(".!?।")
    if not cleaned:
        return SpokenApprovalIntent.NONE

    has_approve = any(p.search(cleaned) for p in _APPROVE_PATTERNS)
    has_deny = any(p.search(cleaned) for p in _DENY_PATTERNS)
    has_wait = any(p.search(cleaned) for p in _WAIT_PATTERNS)

    # Contradictory phrases fail closed to NONE (or DENY if denial is explicit alongside approve).
    if has_approve and has_deny:
        return SpokenApprovalIntent.DENY
    if has_deny:
        return SpokenApprovalIntent.DENY
    if has_wait and not has_approve:
        return SpokenApprovalIntent.WAIT
    if has_approve and not has_wait:
        return SpokenApprovalIntent.APPROVE
    # e.g. "wait, yes do it" or ambiguous -> do not guess APPROVE.
    if has_wait:
        return SpokenApprovalIntent.WAIT
    return SpokenApprovalIntent.NONE


@dataclass
class PendingSpokenApproval:
    spec: ToolSpec
    args: dict[str, Any]
    risk: Risk
    preview: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 120.0)
    state: str = "pending"  # pending | waiting | approved | denied
    decision: ApprovalDecision | None = None
    event: threading.Event = field(default_factory=threading.Event)


class SpokenApprovalManager:
    """Manages pending approvals for voice interactions without weakening ApprovalGate."""

    def __init__(
        self,
        *,
        workspace: Path | str | None = None,
        speak: Callable[[str, str], None] | None = None,
        listen_for_reply: Callable[[], tuple[str, bytes | None]] | None = None,
        verify_speaker: Callable[[bytes | None], tuple[bool, str]] | None = None,
        pin_callback: Callable[[str], str] | None = None,
        telegram_bot_factory: Any = None,
        require_second_factor: bool = False,
        timeout_seconds: float = 120.0,
        language: str = "en",
    ) -> None:
        self.workspace = Path(workspace).resolve() if workspace is not None else None
        self.speak = speak
        self.listen_for_reply = listen_for_reply
        self.verify_speaker = verify_speaker
        self.pin_callback = pin_callback
        self.telegram_bot_factory = telegram_bot_factory
        self.require_second_factor = bool(require_second_factor)
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self.language = language
        self._lock = threading.RLock()
        self.pending: PendingSpokenApproval | None = None
        self.history: list[dict[str, Any]] = []

    @staticmethod
    def describe_action(spec: ToolSpec, args: dict[str, Any] | None = None, preview: str = "") -> str:
        """Render a concrete, non-vague summary of the exact planned action."""
        params = args or {}
        name = spec.name
        if name == "delete_file":
            detail = f"delete file {params.get('path') or preview}"
        elif name in {"write_file", "edit_file"}:
            detail = f"{name.replace('_', ' ')} {params.get('path') or preview}"
        elif name == "run_command":
            detail = f"run command {params.get('command') or preview}"
        elif name == "git_prepare":
            branch = params.get("branch") or "current branch"
            detail = f"prepare git commit on {branch}" + (" and push" if params.get("push") else "")
        elif name == "git":
            raw_args = params.get("args") or []
            cmd = " ".join(str(x) for x in raw_args) if isinstance(raw_args, list) else str(raw_args)
            detail = f"run git {cmd or preview}".strip()
        elif name == "gh_merge_pr":
            detail = f"merge pull request {params.get('pr') or params.get('number') or preview}".strip()
        elif name == "gh_create_pr":
            detail = f"create pull request {params.get('title') or preview}".strip()
        elif name == "gh":
            raw_args = params.get("args") or []
            cmd = " ".join(str(x) for x in raw_args) if isinstance(raw_args, list) else str(raw_args)
            detail = f"run gh {cmd or preview}".strip()
        elif preview:
            detail = f"{name}: {preview}"
        else:
            detail = name
        return sanitize_tts_text(detail)[:160]

    def format_prompt(
        self,
        spec: ToolSpec,
        preview: str,
        *,
        args: dict[str, Any] | None = None,
        language: str | None = None,
    ) -> str:
        lang = (language or self.language or "en").lower()
        risk_class = classify_action_risk(spec, args or {})
        action_summary = self.describe_action(spec, args, preview)
        if lang.startswith("bn"):
            risk_label = "ঝুঁকিপূর্ণ (DANGEROUS)" if risk_class is RiskClass.DANGEROUS else risk_class.value
            return enforce_one_question(
                f"[{risk_label}] কাজটি হলো: {action_summary}। নিশ্চিত করতে হ্যাঁ করো, না বাতিল, নাকি দাঁড়াও বলুন?"
            )
        return enforce_one_question(
            f"{risk_class.value} action — {action_summary}. Say yes do it, no cancel, or wait?"
        )

    def _run_second_factor(self, item: PendingSpokenApproval, decision: ApprovalDecision) -> tuple[bool, str]:
        action_risk = classify_action_risk(item.spec, item.args)
        if action_risk is not RiskClass.DANGEROUS:
            return True, ""
        sf_cfg: dict[str, Any] = {}
        if self.workspace is not None:
            try:
                from ..permissions import Permissions
                from ..second_factor import parse_second_factor_policy

                rule = Permissions(self.workspace).evaluate(item.spec, item.args)
                sf_cfg = dict(rule.get("second_factor") or {})
                if not sf_cfg and (self.workspace / "permissions.yaml").is_file():
                    import yaml
                    raw = yaml.safe_load((self.workspace / "permissions.yaml").read_text(encoding="utf-8")) or {}
                    sf_cfg = parse_second_factor_policy(raw)
            except Exception as exc:  # noqa: BLE001
                return False, f"Second-factor policy error: {exc}"
        if self.require_second_factor and not sf_cfg.get("enabled"):
            sf_cfg = {
                "enabled": True,
                "method": "pin",
                "tools": [],
                "risks": ["danger", "dangerous"],
                "pin_env": "NEXUS_APPROVAL_PIN",
                "pin_hash_env": "NEXUS_APPROVAL_PIN_HASH",
                "max_attempts": 3,
                "lockout_seconds": 300,
                "timeout_seconds": 60,
            }
        if not sf_cfg.get("enabled"):
            return True, ""
        from ..second_factor import requires_second_factor
        from ..second_factor import verify as verify_second_factor

        if not requires_second_factor(sf_cfg, item.spec, item.args, readonly=False, tainted=False):
            return True, ""
        ws = self.workspace or Path.cwd()
        wrapped_pin_cb = None
        if self.pin_callback is not None:
            import inspect

            cb = self.pin_callback
            try:
                sig = inspect.signature(cb)
                n_pos = sum(
                    1
                    for p in sig.parameters.values()
                    if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                )
                has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
            except (ValueError, TypeError):
                n_pos, has_varargs = 1, False
            if has_varargs or n_pos >= 3:
                wrapped_pin_cb = cb
            elif n_pos == 1:
                def wrapped_pin_cb(_spec: Any, _args: dict[str, Any], prompt: str) -> str:
                    return cb(prompt)
            else:
                def wrapped_pin_cb(_spec: Any, _args: dict[str, Any], _prompt: str) -> str:
                    return cb()
        ok_2fa, msg_2fa = verify_second_factor(
            ws,
            sf_cfg,
            item.spec,
            item.args,
            item.preview,
            decision=decision,
            pin_callback=wrapped_pin_cb,
            telegram_bot_factory=self.telegram_bot_factory,
        )
        if ok_2fa:
            decision.second_factor_verified = True
            if msg_2fa:
                decision.reason = f"{decision.reason}; {msg_2fa}" if decision.reason else msg_2fa
        return ok_2fa, msg_2fa

    def submit_intent(
        self,
        text: str,
        *,
        audio_pcm: bytes | None = None,
        language: str | None = None,
    ) -> tuple[SpokenApprovalIntent, str]:
        """Apply a user utterance against the active pending approval."""
        with self._lock:
            item = self.pending
            if item is None:
                return SpokenApprovalIntent.NONE, ""
            if time.time() > item.expires_at:
                item.state = "denied"
                item.decision = ApprovalDecision(False, reason="Spoken approval timed out")
                item.event.set()
                self.pending = None
                return SpokenApprovalIntent.DENY, "Approval timed out and was cancelled."

        intent = parse_approval_intent(text)
        lang = (language or self.language or "en").lower()

        if intent is SpokenApprovalIntent.WAIT:
            with self._lock:
                if self.pending is item:
                    item.state = "waiting"
                    item.expires_at = time.time() + self.timeout_seconds
            reply = (
                "ঠিক আছে, অপেক্ষা করছি। প্রস্তুত হলে হ্যাঁ করো বা না বাতিল বলুন।"
                if lang.startswith("bn")
                else "Okay, waiting. Say yes do it or no cancel when ready."
            )
            if self.speak is not None:
                self.speak(reply, lang)
            return SpokenApprovalIntent.WAIT, reply

        if intent is SpokenApprovalIntent.DENY:
            with self._lock:
                item.state = "denied"
                item.decision = ApprovalDecision(False, reason="Cancelled via spoken intent ('no cancel')")
                item.event.set()
                self.pending = None
                self.history.append({"tool": item.spec.name, "intent": "deny"})
            reply = "বাতিল করা হয়েছে।" if lang.startswith("bn") else "Cancelled."
            if self.speak is not None:
                self.speak(reply, lang)
            return SpokenApprovalIntent.DENY, reply

        if intent is SpokenApprovalIntent.APPROVE:
            # Verify speaker biometrics before granting ApprovalDecision(True) on the voice channel.
            if self.verify_speaker is not None:
                ok_spk, reason_spk = self.verify_speaker(audio_pcm)
                if not ok_spk:
                    with self._lock:
                        item.state = "denied"
                        item.decision = ApprovalDecision(
                            False,
                            reason=reason_spk or "Spoken approval rejected by speaker verification",
                        )
                        item.event.set()
                        self.pending = None
                        self.history.append({"tool": item.spec.name, "intent": "speaker_denied"})
                    reply = "Speaker verification failed, so I cancelled it."
                    if self.speak is not None:
                        self.speak(reply, lang)
                    return SpokenApprovalIntent.DENY, reply

            candidate = ApprovalDecision(
                True,
                scope="once",
                reason="Confirmed via spoken intent ('yes do it')",
                risk_class=classify_action_risk(item.spec, item.args).value,
            )
            ok_2fa, msg_2fa = self._run_second_factor(item, candidate)
            if not ok_2fa:
                with self._lock:
                    item.state = "denied"
                    item.decision = ApprovalDecision(
                        False,
                        reason=msg_2fa or "Second-factor verification failed",
                        risk_class=candidate.risk_class,
                    )
                    item.event.set()
                    self.pending = None
                    self.history.append({"tool": item.spec.name, "intent": "second_factor_denied"})
                reply = (
                    "দ্বিতীয় ধাপের যাচাই ব্যর্থ হয়েছে, তাই বাতিল করা হলো।"
                    if lang.startswith("bn")
                    else "Second-factor verification failed, so I cancelled it."
                )
                if self.speak is not None:
                    self.speak(reply, lang)
                return SpokenApprovalIntent.DENY, reply

            with self._lock:
                item.state = "approved"
                # Always grant scope="once" so fresh confirmation invariants remain intact.
                item.decision = candidate
                item.event.set()
                self.pending = None
                self.history.append({"tool": item.spec.name, "intent": "approve"})
            reply = "ঠিক আছে, করছি।" if lang.startswith("bn") else "Confirmed, running it now."
            if self.speak is not None:
                self.speak(reply, lang)
            return SpokenApprovalIntent.APPROVE, reply

        return SpokenApprovalIntent.NONE, ""

    def request_approval(
        self,
        spec: ToolSpec,
        args: dict[str, Any],
        risk: Risk,
        preview: str,
        *,
        language: str | None = None,
    ) -> ApprovalDecision:
        """Blocking ApprovalCallback for synchronous NexusAgent tool approval."""
        lang = (language or self.language or "en").lower()
        item = PendingSpokenApproval(
            spec=spec,
            args=dict(args),
            risk=risk,
            preview=preview,
            expires_at=time.time() + self.timeout_seconds,
        )
        with self._lock:
            self.pending = item

        prompt = self.format_prompt(spec, preview, args=args, language=lang)
        if self.speak is not None:
            self.speak(prompt, lang)

        try:
            if self.listen_for_reply is not None:
                # Poll bounded reply turns so 'wait' keeps listening until approve/deny/timeout.
                for _ in range(4):
                    if item.event.is_set() or time.time() > item.expires_at:
                        break
                    text, audio = self.listen_for_reply()
                    if not text:
                        break
                    intent, _ = self.submit_intent(text, audio_pcm=audio, language=lang)
                    if intent in {SpokenApprovalIntent.APPROVE, SpokenApprovalIntent.DENY}:
                        break
                    if intent is SpokenApprovalIntent.NONE:
                        clarify = (
                            "দয়া করে হ্যাঁ করো, না বাতিল, বা দাঁড়াও বলুন।"
                            if lang.startswith("bn")
                            else "Please say yes do it, no cancel, or wait."
                        )
                        if self.speak is not None:
                            self.speak(clarify, lang)
            else:
                remaining = max(0.1, item.expires_at - time.time())
                item.event.wait(timeout=remaining)
        finally:
            with self._lock:
                if self.pending is item:
                    self.pending = None

        if item.decision is not None:
            return item.decision
        return ApprovalDecision(False, reason="Spoken approval was not confirmed (timed out / waiting)")


def build_voice_approval_callback(manager: SpokenApprovalManager):
    def _callback(spec: ToolSpec, args: dict[str, Any], risk: Risk, preview: str) -> ApprovalDecision:
        return manager.request_approval(spec, args, risk, preview)

    return _callback


__all__ = [
    "PendingSpokenApproval",
    "SpokenApprovalIntent",
    "SpokenApprovalManager",
    "build_voice_approval_callback",
    "parse_approval_intent",
]
