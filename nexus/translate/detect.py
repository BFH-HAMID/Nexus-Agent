"""Which language is this? Offline, by writing system first and common words second.

Non-Latin scripts identify themselves (Bengali, Arabic, Cyrillic ...). For the
Latin alphabet the commonest function words decide. This is a hint for
choosing a translation direction, not a linguistic authority: short Latin
text, names, and Bengali typed in Latin letters cannot be told apart.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any

LANGUAGES = {
    "en": "English", "bn": "Bengali", "hi": "Hindi", "ur": "Urdu", "ar": "Arabic", "fa": "Persian", "es": "Spanish",
    "fr": "French", "de": "German", "pt": "Portuguese", "it": "Italian", "nl": "Dutch", "ru": "Russian",
    "uk": "Ukrainian", "tr": "Turkish", "id": "Indonesian", "ms": "Malay", "zh": "Chinese", "ja": "Japanese",
    "ko": "Korean", "th": "Thai", "vi": "Vietnamese", "ta": "Tamil", "te": "Telugu", "gu": "Gujarati",
    "pa": "Punjabi", "mr": "Marathi", "ne": "Nepali", "el": "Greek", "he": "Hebrew", "pl": "Polish", "sv": "Swedish",
}
_ALIASES = {"bangla": "bn", "bengali": "bn", "farsi": "fa", "mandarin": "zh", "castilian": "es"}
_BY_NAME = {name.casefold(): code for code, name in LANGUAGES.items()} | _ALIASES

# (first code point, last code point, script). The first matching range wins.
_RANGES = (
    (0x0041, 0x024F, "Latin"), (0x1E00, 0x1EFF, "Latin"), (0x0370, 0x03FF, "Greek"), (0x0400, 0x052F, "Cyrillic"),
    (0x0590, 0x05FF, "Hebrew"), (0x0600, 0x06FF, "Arabic"), (0x0750, 0x077F, "Arabic"), (0xFB50, 0xFDFF, "Arabic"),
    (0xFE70, 0xFEFF, "Arabic"), (0x0900, 0x097F, "Devanagari"), (0x0980, 0x09FF, "Bengali"),
    (0x0A00, 0x0A7F, "Gurmukhi"), (0x0A80, 0x0AFF, "Gujarati"), (0x0B80, 0x0BFF, "Tamil"), (0x0C00, 0x0C7F, "Telugu"),
    (0x0E00, 0x0E7F, "Thai"), (0x1100, 0x11FF, "Hangul"), (0xAC00, 0xD7AF, "Hangul"), (0x3040, 0x30FF, "Kana"),
    (0x3400, 0x4DBF, "Han"), (0x4E00, 0x9FFF, "Han"),
)
_SCRIPT_LANGUAGE = {"Bengali": "bn", "Devanagari": "hi", "Arabic": "ar", "Cyrillic": "ru", "Greek": "el",
                    "Hebrew": "he", "Thai": "th", "Hangul": "ko", "Kana": "ja", "Han": "zh", "Tamil": "ta",
                    "Telugu": "te", "Gujarati": "gu", "Gurmukhi": "pa"}
# the script a correct translation into this language is written in
SCRIPT_OF = {"bn": "Bengali", "hi": "Devanagari", "mr": "Devanagari", "ne": "Devanagari", "ur": "Arabic",
             "ar": "Arabic", "fa": "Arabic", "ru": "Cyrillic", "uk": "Cyrillic", "el": "Greek", "he": "Hebrew",
             "th": "Thai", "ko": "Hangul", "ja": "Kana", "zh": "Han", "ta": "Tamil", "te": "Telugu", "gu": "Gujarati",
             "pa": "Gurmukhi"}
_LATIN_WORDS = {
    "en": "the and is are was were of to in that it for with as this have not you be on at by from or but what which "
          "all my your we they he she do does can will would how there here when who them then than into just also "
          "am been his her their its our me has had if so yes good please thank hello today",
    "es": "el la los las de que y en un una es por con para no se su al lo como más pero sus le ya está muy "
          "yo tú usted nosotros ellos hola gracias buenos días hoy mañana qué cómo dónde cuando porque también todo",
    "fr": "le la les de des et est en un une que qui dans pour pas sur au avec ce il elle nous vous ne se sont "
          "je tu bonjour merci aujourd'hui demain où quand comment pourquoi aussi tout très mais ou donc cette ces",
    "de": "der die das und ist nicht ein eine zu den mit von für auf dem des sich auch es im dass wir sie ich war "
          "du ihr hallo danke heute morgen wo wann wie warum sehr aber oder wenn noch nur schon kann wird haben",
    "pt": "o a os as de que e em um uma é por com para não se da do na no mais como mas ao dos foi são você",
    "it": "il la le di che e è in un una per con non si da del della dei sono come più ma anche gli lo al ho",
    "nl": "de het een en is niet van te dat in op voor met zijn er aan ook als maar om bij dit ik je we wat",
    "id": "yang dan di ini itu dengan untuk tidak dari dalam akan pada juga saya ke karena ada mereka sudah atau bisa",
    "tr": "ve bir bu için ile de da çok ama değil daha gibi en ne var ben sen o olarak kadar mı mi şey",
    "vi": "và là của có không trong một được cho người này với các đã để những khi tôi bạn như cũng",
    "pl": "i w nie na się z do to że jest jak ale o po co tak za od czy są był tego już być",
    "sv": "och i att det som en på är av för med till den har inte om ett han men var jag sig från vi",
}
_WORDS = {code: frozenset(text.split()) for code, text in _LATIN_WORDS.items()}
_SPLIT = re.compile(r"[^\W\d_]+", re.UNICODE)


def script(char: str) -> str:
    """The writing system of a letter or a vowel sign; ``""`` for digits, punctuation and symbols."""
    if unicodedata.category(char)[0] not in "LM":
        return ""
    point = ord(char)
    return next((name for low, high, name in _RANGES if low <= point <= high), "")


def scripts(text: str) -> Counter[str]:
    """Letters per writing system."""
    counts: Counter[str] = Counter()
    for char in str(text or ""):
        name = script(char)
        if name:
            counts[name] += 1
    return counts


def code_of(language: Any) -> str:
    """``"bn"`` for ``"bn"``, ``"Bengali"``, ``"bangla"``; ``""`` when unknown."""
    name = " ".join(str(language or "").split()).casefold()
    return name if name in LANGUAGES else _BY_NAME.get(name, "")


def name_of(code: str) -> str:
    return LANGUAGES.get(code, code)


def detect(text: str) -> dict[str, Any]:
    """``{language, script, confidence, mixed, shares}``; language ``"und"`` when there is nothing to go on."""
    counts = scripts(text)
    total = sum(counts.values())
    if not total:
        return {"language": "und", "script": "", "confidence": 0.0, "mixed": False, "shares": {}}
    shares = {name: round(count / total, 3) for name, count in counts.most_common()}
    top = counts.most_common(1)[0][0]
    if "Kana" in counts and top in ("Han", "Kana"):
        top = "Kana"  # Japanese mixes kana with Han characters; Chinese has no kana
    share = (counts["Kana"] + counts["Han"]) / total if top == "Kana" else counts[top] / total
    result = {"script": top, "mixed": share < 0.85, "shares": shares}
    if top != "Latin":
        language = _SCRIPT_LANGUAGE[top]
        if top == "Arabic":
            language = "ur" if re.search("[ٹڈڑںےھ]", text) else "fa" if re.search("[پچژگ]", text) else "ar"
        elif top == "Cyrillic" and re.search("[іїєґІЇЄҐ]", text):
            language = "uk"
        return {"language": language, "confidence": round(share, 3), **result}
    words = [word.casefold() for word in _SPLIT.findall(text)]
    hits = sorted(((sum(word in known for word in words), code) for code, known in _WORDS.items()), reverse=True)
    best, runner = hits[0], hits[1]
    if best[0] < 2 or best[0] == runner[0]:
        return {"language": "und", "confidence": 0.0, **result}
    confidence = min(0.95, share * (best[0] - runner[0]) / best[0] + 0.1 * min(best[0], 5) / 5)
    return {"language": best[1], "confidence": round(confidence, 3), **result}
