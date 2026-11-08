"""Presence-aware, importance-routed notifications (voice when present; Telegram/desktop when away).

Off by default: controlled via operator-owned ``autonomy.yaml`` (``notifications.enabled``).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import audit as auditlog
from .autonomy import load_autonomy_config
from .config import nexus_home
from .redaction import mask
from .redaction import secrets as known_secrets
from .studio import redact

IMPORTANCE_LEVELS = frozenset({"low", "normal", "medium", "high", "critical"})


def _sanitize_message(workspace: Path, text: str, limit: int = 320) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    masked = mask(cleaned, known_secrets(workspace))
    redacted = redact(masked)
    try:
        from .voice.tts import sanitize_tts_text

        redacted = sanitize_tts_text(redacted, workspace=workspace)
    except Exception:
        pass
    return " ".join(str(redacted).split())[:limit]


def _presence_path(workspace: Path | str) -> Path:
    root = Path(workspace).resolve()
    home = nexus_home().resolve()
    key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:32]
    directory = (home / "notifications" / key).resolve()
    if not directory.is_relative_to(home):
        raise ValueError("Notification state directory must stay inside NEXUS_HOME")
    return directory / "presence.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("Presence path must not be a symlink")
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def mark_user_present(
    workspace: Path | str,
    *,
    channel: str = "voice",
    at: float | None = None,
) -> dict[str, Any]:
    """Record a fresh user-presence heartbeat (e.g. voice wake/turn or terminal interaction)."""
    ts = float(time.time() if at is None else at)
    payload = {"present": True, "channel": str(channel or "voice")[:40], "last_seen_at": ts}
    _atomic_write_json(_presence_path(workspace), payload)
    return payload


def clear_user_presence(workspace: Path | str) -> None:
    path = _presence_path(workspace)
    if path.exists() and not path.is_symlink():
        path.unlink(missing_ok=True)


def presence_state(
    workspace: Path | str,
    *,
    now: float | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Return current user presence state without fabricating activity."""
    root = Path(workspace).resolve()
    current_time = float(time.time() if now is None else now)
    cfg = load_autonomy_config(root)
    policy = cfg.notifications
    ttl = int(policy.present_ttl_seconds if ttl_seconds is None else ttl_seconds)

    if policy.presence == "present":
        return {"present": True, "source": "autonomy.yaml:present", "last_seen_at": current_time}
    if policy.presence == "away":
        return {"present": False, "source": "autonomy.yaml:away", "last_seen_at": None}

    # Check live voice HUD state first
    try:
        from .voice.config import read_hud_state

        hud = read_hud_state(root)
        hud_state = str(hud.get("state") or "")
        observed = hud.get("observed_at")
        if hud_state in {"listening", "thinking", "speaking"}:
            return {"present": True, "source": f"voice:{hud_state}", "last_seen_at": observed or current_time}
        if isinstance(observed, (int, float)) and 0 <= current_time - float(observed) <= ttl:
            return {"present": True, "source": "voice:recent", "last_seen_at": float(observed)}
    except Exception:
        pass

    # Check persisted presence file
    path = _presence_path(root)
    if path.exists() and not path.is_symlink() and path.is_file() and path.stat().st_size <= 16_000:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("present"):
                seen = float(data.get("last_seen_at") or 0.0)
                if 0 <= current_time - seen <= ttl:
                    return {
                        "present": True,
                        "source": str(data.get("channel") or "heartbeat"),
                        "last_seen_at": seen,
                    }
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    return {"present": False, "source": "away_timeout", "last_seen_at": None}


def is_user_present(workspace: Path | str, *, now: float | None = None) -> bool:
    return bool(presence_state(workspace, now=now)["present"])


def _in_quiet_hours(hours: list[int], now_ts: float) -> bool:
    if len(hours) != 2:
        return False
    start, end = int(hours[0]), int(hours[1])
    hour = datetime.fromtimestamp(now_ts, tz=timezone.utc).hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


@dataclass
class NotificationReceipt:
    delivered: bool
    channel: str
    importance: str
    present: bool
    message: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivered": self.delivered,
            "channel": self.channel,
            "importance": self.importance,
            "present": self.present,
            "message": self.message,
            "reason": self.reason,
        }


class NotificationRouter:
    """Route notifications by importance and user presence (voice vs Telegram/desktop)."""

    def __init__(
        self,
        workspace: Path | str | None = None,
        *,
        voice_speak: Callable[[str], None] | None = None,
        telegram_sender: Callable[[str, str], bool] | None = None,
        desktop_notifier: Callable[[str, str], dict[str, Any]] | None = None,
        studio: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.voice_speak = voice_speak
        self.telegram_sender = telegram_sender
        self.desktop_notifier = desktop_notifier
        self.studio = studio
        self.clock = clock

    def _voice_available(self) -> bool:
        if self.voice_speak is not None:
            return True
        try:
            from .voice.config import load_voice_config

            return bool(load_voice_config(self.workspace).enabled)
        except Exception:
            return False

    def _send_voice(self, text: str) -> tuple[bool, str]:
        if self.voice_speak is not None:
            self.voice_speak(text)
            return True, "voice_callback"
        try:
            from .voice.audio_io import AudioOutputPlayer
            from .voice.config import load_voice_config
            from .voice.tts import create_tts

            vcfg = load_voice_config(self.workspace)
            if not vcfg.enabled:
                return False, "voice disabled in voice.yaml"
            clip = create_tts(vcfg, workspace=self.workspace).synthesize(text)
            AudioOutputPlayer(device=vcfg.output_device).play(clip)
            return True, f"tts:{vcfg.tts.provider}"
        except Exception as exc:
            return False, f"voice unavailable: {type(exc).__name__}"

    def _send_telegram(self, title: str, text: str, *, importance: str, now_ts: float) -> tuple[bool, str]:
        clean_msg = f"[{title}] {text}" if title else text
        if self.telegram_sender is not None:
            ok = bool(self.telegram_sender(title, text))
            return ok, "telegram_sender" if ok else "telegram_sender refused"
        try:
            from .server import ServerState
            from .telegram.bot import Bot
            from .telegram.bot import settings as telegram_settings

            cfg = telegram_settings(self.workspace)
            if not cfg.get("enabled"):
                return False, "telegram disabled in telegram.yaml"
            if importance != "critical" and _in_quiet_hours(cfg.get("quiet_utc_hours") or [], now_ts):
                return False, "telegram quiet_utc_hours active"
            studio = self.studio or ServerState(self.workspace, quiet=True).workbench.studio
            bot = Bot(studio)
            try:
                safe = bot.clean(clean_msg)
                for uid in cfg["user_ids"]:
                    bot.api("sendMessage", {"chat_id": uid, "text": safe})
                return True, "telegram"
            finally:
                bot.close()
        except Exception as exc:
            return False, f"telegram unavailable: {type(exc).__name__}"

    def _send_desktop(self, title: str, text: str) -> tuple[bool, str]:
        if self.desktop_notifier is not None:
            res = self.desktop_notifier(title, text)
            ok = bool(isinstance(res, dict) and res.get("submitted", True))
            return ok, "desktop_notifier" if ok else "desktop_notifier refused"
        try:
            from .jarvis.notifications import submit_text

            submit_text(title or "Nexus", text)
            return True, "desktop"
        except Exception as exc:
            return False, f"desktop unavailable: {type(exc).__name__}"

    def notify(
        self,
        message: str,
        *,
        importance: str = "medium",
        title: str = "Nexus",
        present: bool | None = None,
        allow_in_session_voice: bool = False,
    ) -> NotificationReceipt:
        imp = str(importance or "medium").lower()
        if imp not in IMPORTANCE_LEVELS:
            raise ValueError(f"Unknown notification importance '{importance}'")

        safe_title = _sanitize_message(self.workspace, title, limit=80) or "Nexus"
        safe_msg = _sanitize_message(self.workspace, message, limit=320)
        if not safe_msg:
            return NotificationReceipt(False, "none", imp, False, "", "empty message")

        now_ts = self.clock()
        cfg = load_autonomy_config(self.workspace)
        policy = cfg.notifications
        user_present = (
            bool(present)
            if present is not None
            else bool(presence_state(self.workspace, now=now_ts)["present"])
        )

        # In-session explicit voice callback still honors status narration when voice is active.
        if not policy.enabled:
            if allow_in_session_voice and user_present and self.voice_speak is not None:
                ok, reason = self._send_voice(safe_msg)
                return NotificationReceipt(ok, "voice" if ok else "disabled", imp, user_present, safe_msg, reason)
            return NotificationReceipt(
                False,
                "disabled",
                imp,
                user_present,
                safe_msg,
                "notifications disabled in autonomy.yaml",
            )

        # Respect Jarvis Inbox quiet/DND for non-critical messages.
        if imp not in {"high", "critical"}:
            try:
                from .jarvis.inbox import Inbox
                from .server import ServerState

                studio = self.studio or ServerState(self.workspace, quiet=True).workbench.studio
                delivery = Inbox(studio).delivery_policy()
                if delivery.get("quiet") or float(delivery.get("dnd_until") or 0) > now_ts:
                    return NotificationReceipt(False, "suppressed", imp, user_present, safe_msg, "quiet/DND active")
            except Exception:
                pass

        # 1. Present -> Voice
        if user_present and policy.voice_when_present and self._voice_available():
            ok, reason = self._send_voice(safe_msg)
            if ok:
                self._audit(imp, "voice", True, reason)
                return NotificationReceipt(True, "voice", imp, True, safe_msg, reason)
            if imp not in {"high", "critical"}:
                self._audit(imp, "voice", False, reason)
                return NotificationReceipt(False, "voice", imp, True, safe_msg, reason)

        # 2. Away (or high/critical fallback) -> Telegram then desktop (per away_channel)
        away = policy.away_channel
        if away == "none":
            return NotificationReceipt(False, "none", imp, user_present, safe_msg, "away_channel is none")

        reasons: list[str] = []
        if away in {"auto", "telegram"}:
            ok, reason = self._send_telegram(safe_title, safe_msg, importance=imp, now_ts=now_ts)
            if ok:
                self._audit(imp, "telegram", True, reason)
                return NotificationReceipt(True, "telegram", imp, user_present, safe_msg, reason)
            reasons.append(reason)

        if away in {"auto", "desktop"}:
            ok, reason = self._send_desktop(safe_title, safe_msg)
            if ok:
                self._audit(imp, "desktop", True, reason)
                return NotificationReceipt(True, "desktop", imp, user_present, safe_msg, reason)
            reasons.append(reason)

        detail = "; ".join(reasons) or "no delivery channel available"
        self._audit(imp, "unavailable", False, detail)
        return NotificationReceipt(False, "unavailable", imp, user_present, safe_msg, detail)

    def _audit(self, importance: str, channel: str, delivered: bool, reason: str) -> None:
        try:
            auditlog.record(
                self.workspace,
                "notify.route",
                who="nexus",
                target=channel,
                result="delivered" if delivered else "skipped",
                risk="low",
                detail={"importance": importance, "reason": reason[:160]},
            )
        except OSError:
            pass


__all__ = [
    "IMPORTANCE_LEVELS",
    "NotificationReceipt",
    "NotificationRouter",
    "clear_user_presence",
    "is_user_present",
    "mark_user_present",
    "presence_state",
]
