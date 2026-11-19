"""Task 29 - personal finance (opt-in, ``finance.yaml``).

Reads bank statements you exported as CSV into a sealed ledger and reports
spending by category and month, budgets, recurring payments and unusual
transactions.  Read-only towards the world: it never connects to a bank,
never moves money and never goes online by itself.  See ``docs/finance.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import policy


def status(workspace: Path | str) -> dict[str, Any]:
    """Never-raising summary for ``nexus finance status``, ``doctor`` and the HUD. Counts and dates, no amounts."""
    info = policy.POLICY.describe(workspace)
    if not info.get("enabled"):
        return info
    try:
        from .core import Finance

        cfg = policy.load(workspace)
        info.update(currency=cfg["currency"], encrypt_at_rest=cfg["encrypt_at_rest"], categorize=cfg["categorize"]["mode"],
                    budgets=len(cfg["budgets"]), agent_tools=cfg["agent_tools"], voice=cfg["voice"],
                    connects_to_bank=False, moves_money=False)
        finance = Finance(workspace)
        accounts = finance.accounts()
        problem = finance.ledger.problem()
        if problem:
            info["error"] = f"the ledger cannot be sealed: {problem}"[:240]
        info.update(accounts=[row["account"] for row in accounts],
                    transactions=sum(row["transactions"] for row in accounts),
                    first=min((row["first"] for row in accounts), default=None),
                    last=max((row["last"] for row in accounts), default=None))
    except (ValueError, OSError) as exc:
        info["error"] = str(exc)[:240]
    return info
