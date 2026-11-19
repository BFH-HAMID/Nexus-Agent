"""Operator policy for personal finance (``finance.yaml``)."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .. import policyfile

NAME = "finance.yaml"
UNCATEGORIZED = "uncategorized"
TRANSFER = "transfer"          # money moved between your own accounts: neither income nor spending
ROLES = ("date", "description", "amount", "debit", "credit", "balance", "currency", "id", "type")
DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "salary": ["salary", "payroll", "wages", "stipend"],
    "rent": ["rent", "house rent"],
    "groceries": ["grocery", "groceries", "supermarket", "super shop", "shwapno", "agora", "meena bazar", "unimart"],
    "eating_out": ["restaurant", "cafe", "coffee", "foodpanda", "food panda", "kfc", "pizza hut", "bakery"],
    "transport": ["uber", "pathao", "fuel", "petrol", "filling station", "parking", "toll", "railway", "bus ticket"],
    "utilities": ["electricity", "desco", "dpdc", "titas gas", "wasa", "internet", "broadband", "recharge", "water bill"],
    "health": ["pharmacy", "hospital", "clinic", "diagnostic", "doctor"],
    "shopping": ["daraz", "amazon", "aarong", "shopping"],
    "subscriptions": ["netflix", "spotify", "youtube premium", "google one", "icloud", "hoichoi", "chorki"],
    "cash": ["atm", "cash withdrawal"],
    "fees": ["fee", "fees", "service charge", "sms charge", "annual charge", "excise duty", "vat", "source tax"],
}
DEFAULTS = {
    "schema": 1,
    "enabled": False,
    "encrypt_at_rest": True,
    "currency": "BDT",
    "statements": {"decimal": ".", "day_first": True, "max_file_bytes": 2_000_000, "max_rows": 20_000},
    "accounts": {},
    "categories": {},
    "budgets": {},
    "limits": {"max_transactions": 20_000},
    "alerts": {"large_amount": 0, "unusual_factor": 3},
    "categorize": {"mode": "rules", "agent": "", "allow_cloud": False, "max_tokens_per_call": 600},
    "voice": False,
    "agent_tools": False,
}
_ACCOUNT = re.compile(r"[a-z][a-z0-9_-]{0,23}")
_CATEGORY = re.compile(r"[a-z][a-z0-9_]{1,24}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_ACCOUNT_KEYS = {"currency", "decimal", "day_first", "expenses", "columns"}


class FinancePolicyError(policyfile.PolicyError):
    """finance.yaml is invalid or personal finance is OFF."""


def _fail(message: str) -> None:
    raise FinancePolicyError(f"finance.yaml: {message}")


def _text(value: Any, limit: int, where: str) -> str:
    return policyfile.clean_text(value, limit, label=f"finance.yaml: {where}", error=FinancePolicyError).strip()


def _validate(cfg: dict[str, Any]) -> None:
    from .money import words

    if not _CURRENCY.fullmatch(cfg["currency"]):
        _fail("'currency' is a three-letter code such as BDT, USD or EUR")

    accounts = cfg["accounts"]
    if len(accounts) > 12:
        _fail("at most 12 accounts")
    clean_accounts: dict[str, dict[str, Any]] = {}
    for name, raw in accounts.items():
        if not isinstance(name, str) or not _ACCOUNT.fullmatch(name):
            _fail("account names use a-z, 0-9, _ and - (at most 24 characters, starting with a letter)")
        if raw is None:
            raw = {}
        if not isinstance(raw, dict) or set(raw) - _ACCOUNT_KEYS:
            _fail(f"account '{name}' takes only: {', '.join(sorted(_ACCOUNT_KEYS))}")
        entry: dict[str, Any] = {
            "currency": raw.get("currency", cfg["currency"]),
            "decimal": raw.get("decimal", cfg["statements"]["decimal"]),
            "day_first": raw.get("day_first", cfg["statements"]["day_first"]),
            "expenses": raw.get("expenses", "negative"),
            "columns": {},
        }
        if not isinstance(entry["currency"], str) or not _CURRENCY.fullmatch(entry["currency"]):
            _fail(f"account '{name}': 'currency' is a three-letter code such as BDT")
        if entry["decimal"] not in (".", ","):
            _fail(f"account '{name}': 'decimal' is \".\" or \",\"")
        if type(entry["day_first"]) is not bool:
            _fail(f"account '{name}': 'day_first' must be true or false")
        if entry["expenses"] not in ("negative", "positive"):
            _fail(f"account '{name}': 'expenses' is negative (money out has a minus sign) or positive")
        columns = raw.get("columns", {})
        if not isinstance(columns, dict) or set(columns) - set(ROLES):
            _fail(f"account '{name}': 'columns' maps {', '.join(ROLES)} to the column titles of the statement")
        for role, title in columns.items():
            entry["columns"][role] = _text(title, 60, f"account '{name}' column '{role}'")
            if not words(entry["columns"][role]):
                _fail(f"account '{name}': column '{role}' needs a title with letters or digits")
        if "amount" in columns and {"debit", "credit"} & set(columns):
            _fail(f"account '{name}': 'columns' takes either 'amount' or 'debit' / 'credit', not both")
        if len(set(map(tuple, map(words, entry["columns"].values())))) != len(columns):
            _fail(f"account '{name}': 'columns' names one title twice")
        clean_accounts[name] = entry
    cfg["accounts"] = clean_accounts

    categories = cfg["categories"]
    if len(categories) > 30:
        _fail("at most 30 categories")
    clean_categories: dict[str, list[str]] = {}
    for name, keywords in categories.items():
        if not isinstance(name, str) or not _CATEGORY.fullmatch(name) or name == UNCATEGORIZED:
            _fail("category names use a-z, 0-9 and _ (2-25 characters); 'uncategorized' is reserved")
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 40:
            _fail(f"category '{name}' needs a list of 1-40 keywords")
        kept = []
        for keyword in keywords:
            text = _text(keyword, 60, f"category '{name}' keyword").casefold()
            if len(text) < 2 or not words(text):
                _fail(f"category '{name}' has a keyword without two letters or digits")
            kept.append(text)
        clean_categories[name] = kept
    cfg["categories"] = clean_categories

    known = set(categories_of(cfg))
    budgets: dict[str, str] = {}
    for name, amount in cfg["budgets"].items():
        if name not in known or name == TRANSFER:
            _fail(f"budget '{str(name)[:30]}' is not one of your categories ({', '.join(sorted(known - {TRANSFER}))})")
        try:
            value = Decimal(str(amount)) if type(amount) in (int, float) else None
        except InvalidOperation:
            value = None
        if value is None or not value.is_finite() or not 0 < value <= Decimal(10) ** 12 or -value.as_tuple().exponent > 2:
            _fail(f"budget '{name}' must be an amount above 0 with at most two decimals")
        budgets[name] = format(value, "f")
    cfg["budgets"] = budgets


POLICY = policyfile.Policy(
    NAME, DEFAULTS, label="Personal finance", error=FinancePolicyError, validate=_validate,
    free=("accounts", "categories", "budgets"),
    ranges={"statements.max_file_bytes": (1000, 20_000_000), "statements.max_rows": (10, 100_000),
            "limits.max_transactions": (100, 100_000), "alerts.large_amount": (0, 10 ** 12),
            "alerts.unusual_factor": (2, 100), "categorize.max_tokens_per_call": (100, 4000)},
    choices={"statements.decimal": (".", ","), "categorize.mode": ("rules", "model")},
    limit=64_000,
)
load = POLICY.load
require = POLICY.require
enabled = POLICY.enabled


def categories_of(cfg: dict[str, Any]) -> dict[str, list[str]]:
    """The operator's categories, or the built-in ones when none are configured."""
    return cfg["categories"] or DEFAULT_CATEGORIES


def account(cfg: dict[str, Any], name: str) -> tuple[str, dict[str, Any]]:
    """``(name, settings)`` for a statement's account; a configured list of accounts is closed."""
    name = (name or "").strip() or ("main" if not cfg["accounts"] or "main" in cfg["accounts"] else "")
    if cfg["accounts"]:
        if name not in cfg["accounts"]:
            raise FinancePolicyError(f"Say which account this is with --account ({', '.join(sorted(cfg['accounts']))})")
        return name, cfg["accounts"][name]
    if not _ACCOUNT.fullmatch(name):
        raise FinancePolicyError("Account names use a-z, 0-9, _ and - (at most 24 characters, starting with a letter)")
    return name, {"currency": cfg["currency"], "decimal": cfg["statements"]["decimal"],
                  "day_first": cfg["statements"]["day_first"], "expenses": "negative", "columns": {}}
