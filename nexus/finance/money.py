"""Amounts, dates and description text as they appear in bank statements.

Amounts are ``decimal.Decimal`` everywhere: no floating point touches money.
Every pattern here runs on one bounded cell of a statement, never on a whole file.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

MAX_CELL = 60
_MONTHS = {name: number for number, names in enumerate((
    ("jan", "january", "জানুয়ারি", "জানুয়ারী"), ("feb", "february", "ফেব্রুয়ারি", "ফেব্রুয়ারী"), ("mar", "march", "মার্চ"),
    ("apr", "april", "এপ্রিল"), ("may", "মে"), ("jun", "june", "জুন"), ("jul", "july", "জুলাই"),
    ("aug", "august", "আগস্ট"), ("sep", "sept", "september", "সেপ্টেম্বর"), ("oct", "october", "অক্টোবর"),
    ("nov", "november", "নভেম্বর"), ("dec", "december", "ডিসেম্বর")), start=1) for name in names}
_TIME = re.compile(r"(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d{1,6})?)?(?: ?[AaPp][Mm])?(?: ?(?:Z|[+-]\d{2}:?\d{2}))?)?")
_ISO = re.compile(r"(\d{4})([-/.])(\d{1,2})\2(\d{1,2})")
_COMPACT = re.compile(r"(\d{4})(\d{2})(\d{2})")
_NUMERIC = re.compile(r"(\d{1,2})([-/.])(\d{1,2})\2(\d{4}|\d{2})")
_DAY_NAME = re.compile(r"(\d{1,2})[-/. ]([^\d\s,./-]{2,14})\.?,?[-/. ](\d{4}|\d{2})")
_NAME_DAY = re.compile(r"([^\d\s,./-]{2,14})\.?,? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})")
_SPACES = dict.fromkeys(map(ord, " \u00a0\u2009\u202f\u2007'\u2019"), None)
_SIGNS = "৳$€£₹¥"
_DEBIT_WORDS = frozenset({"dr", "db", "debit"})
_CREDIT_WORDS = frozenset({"cr", "credit"})
_DOTTED = frozenset({"tk", "rs"})     # abbreviations written with a full stop: "Tk. 500"
_DOT = re.compile(r"(?:\d{1,3}(?:,\d{2,3}){0,6},\d{3}|\d{0,15})(?:\.\d{1,4})?")
_COMMA = re.compile(r"(?:\d{1,3}(?:\.\d{3}){1,6}|\d{0,15})(?:,\d{1,4})?")
_LONG_NUMBER = re.compile(r"\d(?:[ -]?\d){8,40}")
_LIMIT = Decimal(10) ** 13


class Unreadable(ValueError):
    """A cell or a file could not be read as what it should be."""


class Amount(NamedTuple):
    value: Decimal          # signed as written: a minus sign or brackets make it negative
    marker: str             # "dr", "cr" or "" - what a DR/CR note next to the number says


class Day(NamedTuple):
    """A date cell. ``first`` and ``second`` are set while day and month are still to be told apart."""

    date: dt.date | None
    first: int = 0
    second: int = 0
    year: int = 0


def ascii_digits(text: str) -> str:
    """Decimal digits of any script (Bengali, Arabic-Indic, full-width ...) as 0-9."""
    if text.isascii():
        return text
    return "".join(str(unicodedata.decimal(ch)) if not ch.isascii() and unicodedata.decimal(ch, None) is not None
                   else ch for ch in text)


def parse_amount(cell: str, decimal: str = ".") -> Amount | None:
    """One amount cell, or ``None`` for an empty one. ``decimal`` is the decimal mark of the statement."""
    text = ascii_digits(str(cell or "")).strip()
    if not text or text in {"-", "--", "\u2013", "\u2014"}:
        return None
    if len(text) > MAX_CELL:
        raise Unreadable("too long for an amount")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    marker = ""
    text = text.replace("টাকা", "")

    def word(match: re.Match[str]) -> str:
        nonlocal marker
        low = match[1].casefold()
        if low in _DEBIT_WORDS or low in _CREDIT_WORDS:
            if marker:
                raise Unreadable("more than one DR/CR note")
            marker = "dr" if low in _DEBIT_WORDS else "cr"
        elif not 2 <= len(low) <= 5:
            raise Unreadable("not an amount")  # a currency code is short; anything longer is not a number
        # "Tk. 500", "Rs.500" and "CR." end in a full stop; in "USD.50" the full stop is the decimal mark
        return "" if not match[3] or low in _DOTTED else match[2]

    # a run of letters, the full stop after it, and the digit after that full stop (if there is one)
    text = re.sub(r"([^\W\d_]+)(\.?)(?=(\d?))", word, text).translate(_SPACES).strip(_SIGNS).replace("\u2212", "-")
    for sign in ("-", "+"):
        if text.startswith(sign) or text.endswith(sign):
            negative = negative or sign == "-"
            text = text.strip(sign) if text.count(sign) == 1 else "?"
            break
    text = text.strip(_SIGNS)
    mine, other, mark = (_DOT, _COMMA, ",") if decimal == "." else (_COMMA, _DOT, ".")
    if not text or not any(ch.isdigit() for ch in text) or not mine.fullmatch(text):
        if text and other.fullmatch(text):
            raise Unreadable(f"looks like an amount with a decimal '{mark}' (set decimal: \"{mark}\" for this account)")
        raise Unreadable("not an amount")
    plain = text.replace(",", "") if decimal == "." else text.replace(".", "").replace(",", ".")
    try:
        value = Decimal(plain)
    except InvalidOperation:
        raise Unreadable("not an amount") from None
    if value >= _LIMIT:
        raise Unreadable("too large for an amount")
    return Amount(-value if negative else value, marker)


def _year(text: str) -> int:
    return int(text) if len(text) == 4 else 2000 + int(text)


def _date(year: int, month: int, day: int) -> Day:
    try:
        return Day(dt.date(year, month, day))
    except ValueError:
        raise Unreadable("not a date in the calendar") from None


def parse_day(cell: str) -> Day | None:
    """One date cell (a time of day after it is ignored), or ``None`` for an empty one."""
    text = " ".join(ascii_digits(str(cell or "")).split())
    if not text:
        return None
    if len(text) > MAX_CELL:
        raise Unreadable("too long for a date")
    for pattern in (_ISO, _COMPACT, _NUMERIC, _DAY_NAME, _NAME_DAY):
        match = pattern.match(text)
        if not match or not _TIME.fullmatch(text, match.end()):
            continue
        if pattern is _ISO:
            return _date(int(match[1]), int(match[3]), int(match[4]))
        if pattern is _COMPACT:
            return _date(int(match[1]), int(match[2]), int(match[3]))
        if pattern is _NUMERIC:
            first, second = int(match[1]), int(match[3])
            if first > 12 and second > 12 or not first or not second:
                raise Unreadable("not a date in the calendar")
            return Day(None, first, second, _year(match[4]))
        name, day, year = (match[2], match[1], match[3]) if pattern is _DAY_NAME else (match[1], match[2], match[3])
        month = _MONTHS.get(name.casefold())
        if month is None:
            continue
        return _date(_year(year), month, int(day))
    raise Unreadable("not a date I can read")


def settle(day: Day, day_first: bool) -> dt.date:
    """The calendar date of a cell, reading ``03/04`` as day/month or month/day."""
    if day.date is not None:
        return day.date
    d, m = (day.first, day.second) if day_first else (day.second, day.first)
    found = _date(day.year, m, d).date
    assert found is not None
    return found


def plain(value: Decimal) -> str:
    """An amount for JSON and for storage: no grouping, at least two decimals, never an exponent."""
    return f"{value:.2f}" if value.as_tuple().exponent >= -2 else format(value, "f")


def fmt(value: Decimal) -> str:
    """An amount for people: 1,234.50"""
    places = min(4, max(2, -int(value.as_tuple().exponent)))
    return f"{value:,.{places}f}"


def _kept(ch: str) -> bool:
    return ch.isalnum() or unicodedata.category(ch) in ("Mn", "Mc")


_KEEP: dict[str, bool] = {}


def words(text: str) -> list[str]:
    """Lower-case words of a description or keyword; punctuation separates, accents and vowel signs stay."""
    out = []
    for ch in unicodedata.normalize("NFKC", str(text or "")).casefold():
        keep = _KEEP.get(ch)
        if keep is None:
            keep = _KEEP[ch] = _kept(ch)
        out.append(ch if keep else " ")
    return "".join(out).split()


def merchant_key(description: str) -> str:
    """What stays the same between two payments to one merchant: the words, without numbers and month names.

    "HOUSE RENT OCT 2026" and "HOUSE RENT NOV 2026" are one merchant; so are two
    card payments that differ only in their reference number.
    """
    tokens = words(description)
    named = [token for token in tokens if not any(ch.isdigit() for ch in token)]
    steady = [token for token in named if token not in _MONTHS]
    return " ".join(steady or named or tokens)[:60].strip()


def tidy(text: str, limit: int = 200) -> str:
    """One printable line: control and formatting characters (escape codes, direction overrides) are dropped."""
    kept = "".join(" " if unicodedata.category(ch)[0] in "CZ" else ch for ch in str(text or "")[:limit * 4])
    return " ".join(kept.split())[:limit].strip()


def mask(text: str) -> str:
    """Account, card and phone numbers (nine digits or more) keep their last four digits only."""
    def last_four(match: re.Match[str]) -> str:
        digits = [ch for ch in match[0] if ch.isdigit()]
        return "…" + "".join(digits[-4:])

    return _LONG_NUMBER.sub(last_four, text)
