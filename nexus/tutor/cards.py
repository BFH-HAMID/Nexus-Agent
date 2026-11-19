"""Card text: cleaning what comes in, comparing answers, reading card files. Deterministic, offline.

Every function here works on one bounded piece of text.  Files are read line
by line; no pattern with an open-ended repetition runs over a whole file.
"""
from __future__ import annotations

import csv
import hashlib
import io
import unicodedata

from .. import policyfile

MAX_LINES = 12                 # per side of a card
MAX_FILE_LINES = 40_000
SUFFIXES = (".csv", ".tsv", ".txt", ".md")
_FRONT_TITLES = frozenset({"front", "question", "term", "q", "prompt", "word"})
_BACK_TITLES = frozenset({"back", "answer", "definition", "a", "meaning", "translation"})
_QUESTION = ("q:", "question:")
_ANSWER = ("a:", "answer:")
_JOINERS = "\u200c\u200d"
_EDGE = ".,;:!?\u00bf\u00a1\u2026\"'\u201a\u201c\u201d\u201e\u00ab\u00bb\u2039\u203a()[]{}\u3002\u3001\u0964\u0965\u06d4\u061f\u060c"
# written differently, meant the same: dashes and the minus sign, apostrophes
_SAME = {**dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212"), "-"),
         **dict.fromkeys(map(ord, "\u2019\u02bc\u2018`\u00b4"), "'")}


class Unusable(ValueError):
    """A card text or a card file cannot be used; the message says why in plain words."""


def clean(text: object, limit: int, label: str = "text") -> str:
    """One side of a card: printable text of at most ``limit`` characters and ``MAX_LINES`` lines.

    Control and formatting characters (escape codes, direction overrides) are
    dropped, so a card cannot draw on the terminal that shows it.
    """
    if not isinstance(text, str):
        raise Unusable(f"The {label} must be text")
    if len(text) > limit * 4 + 200:
        raise Unusable(f"The {label} is longer than {limit} characters")
    lines = []
    for raw in unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lines.append(" ".join("".join(_printable(ch) for ch in raw).split()))
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    value = "\n".join(lines)
    if not value:
        raise Unusable(f"The {label} is empty")
    if len(value) > limit:
        raise Unusable(f"The {label} is longer than {limit} characters")
    if len(lines) > MAX_LINES:
        raise Unusable(f"The {label} has more than {MAX_LINES} lines")
    if policyfile.looks_secret(value):
        raise Unusable(f"The {label} looks like a credential; a card is not the place for it")
    return value


def _printable(ch: str) -> str:
    """A character as it may be stored: controls and separators become a space, invisible formatting is dropped.

    The two joiners (U+200C, U+200D) stay: Bengali, Persian and other scripts need them to shape a word.
    """
    if ch in _JOINERS:
        return ch
    category = unicodedata.category(ch)
    if category == "Cc" or category[0] == "Z":
        return " "
    return "" if category[0] == "C" else ch


def normalize(text: object) -> str:
    """What two answers must share to count as the same.

    Ignored: upper and lower case, extra spaces, and punctuation at the edges
    of a word (``Paris.`` = ``paris``).  Everything inside a word is compared,
    so ``-5`` is not ``5`` and ``x+1`` is not ``x-1``.
    """
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold().translate(_SAME)
    tokens = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf").split()
    words = [bare for token in tokens if (bare := token.strip(_EDGE))]
    return " ".join(words or tokens)     # an answer that is nothing but a sign ("?") is compared as it is


def clean_answer(text: object, limit: int) -> str:
    """What the learner typed or said, as one line of printable text. Never raises."""
    line = "".join(_printable(ch) for ch in unicodedata.normalize("NFC", str(text or "")[:limit * 2]))
    return " ".join(line.split())[:limit]


def _without_brackets(text: str) -> str:
    """``to run (irregular)`` -> ``to run``. One pass, no pattern."""
    out, depth = [], 0
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(depth - 1, 0)
        elif not depth:
            out.append(ch)
    return "".join(out)


def accepted(back: str) -> list[str]:
    """Every answer the back of a card accepts: alternatives are separated by ``|``; a note in brackets is optional."""
    found: list[str] = []
    for part in back.split("|"):
        for variant in (part, _without_brackets(part)):
            value = normalize(variant)
            if value and value not in found:
                found.append(value)
    return found


def matches(answer: object, back: str) -> bool:
    given = normalize(answer)
    return bool(given) and given in accepted(back)


def one_line(text: object, limit: int = 120) -> str:
    """A card side on one line, for lists and previews."""
    line = " / ".join(part for part in str(text).split("\n") if part)
    return line if len(line) <= limit else line[:limit - 1] + "\u2026"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode(data: bytes) -> str:
    if b"\x00" in data[:8192]:
        raise Unusable("not a text file")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Unusable("not UTF-8 text") from None


def _starts(line: str, labels: tuple[str, ...]) -> str | None:
    """The text after ``Q:`` / ``A:`` when the line starts with such a label, else ``None``."""
    head = line.lstrip()[:12].casefold()
    for label in labels:
        if head.startswith(label):
            return line.lstrip()[len(label):].strip()
    return None


def _blocks(lines: list[str]) -> tuple[list[tuple[int, str, str]], list[str]]:
    """``Q:`` / ``A:`` blocks. An answer runs until an empty line or the next ``Q:``."""
    cards: list[tuple[int, str, str]] = []
    problems: list[str] = []
    line_no, front, back, side = 0, [], [], ""

    def close() -> None:
        if front and back:
            cards.append((line_no, "\n".join(front), "\n".join(back)))
        elif front:
            problems.append(f"line {line_no}: a question without an answer")

    for number, line in enumerate(lines, start=1):
        question, answer = _starts(line, _QUESTION), _starts(line, _ANSWER)
        if question is not None:
            close()
            line_no, front, back, side = number, [question] if question else [], [], "front"
        elif answer is not None and side:
            back, side = ([answer] if answer else []), "back"
        elif not line.strip():
            if side == "back":
                close()
                front, back, side = [], [], ""
        elif side == "front":
            front.append(line.strip())
        elif side == "back":
            back.append(line.strip())
    close()
    return cards, problems


def _rows(text: str, delimiter: str) -> tuple[list[tuple[int, str, str]], list[str]]:
    """One card per row: front, back. Further columns (tags) are not used."""
    cards: list[tuple[int, str, str]] = []
    problems: list[str] = []
    wide = 0
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=False)
    try:
        for row in reader:
            number = reader.line_num
            cells = [cell.strip() for cell in row]
            if not any(cells) or (cells[0].startswith("#") and not any(cells[1:])):
                continue    # an empty row, or a comment: a line that starts with # and has no answer
            if len(cells) < 2 or not cells[0] or not cells[1]:
                problems.append(f"line {number}: a row needs a question and an answer")
                continue
            if not cards and not problems and normalize(cells[0]) in _FRONT_TITLES and normalize(cells[1]) in _BACK_TITLES:
                continue    # a row of column titles
            wide += any(cells[2:])
            cards.append((number, cells[0], cells[1]))
    except csv.Error as exc:
        raise Unusable(f"not a well-formed table ({str(exc)[:60]})") from None
    if wide:
        problems.append(f"{wide} row(s) have more than two columns: only the first two were used")
    return cards, problems


def parse(text: str, suffix: str) -> tuple[list[tuple[int, str, str]], list[str]]:
    """``([(line, front, back)], [problem])`` from the text of a card file.

    ``.csv`` / ``.tsv``: one card per row.  ``.txt`` / ``.md``: ``Q:`` / ``A:``
    blocks, or one card per line as ``front<TAB>back`` or ``front :: back``.
    """
    lines = text.split("\n")
    if len(lines) > MAX_FILE_LINES:
        raise Unusable(f"more than {MAX_FILE_LINES} lines")
    suffix = suffix.casefold()
    if suffix == ".tsv":
        return _rows(text, "\t")
    if suffix == ".csv":
        first = next((line for line in lines if line.strip() and not line.startswith("#")), "")
        return _rows(text, max(",;\t", key=first.count) if first else ",")   # (a tie goes to the comma)
    if suffix not in SUFFIXES:
        raise Unusable(f"a card file is {', '.join(SUFFIXES)}")
    if any(_starts(line, _QUESTION) is not None for line in lines):
        return _blocks(lines)
    cards: list[tuple[int, str, str]] = []
    problems: list[str] = []
    for number, line in enumerate(lines, start=1):
        front, mark, back = line.partition("\t")
        if not mark:
            front, mark, back = line.partition(" :: ")
        if not line.strip() or (line.startswith("#") and not (mark and back.strip())):
            continue    # an empty line, or a comment: a line that starts with # and has no answer
        if not mark or not front.strip() or not back.strip():
            problems.append(f"line {number}: no answer (write the card as  question<TAB>answer  or  question :: answer)")
            continue
        cards.append((number, front.strip(), back.split("\t")[0].strip()))
    return cards, problems
