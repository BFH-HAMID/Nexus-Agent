"""The World desk: what public data this machine actually holds, and how old it is.

The globe widget already fetches public feeds and keeps them in a bounded disk
cache with a TTL. What it never explained — in one place, in either language —
was *which* sources are configured, *when* each was last read, *how long* before
it is read again, and what happens when the network is down.

This module answers exactly that, and it is deliberately narrow:

* :func:`overview` **reads the cache and never the network.** Opening the page
  cannot spend a request, wake a publisher, or invent a headline. Every number
  is derived from a stored timestamp.
* :func:`refresh` is the only function that touches the network, and only when
  the operator types ``REFRESH PUBLIC SOURCES``. It reuses the widget's own
  fetcher (same budgets, no redirects, no cookies, no proxies), so a manual
  refresh cannot bypass a single guard the automatic one obeys.
* Nothing here writes a headline into an agent, a task, or a memory: public
  text stays display-only.

The dataset itself stays where it was — ``<workspace>/widget.yaml`` for the
configuration and ``<NEXUS_HOME>/world/<hash>/public-cache.json`` for the cache.
No new file, no new database.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .hud.world.sources import SOURCES

SCHEMA = 1
REFRESH_TOKEN = "REFRESH PUBLIC SOURCES"
GROUPS = ("news", "crypto", "quakes", "weather")
MAX_SOURCES = len(SOURCES)

NOTES = (
    "cache_only",
    "no_invented_data",
    "public_and_untrusted",
    "display_only",
)


# ── the world this server already shares with the widget ────────────────── #


def _world(state, world=None):
    """The same cached ``World`` instance the globe uses — never a second one."""
    if world is not None:
        return world
    return state.gui.world()


def _rows(world) -> dict[str, dict[str, Any]]:
    with world.cache.lock:
        return {key: dict(row) for key, row in world.cache.rows.items()}


def _feed(world, rows, key: str, *, group: str, name: str, ttl: int, category: str = "",
          terms: str = "") -> dict[str, Any]:
    row = rows.get(key) or {}
    at = row.get("at")
    now = world.cache.clock()
    age = max(0.0, now - float(at)) if isinstance(at, (int, float)) else None
    fresh = bool(isinstance(age, float) and age < ttl)
    return {
        "key": key.split(":", 1)[1] if key.startswith("news:") else key,
        "group": group,
        "name": name,
        "category": category,
        "terms": terms,
        "cache_key": key,
        "present": isinstance(at, (int, float)),
        "at": float(at) if isinstance(at, (int, float)) else None,
        "age_s": round(age, 1) if age is not None else None,
        "ttl_s": int(ttl),
        "due_in_s": max(0, int(ttl - age)) if age is not None else 0,
        "fresh": fresh,
        "status": "fresh" if fresh else ("stale" if isinstance(at, (int, float)) else "missing"),
        "items": _count(row.get("data")),
    }


def _count(data: Any) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return len(items)
        return 1 if data.get("current") else 0
    return 0


def _weather_key(world) -> str:
    location = world.config["default_location"]
    return f"weather:{world.config['weather_provider']}:{round(float(location['latitude']), 2)}:{round(float(location['longitude']), 2)}"


def feeds(world, rows=None) -> list[dict[str, Any]]:
    rows = _rows(world) if rows is None else rows
    config = world.config
    out = [
        _feed(world, rows, "news:" + source, group="news", name=SOURCES[source][1],
              ttl=config["refresh"]["news"], category=SOURCES[source][0], terms=SOURCES[source][3])
        for source in config["sources"]
    ]
    out.append(_feed(world, rows, "crypto", group="crypto", name="Crypto quotes", ttl=config["refresh"]["crypto"]))
    out.append(_feed(world, rows, "quakes", group="quakes", name="USGS earthquakes · 24 h",
                     ttl=config["refresh"]["quakes"]))
    location = config["default_location"]["name"]
    out.append(_feed(world, rows, _weather_key(world), group="weather", name=f"Weather · {location}",
                     ttl=config["refresh"]["weather"]))
    return out


def _group_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "feeds": len(rows),
        "fresh": sum(1 for row in rows if row["status"] == "fresh"),
        "stale": sum(1 for row in rows if row["status"] == "stale"),
        "missing": sum(1 for row in rows if row["status"] == "missing"),
        "items": sum(row["items"] for row in rows),
        "oldest_age_s": max((row["age_s"] for row in rows if row["age_s"] is not None), default=None),
    }


def overview(state, *, world=None) -> dict[str, Any]:
    """Everything the desk shows, read from the cache — no network at all."""
    box = _world(state, world)
    rows = _rows(box)
    config = box.config
    listing = feeds(box, rows)
    groups = {group: _group_summary([row for row in listing if row["group"] == group]) for group in GROUPS}
    try:
        key_present = bool(box.weather_key())
    except (OSError, ValueError, KeyError):
        key_present = False
    return {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "enabled": bool(config["enabled"]),
        "language": config["language"],
        "provider": config["weather_provider"],
        "provider_label": "WeatherAPI.com" if config["weather_provider"] == "weatherapi" else "Open-Meteo",
        "key_present": key_present,
        "location": dict(config["default_location"]),
        "refresh": dict(config["refresh"]),
        "sources": listing,
        "groups": groups,
        "totals": {
            "feeds": len(listing),
            "fresh": sum(group["fresh"] for group in groups.values()),
            "stale": sum(group["stale"] for group in groups.values()),
            "missing": sum(group["missing"] for group in groups.values()),
            "headlines": groups["news"]["items"],
        },
        "tokens": {"refresh": REFRESH_TOKEN},
        "limits": {"sources": MAX_SOURCES, "groups": list(GROUPS)},
        "notes": list(NOTES),
    }


# ── the only function that may touch the network ───────────────────────── #


def refresh(state, payload: dict[str, Any], *, world=None) -> dict[str, Any]:
    """Re-read the selected public groups. Typed token, one audit row, no writes."""
    if str(payload.get("confirmation") or "").strip() != REFRESH_TOKEN:
        raise ValueError(f"Type {REFRESH_TOKEN} to contact the public sources")
    box = _world(state, world)
    if not box.config["enabled"]:
        raise ValueError("World is disabled in widget.yaml; enable it before fetching")
    wanted = payload.get("groups")
    if wanted is None:
        wanted = [group for group in GROUPS if group != "weather" or _weather_configured(box)]
    if not isinstance(wanted, list) or not wanted or len(wanted) > len(GROUPS):
        raise ValueError("Choose one or more of news, crypto, quakes, weather")
    if any(group not in GROUPS for group in wanted):
        raise ValueError("Choose one or more of news, crypto, quakes, weather")
    wanted = list(dict.fromkeys(wanted))

    calls = {"news": box.news, "crypto": box.crypto, "quakes": box.quakes, "weather": box.weather}
    started = time.time()
    results: dict[str, Any] = {}
    if wanted:
        with ThreadPoolExecutor(max_workers=len(wanted)) as pool:
            futures = {group: pool.submit(calls[group]) for group in wanted}
            for group, future in futures.items():
                try:
                    results[group] = _result(group, future.result())
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    results[group] = {"group": group, "status": "error", "items": 0,
                                      "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}
    _audit(state, {"groups": wanted, "results": {name: row["status"] for name, row in results.items()},
                   "seconds": round(time.time() - started, 2)})
    return {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "seconds": round(time.time() - started, 2),
        "groups": wanted,
        "results": results,
        "overview": overview(state, world=box),
    }


def _weather_configured(world) -> bool:
    try:
        return bool(world.weather_key()) or world.config["weather_provider"] == "open-meteo"
    except (OSError, ValueError, KeyError):
        return False


def _result(group: str, value: Any) -> dict[str, Any]:
    """A small, honest summary of one group's fetch — never the payload itself."""
    if not isinstance(value, dict):
        return {"group": group, "status": "error", "items": 0, "detail": "unexpected response"}
    status = str(value.get("status") or ("ready" if value.get("items") else "unknown"))[:40]
    items = value.get("items")
    count = len(items) if isinstance(items, list) else _count(value.get("data"))
    offline = sum(1 for row in value.get("sources") or [] if row.get("status") == "offline")
    return {"group": group, "status": status, "items": count,
            **({"offline_sources": offline} if group == "news" else {})}


def _audit(state, detail: dict[str, Any]) -> None:
    from . import securitycenter

    try:
        securitycenter.audit_action(state, "world.refresh", "user", detail, risk="medium", target="world")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass  # an audit store that cannot be written must not undo a fetch the operator asked for


def audit_read(state, action: str, client: str, every: float = 120.0) -> None:
    from . import securitycenter

    try:
        securitycenter.audit_read(state, f"world.{action}", client or "unknown", every=every)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
