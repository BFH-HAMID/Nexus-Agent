"""When a card comes back: the SM-2 method (SuperMemo 2, P. Wozniak, 1987). Pure functions, whole numbers only.

Ease is kept in hundredths (250 = 2.50) so no floating point decides a date.

* ``again``  (quality 2): the card was missed. It starts again from the first
  interval and is due today once more. Its ease stays as it is.
* ``hard``   (quality 3): ease - 0.14
* ``good``   (quality 4): ease unchanged
* ``easy``   (quality 5): ease + 0.10

After a right answer the pause is ``first_interval_days``, then
``second_interval_days``, then the previous pause times the new ease.  Ease
never falls below 1.30.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

QUALITY = {"again": 2, "hard": 3, "good": 4, "easy": 5}
MIN_EASE = 130
MATURE_DAYS = 21          # a card with a pause this long counts as well known


def start(today: dt.date, settings: dict[str, Any]) -> dict[str, Any]:
    """The schedule of a card nobody has answered yet: due at once."""
    return {"reps": 0, "lapses": 0, "ease": round(settings["starting_ease"] * 100), "interval": 0,
            "due": today.isoformat(), "last": None}


def after(card: dict[str, Any], grade: str, today: dt.date, settings: dict[str, Any]) -> dict[str, Any]:
    """The schedule of ``card`` after it was answered with ``grade`` today. Changes nothing in place."""
    if grade not in QUALITY:
        raise ValueError(f"A grade is one of {', '.join(QUALITY)}")
    quality, reps, ease = QUALITY[grade], int(card["reps"]), int(card["ease"])
    if quality < 3:
        return {"reps": 0, "lapses": int(card["lapses"]) + (1 if reps else 0), "ease": ease, "interval": 0,
                "due": today.isoformat(), "last": today.isoformat()}
    miss = 5 - quality
    ease = max(MIN_EASE, ease + 10 - miss * (8 + miss * 2))
    reps += 1
    if reps == 1:
        interval = settings["first_interval_days"]
    elif reps == 2:
        interval = settings["second_interval_days"]
    else:
        before = max(int(card["interval"]), 1)
        interval = max(before + 1, (before * ease + 50) // 100)     # rounded half up, and always at least a day longer
    interval = min(int(interval), settings["max_interval_days"])
    return {"reps": reps, "lapses": int(card["lapses"]), "ease": ease, "interval": interval,
            "due": (today + dt.timedelta(days=interval)).isoformat(), "last": today.isoformat()}


def stage(card: dict[str, Any]) -> str:
    """``new`` (never answered), ``learning`` or ``mature`` (a pause of three weeks or more)."""
    if not card.get("last"):
        return "new"
    return "mature" if int(card["interval"]) >= MATURE_DAYS else "learning"
