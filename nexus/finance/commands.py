"""``/finance`` in the REPL and, with ``voice: true``, two spoken questions. All read-only."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import policy

OFF = "Personal finance is OFF (operator opt-in: finance.yaml enabled: true)."
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
          "december")
_ASK = re.compile(r"(?:please )?(?:tell me )?(?:how much|what) (?:did|have) (?:i|we) spen[dt]\b")
_BUDGET = re.compile(r"(?:(?:am i|are we) (?:over|within|under|on) (?:my |our |the )?budget|"
                     r"(?:how is|how's|what is|what's) (?:my|our|the) budget(?: status| looking)?|budget status)\b")
_NAME = re.compile(r"[a-z][a-z _-]{1,29}")
_PERIODS = ("this month", "last month", *(f"in {name}" for name in MONTHS))


def _period(rest: str) -> tuple[str, str]:
    """``(period, what is left)``: the period may come before or after the category."""
    for phrase in _PERIODS:
        if rest == phrase:
            return phrase, ""
        if rest.endswith(" " + phrase):
            return phrase, rest[:-len(phrase)].strip()
        if rest.startswith(phrase + " "):
            return phrase, rest[len(phrase):].strip()
    return "", rest


def parse(text: str) -> dict[str, str] | None:
    """A spoken money question as ``{"kind", "when", "what"}``, or ``None`` when the words are something else."""
    spoken = " ".join(str(text or "").split())
    if len(spoken) > 120:
        return None
    spoken = spoken.rstrip("?.! ").lower().replace("\u2019", "'")
    match = _ASK.match(spoken)
    if match:
        when, rest = _period(spoken[match.end():].strip())
        if not rest:
            return {"kind": "spent", "when": when, "what": ""}
        if not rest.startswith("on "):
            return None
        what = rest[3:].strip()
        for article in ("the ", "my ", "our "):
            what = what.removeprefix(article)
        if not _NAME.fullmatch(what):
            return None
        return {"kind": "spent", "when": when, "what": what.replace(" ", "_").replace("-", "_")}
    match = _BUDGET.match(spoken)
    if match:
        when, rest = _period(spoken[match.end():].strip())
        return None if rest else {"kind": "budget", "when": when, "what": ""}
    return None


def count(text: str, default: int = 6) -> int:
    """A typed number of months; anything that is not a short plain number gives ``default``."""
    text = str(text or "").strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 4 else default


def month_of(when: str, today) -> str:
    """``YYYY-MM`` for "this month", "last month" or "in october" (the latest October that has begun); else ``""``."""
    first = today.replace(day=1)
    if when == "this month":
        return first.isoformat()[:7]
    if when == "last month":
        return first.replace(year=first.year - (first.month == 1), month=first.month - 1 or 12).isoformat()[:7]
    if when.startswith("in "):
        number = MONTHS.index(when[3:]) + 1
        return f"{first.year - (number > first.month)}-{number:02d}"
    return ""


def _service(workspace: Path | str, ctx: dict[str, Any]):
    import time

    from .core import Finance

    return Finance(workspace, ctx.get("config"), ctx.get("providers"), keyring_module=ctx.get("keyring_module"),
                   clock=ctx.get("clock") or time.time)


def handle(workspace: Path | str, text: str, ctx: dict[str, Any] | None = None) -> tuple[str, str] | None:
    """``(reply, intent)`` for a spoken money question; only with ``voice: true`` in finance.yaml."""
    intent = parse(text)
    if intent is None or not policy.enabled(workspace, "voice"):
        return None
    from . import render
    from .core import NoTransactions

    ctx = ctx or {}
    if ctx.get("tainted") or ctx.get("speaker_verified") is False:
        return "I only talk about your money on a trusted, verified request.", "finance_refused"
    if ctx.get("privacy_mode"):
        return ("Voice privacy mode is active, so I won't say your figures aloud. "
                "Read them with: nexus finance summary."), "finance_private"
    try:
        finance = _service(workspace, ctx)
        month = month_of(intent["when"], finance.today())
        if intent["what"] and intent["what"] not in finance.rules:
            names = [name.replace("_", " ") for name in finance.rules]
            return (f"I have no category called {intent['what'].replace('_', ' ')}. Your categories are "
                    f"{', '.join(names[:12])}{' and more' if len(names) > 12 else ''}."), "finance_unknown_category"
        try:
            report = finance.summary(month)
        except NoTransactions as exc:
            if not exc.last:
                return "I have no transactions yet. Import a statement first.", "finance_no_data"
            return f"I have no transactions for {render.month_name(month)}. The latest one is from {exc.last}.", "finance_no_data"
        currency = finance.cfg["currency"]
        if intent["kind"] == "budget":
            return render.spoken_budgets(report, currency), "finance_budget"
        return render.spoken_summary(report, currency, intent["what"]), "finance_spent"
    except (ValueError, OSError) as exc:
        return f"Personal finance: {str(exc)[:200]}", "finance_error"


def slash(workspace: Path | str, argument: str, ctx: dict[str, Any] | None = None) -> str:
    """``/finance`` | ``/finance summary [YYYY-MM]`` | ``months`` | ``recurring`` | ``unusual [YYYY-MM]``."""
    if not policy.enabled(workspace):
        return OFF
    from . import render

    finance = _service(workspace, ctx or {})
    verb, _, rest = argument.strip().partition(" ")
    rest = rest.strip()
    if verb in ("", "summary"):
        return "\n".join(render.summary(finance.summary(rest)))
    if verb == "months":
        return "\n".join(render.months(finance.months(count(rest))))
    if verb == "recurring":
        return "\n".join(render.recurring(finance.recurring()))
    if verb == "unusual":
        return "\n".join(render.unusual(finance.unusual(rest)))
    return "Usage: /finance [summary [YYYY-MM]] | months [N] | recurring | unusual [YYYY-MM]"
