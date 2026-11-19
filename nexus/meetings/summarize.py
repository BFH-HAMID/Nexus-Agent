"""Offline meeting summary: key sentences, action items, decisions, questions.

Deterministic. Every line it returns is a quote from the transcript, so it can
be wrong about importance but it cannot invent content.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

_SENTENCE = re.compile(r"(?<=[.!?।])\s+")
_STRIP = " \t\"'`.,;:!?()[]{}<>«»“”‘’।—–-…*_#/\\|"
_STOP = frozenset("""
a an the and or but if so then than that this these those there here it its is are was were be been being am do does
did done have has had having will would shall should can could may might must not no yes of in on at to for from by
with about into over under after before between through during up down out off again very just also too only own same
such both each few more most other some any all as i me my we us our you your he him his she her they them their what
which who whom when where why how okay ok yeah right well like know think going get got one
এবং ও আর কিন্তু যে এই সেই তা না হয় করে জন্য থেকে একটি আমি আমরা তুমি আপনি সে তারা কি হবে আছে ছিল এটা ওটা তো
""".split())

_DUE = re.compile(
    r"\b(?:by|before|until|on|due)\s+((?:next\s+|this\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"week|month)|tomorrow|today|tonight|eod|eow|end of (?:the )?(?:day|week|month|quarter|year)|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?)\b", re.I)
_BARE_DUE = re.compile(r"\b(today|tomorrow|tonight|next week|next month|this week|this afternoon|this evening)\b", re.I)
_COMMIT = re.compile(
    r"(?<![\w'’-])(?P<who>I|[Ww]e|[Yy]ou|[Hh]e|[Ss]he|[Tt]hey|[A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+)?)\s*"
    r"(?:will|['’]ll|shall|must|need(?:s)? to|ha(?:s|ve) to|(?:am|is|are) going to|can take|should)\b(?!\s+(?:be|have)\s)")
_CUE = re.compile(r"\b(?:action items?|todo|to-do|follow[- ]up|next steps?|assigned to|takes? care of|owner)\b", re.I)
_REQUEST = re.compile(r"^(?:please|can you|could you|would you|let['’]?s|make sure)\b", re.I)
_BN_ACTION = re.compile(r"করতে হবে|দিতে হবে|পাঠাতে হবে|জানাতে হবে|করব|করবে|করবেন|পাঠাব|পাঠাবে|জানাব|দায়িত্ব")
_DECISION = re.compile(r"\b(?:decided|agreed|decision|approved|settled on|go(?:ing)? with|conclusion|finali[sz]ed|"
                       r"signed off)\b|সিদ্ধান্ত", re.I)
_NOT_OWNER = frozenset({"this", "that", "it", "there", "these", "those", "what", "which", "the", "then", "so", "and",
                        "but", "if", "when", "maybe", "hopefully", "everything", "nothing", "something", "someone"})


def words(text: str) -> list[str]:
    """Lower-cased words of any script (split on spaces; ``\\w`` would cut Bengali words at vowel signs)."""
    return [w for w in (raw.strip(_STRIP).casefold() for raw in str(text).split()) if w]


def utterances(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join consecutive pieces of one speaker until a sentence ends (caption files cut mid-sentence)."""
    joined: list[dict[str, Any]] = []
    for seg in segments:
        text = " ".join(str(seg.get("text") or "").split())
        if not text:
            continue
        last = joined[-1] if joined else None
        if (last is not None and last["speaker"] == (seg.get("speaker") or "") and len(last["text"]) < 600
                and not last["text"].rstrip("\"')”").endswith((".", "!", "?", "।"))):
            last["text"] += " " + text
        else:
            joined.append({"t": seg.get("t"), "speaker": seg.get("speaker") or "", "text": text})
    return joined


def sentences(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in utterances(segments):
        for piece in _SENTENCE.split(row["text"]):
            piece = piece.strip()
            if piece:
                out.append({"t": row["t"], "speaker": row["speaker"], "text": piece[:500]})
    return out


def _due(text: str) -> str:
    match = _DUE.search(text) or _BARE_DUE.search(text)
    return match.group(1).lower() if match else ""


def action_item(sentence: dict[str, Any]) -> dict[str, Any] | None:
    """A sentence that sounds like a commitment or a request, with owner and due date when stated."""
    text = sentence["text"]
    commit = next((m for m in _COMMIT.finditer(text) if m.group("who").casefold() not in _NOT_OWNER), None)
    request = _REQUEST.match(text)
    if text.endswith("?") and not request:
        return None
    if not (commit or request or _CUE.search(text) or _BN_ACTION.search(text)):
        return None
    owner = ""
    if commit:
        who = commit.group("who")
        owner = sentence["speaker"] if who == "I" else "" if who.casefold() in {"we", "you", "he", "she", "they"} else who
    return {"text": text, "owner": owner[:60], "due": _due(text), "t": sentence["t"], "speaker": sentence["speaker"]}


def ranked(rows: list[dict[str, Any]]) -> list[int]:
    """Indexes of the sentences worth quoting, best first, without repeats.

    Words that many sentences share carry the topic. The weight grows with the
    logarithm of that count and is divided by the sentence length, so neither a
    very common word nor a very long sentence wins alone. A sentence that says
    the same as a better one (same content words) is left out.
    """
    frequency: Counter[str] = Counter()
    bags = []
    for row in rows:
        bag = frozenset(w for w in words(row["text"]) if len(w) > 2 and w not in _STOP and not w.isdigit())
        bags.append(bag)
        frequency.update(bag)
    scored = []
    for index, (row, bag) in enumerate(zip(rows, bags, strict=True)):
        count = len(words(row["text"]))
        if not 4 <= count <= 60 or row["text"].endswith("?"):
            continue
        score = sum(math.log1p(frequency[w]) for w in bag if frequency[w] > 1) / math.sqrt(max(len(bag), 1))
        if _DECISION.search(row["text"]):
            score *= 1.5
        if score > 0:
            scored.append((score, index))
    order, seen, best = [], set(), []
    for _, index in sorted(scored, key=lambda item: (-item[0], item[1])):
        bag = bags[index]
        if bag in seen or any(len(bag & other) >= 0.7 * len(bag | other) for other in best):
            continue
        seen.add(bag)
        if len(best) < 30:
            best.append(bag)
        order.append(index)
    return order


def line(row: dict[str, Any]) -> str:
    stamp = clock(row.get("t"))
    return (f"[{stamp}] " if stamp else "") + (f"{row['speaker']}: " if row.get("speaker") else "") + row["text"]


def excerpt(segments: list[dict[str, Any]], budget: int) -> tuple[str, float]:
    """The transcript as text, or its best sentences in order when it is longer than ``budget``.

    Returns ``(text, share)`` where ``share`` is the part of the transcript that is included (0-1).
    """
    rows = sentences(segments)
    lines = [line(row) for row in rows]
    total = sum(len(text) + 1 for text in lines)
    if total <= budget:
        return "\n".join(lines), 1.0
    keep, used = set(), 0
    for index in ranked(rows):
        if used + len(lines[index]) + 1 > budget:
            continue
        keep.add(index)
        used += len(lines[index]) + 1
    return "\n".join(lines[i] for i in sorted(keep)), round(used / max(total, 1), 3)


def summarize(segments: list[dict[str, Any]], max_points: int = 6) -> dict[str, Any]:
    rows = sentences(segments)
    keep = sorted(ranked(rows)[:max_points])
    actions = [item for item in (action_item(row) for row in rows) if item]
    spoken: Counter[str] = Counter()
    for seg in segments:
        spoken[seg.get("speaker") or ""] += len(words(seg.get("text") or ""))
    ends = [seg.get("end") or seg.get("t") for seg in segments if isinstance(seg.get("end") or seg.get("t"), (int, float))]
    return {
        "mode": "extractive",
        "points": [rows[i] for i in keep],
        "action_items": actions[:40],
        "decisions": [row for row in rows if _DECISION.search(row["text"]) and not row["text"].endswith("?")][:20],
        "questions": [row for row in rows if row["text"].endswith("?")][:10],
        "speakers": [{"name": name, "words": count} for name, count in spoken.most_common(20) if name],
        "words": sum(spoken.values()),
        "duration_seconds": round(max(ends), 1) if ends else None,
    }


def clock(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return ""
    total = int(seconds)
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}" if total >= 3600 else f"{total // 60}:{total % 60:02d}"
