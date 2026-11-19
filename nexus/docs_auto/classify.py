"""Sort a document into one of the operator's categories by counting keywords. Deterministic."""
from __future__ import annotations

import re
from typing import Any

from .policy import UNSORTED


def scores(text: str, categories: dict[str, dict[str, Any]]) -> list[tuple[str, int]]:
    """``[(category, score)]``, best first. A keyword counts up to three times, plus one when it is near the top."""
    lowered = text.casefold()
    head = lowered[:300]
    out = []
    for name, rule in categories.items():
        total = 0
        for keyword in rule["keywords"]:
            pattern = re.compile(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)")
            hits = len(pattern.findall(lowered))
            if hits:
                total += min(hits, 3) + (1 if pattern.search(head) else 0)
        out.append((name, total))
    return sorted(out, key=lambda item: -item[1])  # stable: the operator's order breaks ties


def classify(text: str, categories: dict[str, dict[str, Any]], min_score: int) -> dict[str, Any]:
    """``{category, score, runner_up, method}``; ``unsorted`` when the keywords are too few or tied."""
    ranked = scores(text, categories)
    best = ranked[0] if ranked else (UNSORTED, 0)
    second = ranked[1] if len(ranked) > 1 else None
    sure = best[1] >= min_score and (second is None or best[1] > second[1])
    return {"category": best[0] if sure else UNSORTED, "score": best[1],
            "runner_up": {"category": second[0], "score": second[1]} if second and second[1] else None,
            "method": "rules"}
