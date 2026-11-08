"""Operator-owned ``persona.yaml`` loader, spoken-style formatter, and HUD/Telegram long-output routing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import FORBIDDEN_INLINE_SECRET_KEYS, MAX_CONFIG_BYTES, VoiceError, voice_state_dir
from .tts import detect_text_language, sanitize_tts_text

_CODE_FENCE_RE = re.compile(r"```([A-Za-z0-9_+-]*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_URL_RE = re.compile(r"https?://([^\s)>\]\"']+)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$")
_HR_RE = re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$", re.MULTILINE)
_BOLD_ITALIC_RE = re.compile(r"(\*\*|__|\*|_|~~)")
_HEX_HASH_RE = re.compile(r"\b[0-9a-fA-F]{16,64}\b")
_DECIMAL_RE = re.compile(r"(?<![\w.])(\d+)\.(\d{3,})(?!\.\d)")
_LARGE_INT_RE = re.compile(r"(?<![\w.-])(\d{5,})(?![\w.-])")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?।])\s+")
_BENGALI_RE = re.compile(r"[\u0980-\u09FF]")


@dataclass(frozen=True)
class ToneConfig:
    style: str = "calm"
    concise: bool = True
    light_humour: bool = True


@dataclass(frozen=True)
class LanguagePolicyConfig:
    mirror_user: bool = True
    allow_bangla_english_mixing: bool = True
    default_language: str = "en"
    supported_languages: tuple[str, ...] = ("en", "bn", "bn-en")


@dataclass(frozen=True)
class SpokenStyleConfig:
    short_sentences: bool = True
    max_sentence_words: int = 22
    strip_markdown: bool = True
    read_lists_aloud: bool = False
    max_list_items_spoken: int = 2
    read_urls_aloud: bool = False
    summarize_numbers: bool = True
    summarize_code: bool = True
    max_spoken_chars: int = 360
    max_spoken_sentences: int = 4
    long_output_route: str = "hud"  # hud | telegram | both
    offer_summary_on_long: bool = True


@dataclass(frozen=True)
class PersonaConfig:
    schema: int = 1
    enabled: bool = False
    name: str = "Nexus"
    tone: ToneConfig = field(default_factory=ToneConfig)
    language_policy: LanguagePolicyConfig = field(default_factory=LanguagePolicyConfig)
    spoken_style: SpokenStyleConfig = field(default_factory=SpokenStyleConfig)


@dataclass(frozen=True)
class SpeechFormattingResult:
    spoken_text: str
    full_text: str
    language: str
    was_summarized: bool = False
    had_code: bool = False
    had_lists: bool = False
    had_urls: bool = False
    dispatched_to: tuple[str, ...] = ()
    hud_record_id: str = ""


def _check_mapping(data: Any, allowed: set[str], section: str) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise VoiceError(f"persona.yaml {section} must be a mapping")
    bad_secret = FORBIDDEN_INLINE_SECRET_KEYS & {str(k) for k in data}
    if bad_secret:
        raise VoiceError(f"Inline secrets are forbidden in persona.yaml ({', '.join(sorted(bad_secret))})")
    unknown = set(data) - allowed
    if unknown:
        raise VoiceError(f"Unknown persona.yaml keys in {section}: {', '.join(sorted(unknown))}")
    return data


def _req_bool(val: Any, name: str) -> bool:
    if type(val) is not bool:
        raise VoiceError(f"persona.yaml '{name}' must be a boolean")
    return val


def _req_int(val: Any, name: str, low: int, high: int) -> int:
    if type(val) is not int or not low <= val <= high:
        raise VoiceError(f"persona.yaml '{name}' must be an integer between {low} and {high}")
    return val


def _req_str(val: Any, name: str, *, max_len: int = 120) -> str:
    if not isinstance(val, str) or len(val) > max_len:
        raise VoiceError(f"persona.yaml '{name}' must be a bounded string")
    return val.strip()


def parse_persona_config(raw: Any) -> PersonaConfig:
    data = _check_mapping(
        raw,
        {"schema", "enabled", "name", "tone", "language_policy", "spoken_style"},
        "root",
    )
    schema = data.get("schema", 1)
    if type(schema) is not int or schema != 1:
        raise VoiceError("persona.yaml schema must be 1")
    enabled = _req_bool(data.get("enabled", False), "enabled")
    name = _req_str(data.get("name", "Nexus"), "name", max_len=64) or "Nexus"

    tone_raw = _check_mapping(data.get("tone"), {"style", "concise", "light_humour"}, "tone")
    style = _req_str(tone_raw.get("style", "calm"), "tone.style", max_len=32) or "calm"
    if style not in {"calm", "warm", "neutral"}:
        raise VoiceError("persona.yaml 'tone.style' must be calm, warm, or neutral")
    tone = ToneConfig(
        style=style,
        concise=_req_bool(tone_raw.get("concise", True), "tone.concise"),
        light_humour=_req_bool(tone_raw.get("light_humour", True), "tone.light_humour"),
    )

    lang_raw = _check_mapping(
        data.get("language_policy"),
        {"mirror_user", "allow_bangla_english_mixing", "default_language", "supported_languages"},
        "language_policy",
    )
    default_lang = _req_str(lang_raw.get("default_language", "en"), "language_policy.default_language", max_len=16) or "en"
    supported_raw = lang_raw.get("supported_languages", ["en", "bn", "bn-en"])
    if not isinstance(supported_raw, list) or not all(isinstance(x, str) and 1 <= len(x) <= 16 for x in supported_raw):
        raise VoiceError("persona.yaml 'language_policy.supported_languages' must be a list of language tags")
    language_policy = LanguagePolicyConfig(
        mirror_user=_req_bool(lang_raw.get("mirror_user", True), "language_policy.mirror_user"),
        allow_bangla_english_mixing=_req_bool(
            lang_raw.get("allow_bangla_english_mixing", True),
            "language_policy.allow_bangla_english_mixing",
        ),
        default_language=default_lang,
        supported_languages=tuple(x.strip() for x in supported_raw if x.strip()) or ("en", "bn", "bn-en"),
    )

    spoken_raw = _check_mapping(
        data.get("spoken_style"),
        {
            "short_sentences",
            "max_sentence_words",
            "strip_markdown",
            "read_lists_aloud",
            "max_list_items_spoken",
            "read_urls_aloud",
            "summarize_numbers",
            "summarize_code",
            "max_spoken_chars",
            "max_spoken_sentences",
            "long_output_route",
            "offer_summary_on_long",
        },
        "spoken_style",
    )
    route = _req_str(spoken_raw.get("long_output_route", "hud"), "spoken_style.long_output_route", max_len=24) or "hud"
    if route not in {"hud", "telegram", "both"}:
        raise VoiceError("persona.yaml 'spoken_style.long_output_route' must be hud, telegram, or both")
    spoken_style = SpokenStyleConfig(
        short_sentences=_req_bool(spoken_raw.get("short_sentences", True), "spoken_style.short_sentences"),
        max_sentence_words=_req_int(spoken_raw.get("max_sentence_words", 22), "spoken_style.max_sentence_words", 6, 80),
        strip_markdown=_req_bool(spoken_raw.get("strip_markdown", True), "spoken_style.strip_markdown"),
        read_lists_aloud=_req_bool(spoken_raw.get("read_lists_aloud", False), "spoken_style.read_lists_aloud"),
        max_list_items_spoken=_req_int(spoken_raw.get("max_list_items_spoken", 2), "spoken_style.max_list_items_spoken", 0, 10),
        read_urls_aloud=_req_bool(spoken_raw.get("read_urls_aloud", False), "spoken_style.read_urls_aloud"),
        summarize_numbers=_req_bool(spoken_raw.get("summarize_numbers", True), "spoken_style.summarize_numbers"),
        summarize_code=_req_bool(spoken_raw.get("summarize_code", True), "spoken_style.summarize_code"),
        max_spoken_chars=_req_int(spoken_raw.get("max_spoken_chars", 360), "spoken_style.max_spoken_chars", 80, 2000),
        max_spoken_sentences=_req_int(spoken_raw.get("max_spoken_sentences", 4), "spoken_style.max_spoken_sentences", 1, 12),
        long_output_route=route,
        offer_summary_on_long=_req_bool(spoken_raw.get("offer_summary_on_long", True), "spoken_style.offer_summary_on_long"),
    )

    return PersonaConfig(
        schema=1,
        enabled=enabled,
        name=name,
        tone=tone,
        language_policy=language_policy,
        spoken_style=spoken_style,
    )


def load_persona_config(workspace: Path | str | None = None) -> PersonaConfig:
    root = Path(workspace or Path.cwd()).resolve()
    path = root / "persona.yaml"
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES)):
        raise VoiceError("persona.yaml must be a bounded regular operator-owned YAML file")
    if not path.exists():
        return PersonaConfig()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise VoiceError(f"Malformed persona.yaml ({type(exc).__name__})") from exc
    return parse_persona_config(raw if raw is not None else {})


def resolve_reply_language(persona: PersonaConfig, user_language: str | None, user_text: str = "") -> str:
    """Resolve the target reply language mirroring what the user spoke."""
    if not persona.language_policy.mirror_user:
        return persona.language_policy.default_language
    norm = (user_language or "").strip().lower()
    has_bn = bool(_BENGALI_RE.search(user_text or ""))
    has_en = bool(re.search(r"[A-Za-z]{2,}", user_text or ""))
    if has_bn and has_en and persona.language_policy.allow_bangla_english_mixing:
        return "bn-en"
    if norm in {"bn-en", "bn_en", "mixed"}:
        return "bn-en" if persona.language_policy.allow_bangla_english_mixing else "bn"
    if norm.startswith("bn") or has_bn:
        return "bn-en" if (has_en and persona.language_policy.allow_bangla_english_mixing) else "bn"
    if norm.startswith("en") or has_en:
        return "en"
    return persona.language_policy.default_language


def build_persona_prompt(persona: PersonaConfig, *, user_language: str = "en", user_text: str = "") -> str:
    """Render system-prompt guidance from ``persona.yaml`` when enabled."""
    if not persona.enabled:
        return ""
    target_lang = resolve_reply_language(persona, user_language, user_text)
    if target_lang == "bn":
        lang_rule = "Reply in natural Bengali (Bangla)."
    elif target_lang == "bn-en":
        lang_rule = (
            "The user mixed Bangla and English. Reply naturally in Bangla with concise English technical terms "
            "where appropriate."
        )
    else:
        lang_rule = "Reply in English."

    humour_note = (
        "Add subtle, dry light humour only when natural — never slapstick or verbose."
        if persona.tone.light_humour
        else "Keep a straightforward tone without jokes."
    )
    return (
        f"# Voice Persona: {persona.name}\n"
        f"- Tone: {persona.tone.style}" + (", concise" if persona.tone.concise else "") + f". {humour_note}\n"
        f"- Language policy: {lang_rule}\n"
        "- Spoken-style rules:\n"
        "  * Use short, direct sentences suited for listening.\n"
        "  * Do NOT use markdown formatting (# headings, **bold**, backticks, tables, bullet lists).\n"
        "  * Never read raw URLs, long file paths, code blocks, or exact multi-digit numbers aloud — summarize them.\n"
        "  * If the answer requires a long list, code, or detailed breakdown, give a short spoken summary and "
        "offer to send the full text to the HUD or Telegram.\n"
        "  * If a request is ambiguous, ask at most ONE short clarifying question."
    )


def _summarize_number_for_speech(num_str: str) -> str:
    try:
        val = int(num_str)
    except ValueError:
        return num_str
    if val >= 1_000_000_000:
        return f"about {round(val / 1_000_000_000, 1):g} billion"
    if val >= 1_000_000:
        return f"about {round(val / 1_000_000, 1):g} million"
    if val >= 10_000:
        return f"about {round(val / 1_000)} thousand"
    return f"about {round(val / 100) * 100}"


def _format_url_summary(url_body: str, *, language: str) -> str:
    host = url_body.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return "একটা লিংক" if language.startswith("bn") else "a link"
    return f"{host}-এর লিংক" if language.startswith("bn") else f"a link to {host}"


def enforce_one_question(text: str) -> str:
    """Ensure clarifying speech asks at most ONE short question."""
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return ""
    if cleaned.count("?") <= 1:
        return cleaned
    first_q_end = cleaned.find("?")
    return cleaned[: first_q_end + 1].strip()


def _shorten_sentence(sentence: str, max_words: int) -> list[str]:
    words = sentence.split()
    if len(words) <= max_words:
        return [sentence]
    parts: list[str] = []
    # Prefer splitting on natural clause punctuation before word-chunking.
    clauses = [c.strip() for c in re.split(r"[;:]|,\s+(?=(?:and|but|so|or|which|that)\b)", sentence) if c.strip()]
    if len(clauses) > 1 and all(len(c.split()) <= max_words for c in clauses):
        for idx, cl in enumerate(clauses):
            if cl[-1] not in ".!?।":
                cl = cl + ("." if idx < len(clauses) - 1 or sentence[-1] not in ".!?।" else sentence[-1])
            parts.append(cl)
        return parts
    # Chunk gently with clean punctuation.
    end_punct = sentence[-1] if sentence[-1] in ".!?।" else "."
    for i in range(0, len(words), max_words):
        chunk = " ".join(words[i : i + max_words]).rstrip(",;:")
        if chunk and chunk[-1] not in ".!?।":
            chunk += end_punct if i + max_words >= len(words) else "."
        parts.append(chunk)
    return parts


def _route_label(route: str, language: str) -> str:
    if language.startswith("bn"):
        if route == "telegram":
            return "Telegram-এ"
        if route == "both":
            return "HUD এবং Telegram-এ"
        return "HUD-এ"
    if route == "telegram":
        return "Telegram"
    if route == "both":
        return "the HUD and Telegram"
    return "the HUD"


def format_for_speech(
    text: str,
    persona: PersonaConfig,
    *,
    user_language: str | None = None,
    workspace: Path | str | None = None,
    dispatch_full: Callable[[str, str], tuple[str, ...]] | None = None,
) -> SpeechFormattingResult:
    """Transform an arbitrary agent reply into clean, concise, secret-redacted spoken prose.

    Enforces:
    - no markdown syntax,
    - no raw lists or raw URLs read aloud verbatim,
    - code blocks and long/decimal numbers summarized,
    - short sentences,
    - long output summarized aloud while sending the full redacted text to HUD / Telegram.
    """
    safe_full = sanitize_tts_text(text, workspace=workspace)
    raw_safe_multiline = _mask_preserve_lines(text, workspace=workspace)
    lang = resolve_reply_language(persona, user_language or detect_text_language(safe_full), safe_full)

    if not persona.enabled:
        return SpeechFormattingResult(
            spoken_text=safe_full,
            full_text=raw_safe_multiline.strip() or safe_full,
            language=lang,
        )

    style = persona.spoken_style
    working = raw_safe_multiline
    had_code = False
    had_lists = False
    had_urls = False

    # 1. Summarize fenced code blocks before stripping inline markdown.
    if style.summarize_code:
        def _replace_code(match: re.Match[str]) -> str:
            nonlocal had_code
            had_code = True
            code_lang = (match.group(1) or "").strip()
            body = (match.group(2) or "").strip()
            lines = max(1, len(body.splitlines())) if body else 1
            if lang.startswith("bn"):
                prefix = f"{code_lang} " if code_lang else ""
                return f"এখানে {lines} লাইনের একটা {prefix}কোড স্নিপেট আছে।"
            prefix = f"{code_lang} " if code_lang else ""
            return f"There is a {lines}-line {prefix}code snippet."

        working = _CODE_FENCE_RE.sub(_replace_code, working)

    # 2. Collapse bullet / numbered lists instead of reading raw list items aloud.
    raw_lines = working.splitlines()
    rebuilt_lines: list[str] = []
    list_buffer: list[str] = []

    def _flush_list() -> None:
        nonlocal had_lists
        if not list_buffer:
            return
        had_lists = True
        count = len(list_buffer)
        if style.read_lists_aloud:
            rebuilt_lines.extend(list_buffer)
        else:
            preview = "; ".join(item.rstrip(".;:") for item in list_buffer[: style.max_list_items_spoken])
            if lang.startswith("bn"):
                if preview and count > style.max_list_items_spoken:
                    rebuilt_lines.append(f"মোট {count}টা আইটেম আছে, যেমন {preview}।")
                elif preview:
                    rebuilt_lines.append(f"{count}টা আইটেম আছে: {preview}।")
                else:
                    rebuilt_lines.append(f"মোট {count}টা আইটেম আছে।")
            else:
                if preview and count > style.max_list_items_spoken:
                    rebuilt_lines.append(f"There are {count} items, starting with {preview}.")
                elif preview:
                    rebuilt_lines.append(f"There are {count} items: {preview}.")
                else:
                    rebuilt_lines.append(f"There are {count} items.")
        list_buffer.clear()

    for line in raw_lines:
        m = _LIST_LINE_RE.match(line)
        if m:
            item_text = m.group(1).strip()
            if item_text:
                list_buffer.append(item_text)
        else:
            _flush_list()
            rebuilt_lines.append(line)
    _flush_list()
    working = "\n".join(rebuilt_lines)

    # 3. Strip markdown & summarize URLs.
    if style.strip_markdown:
        working = _TABLE_SEP_RE.sub(" ", working)
        working = _HEADING_RE.sub("", working)
        working = _BLOCKQUOTE_RE.sub("", working)
        working = _HR_RE.sub(" ", working)

    def _replace_md_link(match: re.Match[str]) -> str:
        nonlocal had_urls
        had_urls = True
        label = match.group(1).strip()
        url_body = match.group(2).removeprefix("https://").removeprefix("http://")
        if style.read_urls_aloud:
            return f"{label} ({match.group(2)})"
        return label or _format_url_summary(url_body, language=lang)

    working = _MD_LINK_RE.sub(_replace_md_link, working)

    if not style.read_urls_aloud:
        def _replace_bare_url(match: re.Match[str]) -> str:
            nonlocal had_urls
            had_urls = True
            return _format_url_summary(match.group(1), language=lang)

        working = _URL_RE.sub(_replace_bare_url, working)

    if style.strip_markdown:
        working = _INLINE_CODE_RE.sub(lambda m: m.group(1), working)
        working = _BOLD_ITALIC_RE.sub("", working)
        working = working.replace("|", ", ")

    # 4. Summarize long hashes, long decimals, and large integers.
    if style.summarize_numbers:
        working = _HEX_HASH_RE.sub("a short identifier" if not lang.startswith("bn") else "একটা আইডি", working)
        working = _DECIMAL_RE.sub(lambda m: f"about {round(float(m.group(0)), 1):g}", working)
        working = _LARGE_INT_RE.sub(lambda m: _summarize_number_for_speech(m.group(1)), working)

    normalized = " ".join(working.split()).strip()
    if not normalized:
        normalized = "Done." if not lang.startswith("bn") else "হয়েছে।"

    # 5. Shorten overly long sentences.
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(normalized) if s.strip()]
    if not sentences:
        sentences = [normalized]
    if style.short_sentences:
        shortened: list[str] = []
        for sent in sentences:
            shortened.extend(_shorten_sentence(sent, style.max_sentence_words))
        sentences = shortened

    joined = " ".join(sentences)
    is_long = (
        len(safe_full) > style.max_spoken_chars
        or len(joined) > style.max_spoken_chars
        or len(sentences) > style.max_spoken_sentences
        or had_code
        or (had_lists and len(safe_full) > 220)
    )

    dispatched: tuple[str, ...] = ()
    record_id = ""
    if is_long:
        summary_sentences = sentences[: max(1, min(2, style.max_spoken_sentences))]
        summary_body = " ".join(summary_sentences).strip()
        if len(summary_body) > style.max_spoken_chars:
            summary_body = summary_body[: max(40, style.max_spoken_chars - 60)].rsplit(" ", 1)[0].rstrip(",;:") + "."
        if summary_body and summary_body[-1] not in ".!?।":
            summary_body += "।" if lang.startswith("bn") else "."

        if dispatch_full is not None:
            dispatched = tuple(dispatch_full(raw_safe_multiline.strip() or safe_full, style.long_output_route))
        else:
            dispatched, record_id = dispatch_long_output(
                workspace=workspace,
                full_text=raw_safe_multiline.strip() or safe_full,
                summary=summary_body,
                route=style.long_output_route,
                language=lang,
            )

        if style.offer_summary_on_long:
            dest_label = _route_label(style.long_output_route, lang)
            if lang.startswith("bn"):
                notice = f"বিস্তারিত পুরো লেখাটা {dest_label} পাঠিয়ে দিয়েছি। কোনো অংশ নিয়ে বলব?"
            else:
                notice = f"I sent the full details to {dest_label}. Want me to go into any part?"
            spoken_final = f"{summary_body} {notice}".strip()
        else:
            spoken_final = summary_body
        spoken_final = enforce_one_question(spoken_final)
        return SpeechFormattingResult(
            spoken_text=spoken_final,
            full_text=raw_safe_multiline.strip() or safe_full,
            language=lang,
            was_summarized=True,
            had_code=had_code,
            had_lists=had_lists,
            had_urls=had_urls,
            dispatched_to=dispatched,
            hud_record_id=record_id,
        )

    spoken_final = enforce_one_question(joined)
    return SpeechFormattingResult(
        spoken_text=spoken_final,
        full_text=raw_safe_multiline.strip() or safe_full,
        language=lang,
        was_summarized=False,
        had_code=had_code,
        had_lists=had_lists,
        had_urls=had_urls,
    )


def _mask_preserve_lines(text: str, *, workspace: Path | str | None = None) -> str:
    from ..redaction import mask, secrets
    from ..studio import redact

    secret_list = list(secrets(workspace))
    cleaned = mask(text or "", secret_list)
    cleaned = redact(cleaned)
    return cleaned


def dispatch_long_output(
    *,
    workspace: Path | str | None,
    full_text: str,
    summary: str,
    route: str = "hud",
    language: str = "en",
    telegram_sender: Callable[[str], bool] | None = None,
) -> tuple[tuple[str, ...], str]:
    """Send full redacted text to HUD feed (and optionally whitelisted Telegram)."""
    ws = Path(workspace or Path.cwd()).resolve()
    safe_full = _mask_preserve_lines(full_text, workspace=ws)[:32_000]
    safe_summary = sanitize_tts_text(summary, workspace=ws)[:400]
    sent: list[str] = []
    record_id = hashlib.sha256(f"{time.time_ns()}:{safe_summary}".encode()).hexdigest()[:12]

    if route in {"hud", "both"}:
        feed_path = voice_state_dir(ws) / "hud_messages.json"
        feed_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not feed_path.is_symlink():
            existing: list[dict[str, Any]] = []
            if feed_path.is_file():
                try:
                    loaded = json.loads(feed_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        existing = loaded
                except (OSError, json.JSONDecodeError):
                    existing = []
            existing.append(
                {
                    "id": record_id,
                    "at": time.time(),
                    "language": language,
                    "summary": safe_summary,
                    "full_text": safe_full,
                }
            )
            existing = existing[-25:]
            tmp = feed_path.with_name(f".{feed_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(existing, ensure_ascii=False, indent=2) + "\n")
                os.replace(tmp, feed_path)
                sent.append("hud")
            finally:
                tmp.unlink(missing_ok=True)

    if route in {"telegram", "both"}:
        if telegram_sender is not None:
            try:
                if telegram_sender(safe_full[:3500]):
                    sent.append("telegram")
            except Exception:
                pass
        else:
            if _send_via_whitelisted_telegram(ws, safe_full):
                sent.append("telegram")

    return tuple(sent), record_id


def _send_via_whitelisted_telegram(workspace: Path, safe_text: str) -> bool:
    """Deliver long voice output ONLY when telegram.yaml explicitly enables whitelisted users."""
    try:
        from ..studio import Studio
        from ..telegram.bot import Bot
        from ..telegram.bot import settings as telegram_settings
        from ..workbench import WorkbenchState

        cfg = telegram_settings(workspace)
        if not cfg.get("enabled") or not cfg.get("user_ids"):
            return False
        if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_APPROVAL_PIN"):
            return False
        studio = Studio(WorkbenchState(workspace))
        bot = Bot(studio)
        try:
            for user_id in cfg["user_ids"]:
                bot.api("sendMessage", {"chat_id": user_id, "text": bot.clean(safe_text)})
            return True
        finally:
            bot.close()
    except Exception:
        return False


def read_hud_messages(workspace: Path | str | None = None, *, limit: int = 10) -> list[dict[str, Any]]:
    feed_path = voice_state_dir(workspace) / "hud_messages.json"
    if not feed_path.is_file() or feed_path.is_symlink() or feed_path.stat().st_size > 256_000:
        return []
    try:
        rows = json.loads(feed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    return rows[-max(1, limit) :]


__all__ = [
    "LanguagePolicyConfig",
    "PersonaConfig",
    "SpeechFormattingResult",
    "SpokenStyleConfig",
    "ToneConfig",
    "build_persona_prompt",
    "dispatch_long_output",
    "enforce_one_question",
    "format_for_speech",
    "load_persona_config",
    "parse_persona_config",
    "read_hud_messages",
    "resolve_reply_language",
]
