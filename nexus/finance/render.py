"""Plain-text views of the finance reports for the terminal, ``/finance`` and speech."""
from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from . import money
from .policy import UNCATEGORIZED


def _amount(text: str) -> str:
    return money.fmt(Decimal(text))


def summary(report: dict[str, Any]) -> list[str]:
    where = "all accounts" if report["account"] == "all" else f"account {report['account']}"
    lines = [f"{report['month']}{' (latest month with transactions)' if report['latest'] else ''} - {where}"]
    for block in report["currencies"]:
        lines.append(f"{block['currency']}  income {_amount(block['income'])}   spending {_amount(block['spending'])}   "
                     f"net {_amount(block['net'])}")
        for item in block["categories"]:
            spent, received = Decimal(item["spent"]), Decimal(item["received"])
            parts = [f"  {item['category']:<18}"]
            if spent or not received:
                parts.append(f"{_amount(item['spent']):>14}  {item['share']:5.1f}%")
            if received:
                parts.append(f"  +{_amount(item['received'])} in")
            if "budget" in item:
                left = Decimal(item["left"])
                parts.append(f"  budget {_amount(item['budget'])}, "
                             + (f"OVER by {money.fmt(-left)}" if item["over"] else f"{money.fmt(left)} left"))
            lines.append("".join(parts))
        if Decimal(block["transfers_in"]) or Decimal(block["transfers_out"]):
            lines.append(f"  transfers (not counted): {_amount(block['transfers_out'])} out, "
                         f"{_amount(block['transfers_in'])} in")
    if report["uncategorized"]:
        lines.append(f"{report['uncategorized']} transaction(s) have no category: nexus finance categories")
    return lines


def months(rows: list[dict[str, Any]]) -> list[str]:
    lines = [f"{'month':<8} {'':3} {'income':>14} {'spending':>14} {'net':>14}"]
    lines += [f"{row['month']:<8} {row['currency']:3} {_amount(row['income']):>14} {_amount(row['spending']):>14} "
              f"{_amount(row['net']):>14}" for row in rows]
    return lines if rows else ["no transactions yet"]


def recurring(items: list[dict[str, Any]]) -> list[str]:
    lines = []
    for item in items:
        # a payment that did not come when it was due is shown with its last date, not with a "next" date in the past
        note = f", next about {item['next_expected']}" if item["active"] else f", last on {item['last']}: stopped?"
        if "changed" in item:
            note += f"  (last payment {_amount(item['changed']['to'])}, before {_amount(item['changed']['from'])})"
        lines.append(f"{item['every']:<16} {_amount(item['amount']):>12} {item['currency']}  {item['description'][:40]:<40} "
                     f"[{item['category']}]  {_amount(item['per_year'])} a year{note}")
    return lines or ["nothing looks recurring yet (it takes three payments to the same merchant)"]


def unusual(report: dict[str, Any]) -> list[str]:
    lines = [f"{item['date']}  {_amount(item['amount']):>14} {item['currency']}  {item['description'][:50]}\n"
             f"    {item['why']}" for item in report["items"]]
    return lines or [f"nothing stands out in {report['month']}"]


def preview(parsed: dict[str, Any]) -> list[str]:
    """What an import would do, for review before anything is stored."""
    names = ", ".join(f"{role}={title}" for role, title in parsed["columns"].items())
    lines = [f"{parsed['file']} -> account {parsed['account']} ({', '.join(parsed['currencies'])})",
             f"columns: {names}",
             f"dates: {parsed['first']} to {parsed['last']}"
             + (f", read as {parsed['date_order']}" + (" (statements.day_first; the file does not say)"
                                                      if parsed["date_order_assumed"] else "")
                if parsed["date_order"] else ""),
             f"{len(parsed['rows'])} transactions: {money.fmt(parsed['money_in'])} in, {money.fmt(parsed['money_out'])} out",
             f"new: {parsed['new']}   already in the ledger: {parsed['known']}"]
    check = parsed["balance_check"]
    if check["result"] == "ok":
        lines.append("the balance column agrees with the amounts")
    elif check["result"] == "inverted":
        lines.append("NOTE: the balance moves against the amounts. That is normal for a credit card or a loan (the "
                     "balance is what you owe). For a bank account it means money in and money out are swapped: "
                     "set expenses: positive for this account.")
    elif check["result"] == "mismatch":
        lines.append(f"NOTE: the balance column does not follow the amounts in {check['mismatches']} place(s), first "
                     f"at line {check['first_line']}: rows may be missing or the wrong columns were picked")
    lines += [f"NOTE: {note}" for note in parsed["notes"]]
    if parsed["ignored_count"]:
        lines.append(f"{parsed['ignored_count']} row(s) are not transactions and were left out:")
        lines += [f"  line {row['line']} ({row['reason']}): {row['text']}" for row in parsed["ignored"][:5]]
    lines.append("first rows:")
    for row in parsed["rows"][:5]:
        lines.append(f"  {row['date']}  {money.fmt(row['amount']):>14}  {row['description'][:50]}  [{row['category']}]")
    return lines


def spoken_amount(value: Decimal, currency: str) -> str:
    return f"{value.quantize(Decimal(1), rounding=ROUND_HALF_UP):,} {currency}"


def month_name(month: str) -> str:
    return dt.date.fromisoformat(month + "-01").strftime("%B %Y")


def _spoken_name(category: str) -> str:
    return "payments without a category" if category == UNCATEGORIZED else category.replace("_", " ")


def spoken_summary(report: dict[str, Any], currency: str, category: str = "") -> str:
    """Two short sentences for speech: the total, then where most of it went."""
    block = next((block for block in report["currencies"] if block["currency"] == currency), None)
    when = month_name(report["month"])
    if block is None:
        return f"I have no {currency} transactions for {when}."
    if category:
        item = next((item for item in block["categories"] if item["category"] == category), None)
        spent = Decimal(item["spent"]) if item else Decimal(0)
        return f"In {when} you spent {spoken_amount(spent, currency)} on {category.replace('_', ' ')}."
    top = [item for item in block["categories"] if Decimal(item["spent"])][:3]
    parts = ", then ".join(f"{_spoken_name(item['category'])} with {spoken_amount(Decimal(item['spent']), currency)}"
                           for item in top)
    return (f"In {when} you spent {spoken_amount(Decimal(block['spending']), currency)} and received "
            f"{spoken_amount(Decimal(block['income']), currency)}." + (f" Most went to {parts}." if parts else ""))


def spoken_budgets(report: dict[str, Any], currency: str) -> str:
    block = next((block for block in report["currencies"] if block["currency"] == currency), None)
    budgeted = [item for item in (block["categories"] if block else []) if "budget" in item]
    when = month_name(report["month"])
    if not budgeted:
        return "No budgets are set in finance.yaml."
    over = [item for item in budgeted if item["over"]]
    if not over:
        return f"In {when} every budget is within its limit."
    parts = ", ".join(f"{item['category'].replace('_', ' ')} by {spoken_amount(-Decimal(item['left']), currency)}"
                      for item in over)
    return f"In {when} you are over budget on {parts}."
