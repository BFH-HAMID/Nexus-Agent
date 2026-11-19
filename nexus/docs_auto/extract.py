"""Read a document's text and pull out the obvious fields. Deterministic, offline.

Everything returned here comes from a file somebody else wrote, so it is data:
it is shown to the user and stored in the index, and it never decides where a
file may go beyond a sanitised file-name part.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_MONTHS = {name: index for index, names in enumerate((
    ("jan", "january", "জানুয়ারি"), ("feb", "february", "ফেব্রুয়ারি"), ("mar", "march", "মার্চ"), ("apr", "april", "এপ্রিল"),
    ("may", "মে"), ("jun", "june", "জুন"), ("jul", "july", "জুলাই"), ("aug", "august", "আগস্ট"),
    ("sep", "sept", "september", "সেপ্টেম্বর"), ("oct", "october", "অক্টোবর"), ("nov", "november", "নভেম্বর"),
    ("dec", "december", "ডিসেম্বর")), start=1) for name in names}
_MONTH = "|".join(sorted((re.escape(name) for name in _MONTHS), key=len, reverse=True))
_DATE = re.compile(
    r"(?<![\w/.-])(?:"
    r"(?P<y1>(?:19|20)\d{2})[-/.](?P<m1>\d{1,2})[-/.](?P<d1>\d{1,2})"
    r"|(?P<a>\d{1,2})[-/.](?P<b>\d{1,2})[-/.](?P<y2>(?:19|20)\d{2})"
    rf"|(?P<d3>\d{{1,2}})(?:st|nd|rd|th)?[\s-]{{1,3}}(?P<m3>{_MONTH})\.?,?[\s-]{{1,3}}(?P<y3>(?:19|20)\d{{2}})"
    rf"|(?P<m4>{_MONTH})\.?\s{{1,3}}(?P<d4>\d{{1,2}})(?:st|nd|rd|th)?,?\s{{1,3}}(?P<y4>(?:19|20)\d{{2}})"
    r")(?![\w/-])", re.IGNORECASE)
_CURRENCY = {"৳": "BDT", "bdt": "BDT", "tk": "BDT", "tk.": "BDT", "taka": "BDT", "টাকা": "BDT", "$": "USD", "usd": "USD",
             "us$": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP", "₹": "INR", "inr": "INR", "rs": "INR",
             "rs.": "INR"}
_SIGN = r"(?:৳|\$|€|£|₹|US\$|BDT|USD|EUR|GBP|INR|Tk\.?|Rs\.?)"
# Every repetition below is bounded: a document is hostile input, and an open-ended pattern can take minutes on it.
_NUMBER = r"\d{1,3}(?:,\d{2,3}){1,6}(?:\.\d{1,2})?|\d{1,15}(?:\.\d{1,2})?"
_CODE = "BDT|USD|EUR|GBP|INR|taka|টাকা|৳|€|£"
# an amount needs a currency sign or code; "1.200,50" (decimal comma) is not read rather than read wrongly
_AMOUNT = re.compile(rf"(?<![\w.,])(?:(?P<pre>{_SIGN})[ \t]?(?P<v1>{_NUMBER})|(?P<v2>{_NUMBER})[ \t]?(?P<post>{_CODE}))(?![\w])",
                     re.IGNORECASE)
# after a "total" label the currency may be missing
_VALUE = rf"(?:(?P<pre>{_SIGN})[ \t]?(?P<v1>{_NUMBER})|(?P<v2>{_NUMBER})(?:[ \t]?(?P<post>{_CODE}))?)(?![\w.,]|[.,]\d)"
_TOTALS = tuple(
    re.compile(rf"(?<!\w){re.escape(label)}[ \t]{{0,4}}(?:\([^)\n]{{0,24}}\)[ \t]{{0,4}})?(?:[:=-][ \t]{{0,4}})?{_VALUE}",
               re.IGNORECASE)
    for label in ("grand total", "total due", "amount due", "balance due", "net payable", "total amount", "amount paid",
                  "total", "মোট"))
_REFERENCE = re.compile(
    r"(?<!\w)(?:invoice|receipt|order|reference|ref|contract|bill|document|চালান|রসিদ)[ \t]{0,4}"
    r"(?:(?:no|number|num|id|নং)(?![A-Za-z0-9])\.?[ \t]{0,4}[:#-]?|[:#])[ \t]{0,4}(?P<value>[A-Z0-9][A-Z0-9/_-]{1,29})(?![\w])",
    re.IGNORECASE)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,6}(?![\w-])")
_MAX_DATES = 400


class Unreadable(ValueError):
    """The file's text cannot be read; the message says why in plain words."""


def read_text(path: Path, limits: dict[str, Any]) -> str:
    """The text of a txt / md / pdf / docx file, cut at ``max_text_chars``."""
    suffix = path.suffix.casefold()
    budget = limits["max_text_chars"]
    if suffix in (".txt", ".md"):
        data = path.read_bytes()
        if b"\x00" in data[:8192]:
            raise Unreadable("not a text file")
        try:
            return data.decode("utf-8-sig")[:budget]
        except UnicodeDecodeError:
            raise Unreadable("not UTF-8 text") from None
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise Unreadable('reading PDF needs: pip install "nexus-agent[docs-auto]"') from None
        log = logging.getLogger("pypdf")
        level = log.level
        log.setLevel(logging.ERROR)  # a damaged file is reported once, as the reason below
        try:
            reader = PdfReader(str(path), strict=False)
            if reader.is_encrypted:
                raise Unreadable("the PDF is password-protected")
            pieces, size = [], 0
            for page in reader.pages[:limits["max_pdf_pages"]]:
                piece = page.extract_text() or ""
                pieces.append(piece)
                size += len(piece)
                if size >= budget:
                    break
        except Unreadable:
            raise
        except Exception as exc:  # noqa: BLE001 - pypdf raises many types on damaged files; none may stop a scan
            raise Unreadable(f"the PDF could not be read ({type(exc).__name__})") from None
        finally:
            log.setLevel(level)
        text = "\n".join(pieces)[:budget]
        if not text.strip():
            raise Unreadable("the PDF has no text layer (a scan?); text recognition is not part of this feature")
        return text
    if suffix == ".docx":
        from .. import office

        try:
            return str(office.operate(path, {"action": "read"})[0])[:budget]
        except ImportError:
            raise Unreadable('reading DOCX needs: pip install "nexus-agent[docs-auto]"') from None
        except Exception as exc:  # noqa: BLE001 - damaged archive, macro, external link, over 500 KB ...
            raise Unreadable(f"the DOCX was refused ({str(exc)[:80] or type(exc).__name__})") from None
    raise Unreadable(f"{suffix or 'this'} files are not read")


def _iso(year: Any, month: Any, day: Any) -> str:
    try:
        return dt.date(int(year), int(month), int(day)).isoformat()
    except (ValueError, TypeError):
        return ""


def dates(text: str, day_first: bool = True) -> list[tuple[int, str]]:
    """``[(position, "YYYY-MM-DD")]`` for every real calendar date in the text."""
    found = []
    for match in _DATE.finditer(text.translate(_BN_DIGITS)):
        g = match.groupdict()
        if g["y1"]:
            iso = _iso(g["y1"], g["m1"], g["d1"])
        elif g["y2"]:
            a, b = int(g["a"]), int(g["b"])
            day, month = (a, b) if (day_first and b <= 12) or a > 12 else (b, a)
            iso = _iso(g["y2"], month, day)
        elif g["y3"]:
            iso = _iso(g["y3"], _MONTHS[g["m3"].casefold()], g["d3"])
        else:
            iso = _iso(g["y4"], _MONTHS[g["m4"].casefold()], g["d4"])
        if iso:
            found.append((match.start(), iso))
    return found


def amounts(text: str) -> list[tuple[int, str, str]]:
    """``[(position, "1200.00", "BDT")]`` for amounts written with a currency sign or code."""
    found = []
    for match in _AMOUNT.finditer(text.translate(_BN_DIGITS)):
        raw = (match.group("v1") or match.group("v2")).replace(",", "")
        sign = (match.group("pre") or match.group("post")).casefold()
        try:
            value = Decimal(raw)
        except InvalidOperation:
            continue
        found.append((match.start(), f"{value:.2f}", _CURRENCY.get(sign, sign.upper())))
    return found


def fields(text: str, *, day_first: bool = True) -> dict[str, Any]:
    """Date, due date, total, currency, document number, e-mail addresses and a title line."""
    plain = text.translate(_BN_DIGITS)
    out: dict[str, Any] = {}
    every = dates(plain, day_first)[:_MAX_DATES]
    if every:
        lowered = plain.casefold()

        def near(labels: tuple[str, ...]) -> str:
            """The first date that follows one of the labels within 30 characters; earlier labels win."""
            for label in labels:
                for position, iso in every:
                    if label in lowered[max(position - 30 - len(label), 0):position]:
                        return iso
            return ""

        due = near(("due date", "payment due", "due by", "pay by", "due on", "due:"))
        dated = near(("invoice date", "date of issue", "issue date", "dated", "date:", "date", "তারিখ"))
        out["date"] = dated if dated and dated != due else next((iso for _, iso in every if iso != due), every[0][1])
        if due:
            out["due_date"] = due
    money = amounts(plain)
    codes = [code for _, _, code in money]
    common = max(sorted(set(codes)), key=codes.count) if codes else ""
    # the most specific label wins ("grand total" before "total"); of several, the last one in the document
    labelled = next((found[-1] for found in (list(pattern.finditer(plain)) for pattern in _TOTALS) if found), None)
    if labelled:
        sign = (labelled.group("pre") or labelled.group("post") or "").casefold()
        out["total"] = f"{Decimal((labelled.group('v1') or labelled.group('v2')).replace(',', '')):.2f}"
        currency = _CURRENCY.get(sign, sign.upper()) or common
        if currency:
            out["currency"] = currency
    elif money:
        largest = max(money, key=lambda item: Decimal(item[1]))
        out["total"], out["currency"], out["total_guessed"] = largest[1], largest[2], True
    ref = next((match.group("value") for match in _REFERENCE.finditer(plain) if re.search(r"\d", match.group("value"))), "")
    if ref:
        out["number"] = ref
    mails = list(dict.fromkeys(match.group(0).lower() for match in _EMAIL.finditer(plain)))[:5]
    if mails:
        out["emails"] = mails
    title = next((" ".join(line.split()) for line in text.splitlines() if len(line.strip()) >= 3), "")
    if title:
        out["title"] = title[:80]
    return out
