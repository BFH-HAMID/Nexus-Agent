"""Deterministic fact extraction: text -> (subject, predicate, object) triples.

The rules are intentionally conservative.  A sentence that does not match a
pattern produces nothing; nothing is guessed.  ``I``/``my`` resolve to the
entity ``user``.  The optional model path lives in :mod:`graph` and is validated
with the same :func:`clean_fact` gate.
"""
from __future__ import annotations

import re
from typing import Any

USER = "user"
_SENTENCE = re.compile(r"(?<=[.!?;।])\s+|\n+")
_NAME = r"[A-Z][\w.&+#-]*(?:\s+(?:[A-Z][\w.&+#-]*|of|de|van|bin|al))*"
_SUBJECT = rf"(?:I|We|{_NAME}|[Tt]he\s+[a-z][\w -]{{1,40}}?)"
_CUT = re.compile(r"\s+(?:and|but|because|which|who|that|when|while|so|since|although)\s+|[,(]", re.I)
_PRONOUNS = frozenset({"it", "that", "this", "him", "her", "them", "he", "she", "they", "there", "here",
                       "something", "someone", "nothing", "one", "you", "us"})
_ARTICLES = re.compile(r"^(?:the|a|an|my|our)\s+", re.I)
_FIRST_PERSON = frozenset({"i", "me", "my", "myself", "we", "us", "our"})

# verb phrase -> canonical predicate
VERBS: tuple[tuple[str, str], ...] = (
    (r"work(?:s|ed)? (?:at|for)", "works_at"),
    (r"live(?:s|d)? in", "lives_in"),
    (r"(?:was|were|am|is) born in", "born_in"),
    (r"(?:am|is|are) married to", "married_to"),
    (r"report(?:s|ed)? to", "reports_to"),
    (r"manage(?:s|d)?", "manages"),
    (r"(?:am|is|are) located in", "located_in"),
    (r"(?:am|is|are) based in", "located_in"),
    (r"(?:am|is|are) part of", "part_of"),
    (r"belong(?:s|ed)? to", "part_of"),
    (r"depend(?:s|ed)? on", "depends_on"),
    (r"(?:am|is|are) allergic to", "allergic_to"),
    (r"stud(?:y|ies|ied) at", "studies_at"),
    (r"stud(?:y|ies|ied)", "studies"),
    (r"teach(?:es)?", "teaches"),
    (r"own(?:s|ed)?", "owns"),
    (r"use(?:s|d)?", "uses"),
    (r"prefer(?:s|red)?", "prefers"),
    (r"like(?:s|d)?", "likes"),
    (r"love(?:s|d)?", "likes"),
    (r"(?:hate(?:s|d)?|dislike(?:s|d)?)", "dislikes"),
    (r"know(?:s)?", "knows"),
    (r"speak(?:s)?", "speaks"),
    (r"play(?:s|ed)?", "plays"),
    (r"found(?:ed|s)", "founded"),
    (r"lead(?:s)?", "leads"),
    (r"maintain(?:s|ed)?", "maintains"),
    (r"created?", "created"),
)
_VERB_RULES = tuple((re.compile(rf"^(?P<s>{_SUBJECT})\s+(?:also\s+|really\s+|currently\s+)?{verb}\s+(?P<o>.+)$"), pred)
                    for verb, pred in VERBS)
_MY_IS = re.compile(r"^(?:My|Our)\s+(?P<rel>[a-z][a-z' -]{1,40}?)(?:'s name)?\s+(?:is|are|was)\s+(?:called\s+|named\s+)?(?P<o>.+)$", re.I)
_IS_MY = re.compile(rf"^(?P<a>{_NAME})\s+(?:is|was)\s+(?:my|our)\s+(?P<rel>[a-z][a-z -]{{1,40}})$")
_IS_POSSESSIVE = re.compile(rf"^(?P<a>{_NAME})\s+(?:is|was)\s+(?P<b>{_NAME})'s\s+(?P<rel>[a-z][a-z -]{{1,40}})$")
_POSSESSIVE_IS = re.compile(rf"^(?P<b>{_NAME})'s\s+(?P<rel>[a-z][a-z -]{{1,40}}?)\s+(?:is|was)\s+(?:on\s+)?(?P<a>.+)$")
_IS_A = re.compile(rf"^(?P<s>{_SUBJECT})\s+(?:am|is|are|was)\s+(?:an?|the)\s+(?P<o>.+)$")
_I_AM_NAMED = re.compile(rf"^(?:I am|I'm)\s+(?:called\s+|named\s+)?(?P<o>{_NAME})$")
_BN_NAME = re.compile(r"^আমার\s+(?:(?P<rel>[\u0980-\u09ff]+?)(?:ের|র)\s+)?নাম\s+(?P<o>[^।.!?]+)$")


def entity_key(name: str) -> str:
    return re.sub(r"\s+", " ", str(name)).strip().casefold()


def clean_name(value: Any, *, object_side: bool = False) -> str:
    """Normalise an entity mention; empty string when it is not a usable entity."""
    text = re.sub(r"\s+", " ", str(value or "")).strip(" \t\"'`.!?;:।")
    if object_side:
        text = _CUT.split(text, maxsplit=1)[0].strip(" \t\"'`.!?;:")
    if text.casefold() in _FIRST_PERSON:
        return USER
    text = _ARTICLES.sub("", text).strip()
    if not text or text.casefold() in _PRONOUNS or len(text) > 80 or len(text.split()) > 8:
        return ""
    if re.search(r"[<>{}\[\]\\|]|https?://", text):
        return ""
    return text


def clean_predicate(value: Any) -> str:
    text = re.sub(r"[^a-z0-9\u0980-\u09ff]+", "_", str(value or "").casefold()).strip("_")
    return text[:48]


def clean_fact(subject: Any, predicate: Any, obj: Any, confidence: Any = 0.7) -> dict[str, Any] | None:
    """The single validation gate for rule- and model-produced facts."""
    s, p, o = clean_name(subject), clean_predicate(predicate), clean_name(obj, object_side=True)
    if not s or not p or not o or entity_key(s) == entity_key(o):
        return None
    try:
        score = float(confidence)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= score <= 1.0:
        return None
    return {"subject": s, "predicate": p, "object": o, "confidence": round(score, 3)}


def _sentence_facts(sentence: str) -> list[dict[str, Any]]:
    text = re.sub(r"\s+", " ", sentence).strip().rstrip(".!?;।").strip()
    if not 3 <= len(text) <= 300:
        return []
    found: list[dict[str, Any] | None] = []
    match = _BN_NAME.match(text)
    if match:
        found.append(clean_fact(USER, match.group("rel") or "name", match.group("o"), 0.9))
        return [f for f in found if f]
    match = _I_AM_NAMED.match(text)
    if match:
        found.append(clean_fact(USER, "name", match.group("o"), 0.85))
        return [f for f in found if f]
    match = _MY_IS.match(text)
    if match:
        found.append(clean_fact(USER, match.group("rel"), match.group("o"), 0.9))
        return [f for f in found if f]
    match = _IS_MY.match(text)
    if match:
        found.append(clean_fact(USER, match.group("rel"), match.group("a"), 0.9))
        return [f for f in found if f]
    match = _IS_POSSESSIVE.match(text)
    if match:
        found.append(clean_fact(match.group("b"), match.group("rel"), match.group("a"), 0.85))
        return [f for f in found if f]
    match = _POSSESSIVE_IS.match(text)
    if match:
        found.append(clean_fact(match.group("b"), match.group("rel"), match.group("a"), 0.85))
        return [f for f in found if f]
    for rule, predicate in _VERB_RULES:
        match = rule.match(text)
        if match:
            found.append(clean_fact(match.group("s"), predicate, match.group("o"), 0.75))
            return [f for f in found if f]
    match = _IS_A.match(text)
    if match:
        found.append(clean_fact(match.group("s"), "is_a", match.group("o"), 0.7))
    return [f for f in found if f]


def extract_facts(text: str, limit: int = 12) -> list[dict[str, Any]]:
    """Rule-based triples from free text, de-duplicated, at most ``limit``."""
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for sentence in _SENTENCE.split(str(text or "")[:20_000]):
        for fact in _sentence_facts(sentence):
            key = (entity_key(fact["subject"]), fact["predicate"], entity_key(fact["object"]))
            if key in seen:
                continue
            seen.add(key)
            facts.append(fact)
            if len(facts) >= limit:
                return facts
    return facts
