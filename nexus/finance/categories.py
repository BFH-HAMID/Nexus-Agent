"""Which category a transaction belongs to: what the operator taught first, then the keyword rules."""
from __future__ import annotations

from . import money
from .policy import TRANSFER, UNCATEGORIZED


class Categorizer:
    """Whole-word keyword matching. The longest keyword wins; a tie goes to the category listed first."""

    def __init__(self, rules: dict[str, list[str]], learned: dict[str, str] | None = None) -> None:
        self.names = list(rules)
        self.known = {*rules, TRANSFER}
        self._learned = {key: name for key, name in (learned or {}).items() if name in self.known}
        self._index: dict[str, list[tuple[tuple[str, ...], int, int, str]]] = {}
        order = 0
        for name, keywords in rules.items():
            for keyword in keywords:
                tokens = tuple(money.words(keyword))
                if tokens:
                    self._index.setdefault(tokens[0], []).append((tokens, len(" ".join(tokens)), order, name))
                    order += 1
        self._memo: dict[str, str] = {}

    def of(self, description: str) -> str:
        found = self._memo.get(description)
        if found is None:
            found = self._memo[description] = self._find(description)
        return found

    def by_rule(self, description: str) -> str:
        """The category from the keyword rules alone."""
        tokens = money.words(description)
        best: tuple[int, int, str] | None = None
        for at, token in enumerate(tokens):
            for keyword, length, order, name in self._index.get(token, ()):
                if tuple(tokens[at:at + len(keyword)]) == keyword and (best is None or (-length, order) < best[:2]):
                    best = (-length, order, name)
        return best[2] if best else UNCATEGORIZED

    def _find(self, description: str) -> str:
        return self._learned.get(money.merchant_key(description)) or self.by_rule(description)
