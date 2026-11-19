"""Read one exported bank statement (CSV) into transactions. Nothing is stored here.

A statement is untrusted data: descriptions are written by whoever sent or
received the money.  The reader refuses a file rather than guess: a row that
looks like a transaction but cannot be read stops the import.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import itertools
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import money
from .money import Unreadable

SUFFIXES = (".csv", ".tsv", ".txt")
DELIMITERS = (",", ";", "\t", "|")
HEADER_SEARCH = 40          # the column titles must be within the first 40 rows
MAX_COLUMNS = 60
MAX_CELL = 400
MAX_TITLE = 60              # a longer cell, or one of more than eight words, is not a column title
MAX_TITLE_WORDS = 8
MAX_RECORD_LINES = 5        # a quoted value may run over a few lines; more than that is an unclosed quote
ALIASES: dict[str, tuple[str, ...]] = {   # per role, most specific first
    "date": ("transaction date", "txn date", "trans date", "date", "booking date", "posting date", "posted date",
             "post date", "value date", "তারিখ"),
    "description": ("description", "narration", "particulars", "details", "memo", "payee", "merchant", "remarks",
                    "name", "reference", "বিবরণ"),
    "amount": ("amount", "transaction amount", "amt", "পরিমাণ"),
    "debit": ("debit", "debits", "withdrawal", "withdrawals", "paid out", "money out", "dr", "ডেবিট"),
    "credit": ("credit", "credits", "deposit", "deposits", "paid in", "money in", "cr", "ক্রেডিট"),
    "balance": ("balance", "ব্যালেন্স"),
    "currency": ("currency", "ccy"),
    "id": ("transaction id", "txn id", "trans id", "trx id", "transaction reference", "reference no",
           "reference number", "ref no", "transaction number", "fitid", "id"),
    "type": ("transaction type", "txn type", "dr cr", "cr dr", "debit credit", "credit debit", "type"),
}
_EXACT_ONLY = frozenset({"name", "reference", "id", "type"})   # too common inside other titles
_PRECEDENCE = ("date", "balance", "type", "debit", "credit", "id", "currency", "amount", "description")
_EXACT: dict[tuple[str, ...], tuple[str, int]] = {}
for _role_name, _names in ALIASES.items():
    for _rank, _alias in enumerate(_names):
        _EXACT.setdefault(tuple(_alias.split()), (_role_name, _rank))
_INSIDE = tuple((tuple(alias.split()), role, 100 + rank) for role in _PRECEDENCE
                for rank, alias in enumerate(ALIASES[role]) if alias not in _EXACT_ONLY)
_TYPE_WORDS = {**dict.fromkeys(("dr", "d", "debit", "db", "withdrawal", "out"), "dr"),
               **dict.fromkeys(("cr", "c", "credit", "deposit", "in"), "cr")}
_NOT_CSV = ((b"PK\x03\x04", "a spreadsheet (xlsx)"), (b"%PDF-", "a PDF"), (b"\xd0\xcf\x11\xe0", "a spreadsheet (xls)"))
_CURRENCY = re.compile(r"[A-Za-z]{3}")


def _title(cell: str) -> tuple[str, ...]:
    if len(cell) > MAX_TITLE:
        return ()
    found = tuple(money.words(cell))
    return found if len(found) <= MAX_TITLE_WORDS else ()


def _contains(title: tuple[str, ...], alias: tuple[str, ...]) -> bool:
    return any(title[at:at + len(alias)] == alias for at in range(len(title) - len(alias) + 1))


def _role(title: tuple[str, ...]) -> tuple[str, int] | None:
    """``(role, rank)`` of one column title; a lower rank is a better match for that role."""
    if not title:
        return None
    exact = _EXACT.get(title)
    if exact:
        return exact
    present = set(title)
    for alias, role, rank in _INSIDE:
        if alias[0] in present and _contains(title, alias):
            return role, rank
    return None


def _key(cells: list[str]) -> tuple[str, ...]:
    """A row of titles as it compares with a repeat of itself further down (page breaks)."""
    key = [" ".join(cell.casefold().split()) for cell in cells]
    while key and not key[-1]:
        key.pop()
    return tuple(key)


def columns(header: list[str], wanted: dict[str, str]) -> dict[str, Any] | None:
    """Which column holds what, or ``None`` when this row is not the row of column titles.

    Titles the operator named (``accounts.<name>.columns``) are matched exactly and
    win; the other roles are recognised by their usual titles.
    """
    titles = [_title(cell) for cell in header]
    found: dict[str, list[tuple[int, int]]] = {}
    for role, name in wanted.items():
        at = [index for index, title in enumerate(titles) if title and title == _title(name)]
        if not at:
            return None
        found[role] = [(0, at[0])]
    named = {hits[0][1] for hits in found.values()}
    closed = set(wanted) | ({"debit", "credit"} if "amount" in wanted else set()) \
        | ({"amount"} if {"debit", "credit"} & set(wanted) else set())
    for index, title in enumerate(titles):
        hit = _role(title) if index not in named else None
        if hit and hit[0] not in closed:
            found.setdefault(hit[0], []).append((hit[1], index))
    if "date" not in found or not {"amount", "debit", "credit"} & set(found):
        return None
    if "debit" in found and "credit" in found:
        found.pop("amount", None)
    elif "amount" in found:
        found.pop("debit", None)
        found.pop("credit", None)
    out: dict[str, Any] = {"description": [index for _rank, index in found.get("description", [])]}
    for role in ("date", "amount", "debit", "credit", "balance", "currency", "id", "type"):
        hits = sorted(found.get(role, []))
        if len(hits) > 1 and role in ("amount", "debit", "credit"):
            names = ", ".join(f"'{money.tidy(header[index], 30)}'" for _rank, index in hits)
            raise Unreadable(f"more than one column could be the {role} ({names}): name the right one under "
                             f"accounts.<name>.columns in finance.yaml, for example  columns: {{{role}: "
                             f"\"{money.tidy(header[hits[-1][1]], 30)}\"}}")
        if len(hits) > 1 and role == "balance" and sum(rank < 100 for rank, _index in hits) != 1:
            hits = []  # several balances (ledger, available ...): none is checked rather than the wrong one
        out[role] = hits[0][1] if hits else None
    return out


def decode(data: bytes) -> tuple[str, str]:
    for magic, kind in _NOT_CSV:
        if data.startswith(magic):
            raise Unreadable(f"this is {kind}, not a CSV file: export or save the statement as CSV")
    try:
        if data.startswith(b"\xef\xbb\xbf"):
            return data[3:].decode("utf-8"), "utf-8"
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return data.decode("utf-16"), "utf-16"
        if b"\x00" in data:
            raise Unreadable("not a text file: export or save the statement as CSV")
        try:
            return data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            return data.decode("cp1252"), "windows-1252"
    except UnicodeDecodeError:
        raise Unreadable("the text is neither UTF-8 nor Windows-1252: save the statement as UTF-8 CSV") from None


def _records(text: str, delimiter: str):
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    before = 0
    for record in reader:
        if reader.line_num - before > MAX_RECORD_LINES:
            raise Unreadable(f"line {before + 1}: a quoted value runs over {reader.line_num - before} lines "
                             "(a quotation mark that is never closed?)")
        before = reader.line_num
        if len(record) > MAX_COLUMNS:
            raise Unreadable(f"line {reader.line_num}: more than {MAX_COLUMNS} columns")
        yield reader.line_num, [cell[:MAX_CELL].strip() for cell in record]


def _titles(cells: list[str]) -> bool:
    """Does this row name a date and an amount column, as a row of column titles does?"""
    try:
        return columns(cells, {}) is not None
    except Unreadable:
        return True   # titles that cannot be told apart are titles all the same


def _layout(text: str, wanted: dict[str, str]) -> tuple[str, list[tuple[int, list[str], dict[str, Any] | None]]]:
    """``(delimiter, rows of column titles)`` among the first rows of the file.

    Each row is ``(line, cells, columns)``; ``columns`` is ``None`` for titles that
    cannot be told apart.  A repeat of an earlier row of titles is not listed.
    """
    forced = re.match(r"sep=(.)\r?\n", text)
    best: tuple[int, str, list[tuple[int, list[str], dict[str, Any] | None]]] | None = None
    refusal: Unreadable | None = None
    for delimiter in ((forced[1],) if forced and forced[1] in DELIMITERS else DELIMITERS):
        rows: list[tuple[int, list[str], dict[str, Any] | None]] = []
        seen: set[tuple[str, ...]] = set()
        try:
            for line, cells in itertools.islice(_records(text, delimiter), HEADER_SEARCH):
                if len(cells) < 2 or _key(cells) in seen:
                    continue
                try:
                    found = columns(cells, wanted)
                except Unreadable as exc:
                    refusal = refusal or exc
                    rows.append((line, cells, None))
                    continue
                if found:
                    rows.append((line, cells, found))
                    seen.add(_key(cells))
        except (csv.Error, Unreadable):
            pass  # not this delimiter, or not from here on
        width = max((len(cells) for _line, cells, found in rows if found), default=0)
        if width and (best is None or width > best[0]):
            best = (width, delimiter, rows)
    if best is None:
        if refusal:
            raise refusal
        hint = ("the column titles named under accounts.<name>.columns" if wanted
                else "column titles with a date and an amount (or debit and credit)")
        raise Unreadable(f"no row in the first {HEADER_SEARCH} has {hint}"
                         + ("" if wanted else ": name the columns under accounts.<name>.columns in finance.yaml"))
    return best[1], best[2]


class _Table:
    """One row of column titles and how the rows below it are read."""

    def __init__(self, line: int, header: list[str], col: dict[str, Any], settings: dict[str, Any]) -> None:
        self.line, self.header, self.col, self.settings = line, header, col, settings
        self.paired = col["debit"] is not None or col["credit"] is not None
        self.width = max(index for index, title in enumerate(header) if title) + 1
        self.key = _key(header)

    def cell(self, cells: list[str], index: int | None) -> str:
        return cells[index] if index is not None and index < len(cells) else ""

    def _one(self, cells: list[str], role: str) -> money.Amount | None:
        text = self.cell(cells, self.col[role])
        try:
            return money.parse_amount(text, self.settings["decimal"])
        except Unreadable as exc:
            raise Unreadable(f"{role} '{money.tidy(text, 24)}': {exc}") from None

    def amount(self, cells: list[str]) -> tuple[Decimal | None, str]:
        """``(amount, DR/CR note)`` of one row; with debit and credit columns, money in is positive."""
        if not self.paired:
            single = self._one(cells, "amount")
            return (single.value, single.marker) if single else (None, "")
        debit, credit = self._one(cells, "debit"), self._one(cells, "credit")
        if debit is None and credit is None:
            return None, ""
        out = abs(debit.value) if debit else Decimal(0)
        back = abs(credit.value) if credit else Decimal(0)
        if out and back:
            raise Unreadable(f"a debit ({money.plain(out)}) and a credit ({money.plain(back)}) in one row")
        return back - out, ""

    def row(self, cells: list[str]) -> tuple[str, Any]:
        """``("tx", fields)``, ``("skip", reason)`` or ``("error", message)`` for one row below the titles."""
        # the amount first: it decides whether this row is a transaction at all
        try:
            amount, marker = self.amount(cells)
            amount_error = ""
        except Unreadable as exc:
            amount, marker, amount_error = None, "", str(exc)
        date_text = self.cell(cells, self.col["date"])
        try:
            day = money.parse_day(date_text)
            date_error = ""
        except Unreadable as exc:
            day, date_error = None, str(exc)
        if day is None:
            if date_error and amount and any(ch.isdigit() for ch in date_text):
                return "error", f"date '{money.tidy(date_text, 24)}': {date_error}"
            return "skip", "no date"
        if amount_error:
            return "error", amount_error
        if amount is None:
            return "skip", "no amount"
        if not amount:
            return "skip", "zero amount"
        beyond = [value for value in cells[self.width:] if value]
        if beyond:
            return "error", (f"{len(cells)} values under {self.width} column titles ('{money.tidy(beyond[0], 24)}' has "
                             "none): a separator inside a number or a description that is not quoted?")
        try:
            balance = money.parse_amount(self.cell(cells, self.col["balance"]), self.settings["decimal"])
        except Unreadable:
            balance = None
        code = self.cell(cells, self.col["currency"])
        kind = self.cell(cells, self.col["type"])
        return "tx", {
            "day": day, "amount": amount, "marker": marker, "kind": " ".join(money.words(kind)), "type_text": kind,
            "text": [self.cell(cells, index) for index in self.col["description"]],
            "balance": None if balance is None else (-balance.value if balance.marker == "dr" else balance.value),
            "currency": code.upper() if _CURRENCY.fullmatch(code) else self.settings["currency"],
            "ref": money.tidy(self.cell(cells, self.col["id"]), 80),
        }


def _names(cells: list[str]) -> str:
    return ", ".join(f"'{money.tidy(cell, 24)}'" for cell in cells[:4] if cell) + (", ..." if len(cells) > 4 else "")


def _pick(text: str, delimiter: str, titles: list[tuple[int, list[str], dict[str, Any] | None]],
          settings: dict[str, Any]) -> tuple[_Table, list[str]]:
    """Of several rows of column titles, the one with the most transactions under it.

    A statement may carry a summary block with titles of its own above or below
    the transactions.  Each row of titles owns the rows down to the next one.
    """
    tables = [(line, _Table(line, cells, col, settings) if col else None) for line, cells, col in titles]
    usable = [table for _line, table in tables if table]
    if len(usable) == 1:
        return usable[0], []
    lines = {line for line, _table in tables}
    counts = dict.fromkeys(lines, 0)
    try:
        for line, cells in itertools.islice(_records(text, delimiter), HEADER_SEARCH + 200):
            owner = max((entry for entry in tables if entry[0] < line), key=lambda entry: entry[0], default=None)
            if owner and owner[1] and line not in lines and any(cells) and owner[1].row(cells)[0] == "tx":
                counts[owner[0]] += 1
    except (csv.Error, Unreadable):
        pass
    chosen = max(usable, key=lambda table: (counts[table.line], bool(table.col["description"]), -table.line))
    notes = [f"line {table.line} holds other column titles ({_names(table.header)}): the "
             f"{counts[table.line]} row(s) under it were not read"
             for table in usable if table.line < chosen.line and counts[table.line]]
    if len(notes) > 3:
        notes[3:] = [f"{len(notes) - 3} more rows of other column titles above line {chosen.line} were passed over"]
    return chosen, notes


def _balance_check(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Does the running balance move with the amounts? The file may list the newest row first or last."""
    pairs = [(a, b) for a, b in itertools.pairwise(rows) if a["balance"] is not None and b["balance"] is not None]
    if not pairs:
        return {"result": "none"}
    tests = {
        "down": lambda a, b: b["balance"] - a["balance"] == b["amount"],     # oldest row first
        "up": lambda a, b: a["balance"] - b["balance"] == a["amount"],       # newest row first
        "down_inverted": lambda a, b: b["balance"] - a["balance"] == -b["amount"],
        "up_inverted": lambda a, b: a["balance"] - b["balance"] == -a["amount"],
    }
    good = {name: sum(1 for a, b in pairs if test(a, b)) for name, test in tests.items()}
    if max(good["down"], good["up"]) == len(pairs):
        return {"result": "ok", "pairs": len(pairs)}
    if max(good["down_inverted"], good["up_inverted"]) == len(pairs):
        return {"result": "inverted", "pairs": len(pairs)}
    name = "down" if good["down"] >= good["up"] else "up"
    first = next(b["line"] if name == "down" else a["line"] for a, b in pairs if not tests[name](a, b))
    return {"result": "mismatch", "pairs": len(pairs), "mismatches": len(pairs) - good[name], "first_line": first}


def read(path: Path, account: str, settings: dict[str, Any], *, max_bytes: int, max_rows: int,
         today: dt.date) -> dict[str, Any]:
    """Parse one statement file. Raises ``Unreadable`` with the line numbers when it cannot be trusted."""
    path = Path(path)
    try:
        return _read(path, account, settings, max_bytes, max_rows, today)
    except Unreadable as exc:
        raise Unreadable(f"{money.tidy(path.name, 80)}: {exc}") from None
    except csv.Error as exc:
        reason = "a quoted value never ends: a quotation mark that is never closed?" if "field limit" in str(exc) \
            else str(exc)[:80]
        raise Unreadable(f"{money.tidy(path.name, 80)}: not a well-formed CSV file ({reason})") from None


def _read(path: Path, account: str, settings: dict[str, Any], max_bytes: int, max_rows: int,
          today: dt.date) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise Unreadable("not a regular file")
    if path.suffix.casefold() not in SUFFIXES:
        raise Unreadable(f"import a CSV export of the statement ({', '.join(SUFFIXES)})")
    if path.stat().st_size > max_bytes:
        raise Unreadable("larger than statements.max_file_bytes")
    data = path.read_bytes()[:max_bytes + 1]
    if len(data) > max_bytes:
        raise Unreadable("larger than statements.max_file_bytes")
    text, encoding = decode(data)
    delimiter, titles = _layout(text, settings["columns"])
    table, notes = _pick(text, delimiter, titles, settings)
    col, header = table.col, table.header
    if encoding == "windows-1252":
        notes.append("the file is not UTF-8 and was read as Windows-1252; check the descriptions")

    raw: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    errors: list[str] = []
    other: tuple[int, list[str]] | None = None      # a later row with other column titles: reading stops there
    left = 0
    budget = max(50_000, 3 * max_rows)               # cells of text looked at for being such a row

    def skip(line: int, reason: str, cells: list[str]) -> None:
        ignored.append({"line": line, "reason": reason, "text": money.mask(money.tidy(" ".join(c for c in cells if c), 80))})

    for line, cells in _records(text, delimiter):
        if line <= table.line or not any(cells):
            continue
        if other:
            left += 1
            continue
        if len(raw) + len(ignored) >= max_rows:
            raise Unreadable("more than statements.max_rows rows")
        if len(errors) >= 50:
            break
        kind, value = table.row(cells)
        if kind == "tx":
            raw.append({"line": line, **value})
        elif kind == "error":
            errors.append(f"line {line}: {value}")
        else:
            filled = sum(1 for item in cells if item)
            if value == "no date" and filled > 1:
                budget -= filled
                if budget < 0:
                    raise Unreadable("too many rows of text without a date: this is not a statement I can read safely")
                if _key(cells) == table.key:
                    value = "the column titles again"
                elif _titles(cells):
                    other = (line, cells)
                    continue
            skip(line, value, cells)
    if other:
        notes.append(f"line {other[0]} holds other column titles ({_names(other[1])}): the {left} row(s) below it "
                     "were not read")

    # the type column is used only when every row says debit or credit in it
    typed = col["type"] is not None and not table.paired and all(row["kind"] in _TYPE_WORDS for row in raw) and bool(raw)
    if col["type"] is not None and not typed:
        col["description"] = sorted({*col["description"], col["type"]})
        for row in raw:
            row["text"].append(row["type_text"])

    # day/month or month/day: the file must be consistent, and says so itself when any day is above 12
    numeric = [row for row in raw if row["day"].date is None]
    day_first_rows = [row["line"] for row in numeric if row["day"].first > 12]
    month_first_rows = [row["line"] for row in numeric if row["day"].second > 12]
    if day_first_rows and month_first_rows:
        raise Unreadable(f"the dates are day/month in line {day_first_rows[0]} and month/day in line "
                         f"{month_first_rows[0]}")
    day_first = bool(day_first_rows) or (not month_first_rows and settings["day_first"])
    assumed = bool(numeric) and not day_first_rows and not month_first_rows

    rows: list[dict[str, Any]] = []
    earliest, latest = dt.date(1990, 1, 1), today + dt.timedelta(days=366)
    for row in raw:
        line = row["line"]
        try:
            date = money.settle(row["day"], day_first)
        except Unreadable as exc:
            errors.append(f"line {line}: date: {exc}")
            continue
        if not earliest <= date <= latest:
            errors.append(f"line {line}: date {date.isoformat()} is outside 1990 .. one year from now")
            continue
        amount = row["amount"]
        if row["marker"] or typed:
            direction = row["marker"] or _TYPE_WORDS[row["kind"]]
            if typed and row["marker"] and _TYPE_WORDS[row["kind"]] != row["marker"]:
                errors.append(f"line {line}: the amount and the type column disagree about debit or credit")
                continue
            if amount < 0 and direction == "cr":
                errors.append(f"line {line}: a negative amount that is marked as a credit: I cannot tell which way it goes")
                continue
            amount = -abs(amount) if direction == "dr" else abs(amount)
        elif not table.paired and settings["expenses"] == "positive":
            amount = -amount
        text = money.tidy(" ".join(dict.fromkeys(part for part in row["text"] if part)), 200)
        rows.append({"line": line, "date": date.isoformat(), "amount": amount, "currency": row["currency"],
                     "description": money.mask(text), "balance": row["balance"], "ref": row["ref"],
                     "_text": " ".join(text.casefold().split())})
    if errors:
        errors.sort(key=lambda message: int(message[5:message.index(":")]))
        more = f" (and {len(errors) - 5} more)" if len(errors) > 5 else ""
        raise Unreadable(f"{'; '.join(errors[:5])}{more}. Nothing was imported.")
    if not rows:
        raise Unreadable(f"no transactions found below the column titles in line {table.line}"
                         + (f" ({len(ignored)} rows without a date or an amount)" if ignored else ""))

    # identity: account, date, amount and currency, with the bank's own id when every row has a different one,
    # otherwise with the description and its count among identical rows of that day
    refs = [row["ref"] for row in rows]
    by_ref = col["id"] is not None and all(refs) and len(set(refs)) == len(refs)
    if col["id"] is not None and not by_ref:
        notes.append("the id column is empty or repeats in some rows and is not used to recognise transactions")
    seen: dict[tuple[str, ...], int] = {}
    for row in rows:
        content = (account, row["date"], format(row["amount"].normalize(), "f"), row["currency"])
        if by_ref:
            identity = ("id", *content, row["ref"])   # the wording of the description may change between exports
        else:
            content = (*content, row["_text"])
            seen[content] = seen.get(content, 0) + 1
            identity = ("row", *content, str(seen[content]))
        row["id"] = "tx-" + hashlib.sha256("\x1f".join(identity).encode("utf-8")).hexdigest()[:16]
        row["account"] = account
        del row["_text"], row["ref"]

    check = _balance_check(rows)
    newest_first = rows[0]["date"] > rows[-1]["date"]
    oldest_first = rows[0]["date"] < rows[-1]["date"]
    closing = rows[0] if newest_first else rows[-1] if oldest_first else None
    names = {role: money.tidy(header[col[role]], 40) for role in ("date", "amount", "debit", "credit", "balance",
                                                                  "currency", "id", "type") if col[role] is not None}
    if col["type"] is not None and not typed:
        names.pop("type", None)
    if not by_ref:
        names.pop("id", None)
    names["description"] = ", ".join(money.tidy(header[index], 40) for index in col["description"]) or "(none)"
    return {
        "file": money.tidy(path.name, 80), "sha256": hashlib.sha256(data).hexdigest(), "account": account,
        "currency": settings["currency"], "encoding": encoding, "delimiter": delimiter, "header_line": table.line,
        "columns": names, "date_order": ("day/month" if day_first else "month/day") if numeric else "",
        "date_order_assumed": assumed, "rows": rows, "ignored": ignored[:20], "ignored_count": len(ignored),
        "balance_check": check, "notes": notes,
        "closing": ({"balance": money.plain(closing["balance"]), "date": closing["date"]}
                    if closing and closing["balance"] is not None else None),
    }
