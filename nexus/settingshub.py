"""The settings hub: one honest read of what this workspace and build remember.

The dashboard used to scatter its preferences across three places — two
browser toggles in ``gui.js``, a layout table inside the Workbench dialog, and
a World switches file — with no page that said *where a choice lives* or *how
old the stored value is*. This module answers that question for the Settings
page and nothing else:

* :func:`summary` is **read-only**. It opens no socket, writes no file and
  starts no task. Every value is copied from configuration that already exists.
* Values that the browser owns (theme, compact layout, reduce effects) are
  reported as *keys*, never as values: the server cannot read them and does not
  pretend to.
* Nothing secret is echoed — a weather credential is reported as a boolean, and
  paths are reported as counts.

There is no new store: the workspace preferences stay in the Workbench SQLite
file, the World switches stay in ``widget.yaml``, and the display choices stay
in the browser's own storage.
"""

from __future__ import annotations

import time
from typing import Any

from . import __version__

SCHEMA = 1
# The browser owns these; the page reads and writes them locally.
DISPLAY_KEYS = ("nexus.ui.theme", "nexus.ui.compact", "nexus.ui.still", "nexus.ui.lang")
NOTES = ("read_only", "browser_display_stored_locally", "no_secret_values", "presentation_only")


def _workspace_prefs(state) -> dict[str, Any]:
    """The Workbench's persisted preferences, summarised — never a second copy."""
    try:
        prefs = state.workbench.settings()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"available": False, "reason": "preferences_unavailable"}
    layout = prefs.get("layout") if isinstance(prefs.get("layout"), dict) else {}
    cards = layout.get("cards") if isinstance(layout.get("cards"), dict) else {}
    pricing = prefs.get("pricing") if isinstance(prefs.get("pricing"), dict) else {}
    mapping = prefs.get("role_models") if isinstance(prefs.get("role_models"), dict) else {}
    paths = prefs.get("write_paths") if isinstance(prefs.get("write_paths"), list) else []
    language = prefs.get("language") if prefs.get("language") in {"en", "bn"} else "en"
    return {
        "available": True,
        "language": language,
        "local_only": prefs.get("local_only") is True,
        "focus_mode": layout.get("focus") is True,
        "hidden_cards": sum(1 for card in cards.values() if isinstance(card, dict) and card.get("hidden") is True),
        "resized_cards": sum(1 for card in cards.values() if isinstance(card, dict) and bool(card.get("height"))),
        "default_local_model": str(prefs.get("default_local_model") or ""),
        "write_folders": len(paths),
        "priced_models": len(pricing),
        "mapped_roles": len(mapping),
    }


def _world_switches(state) -> dict[str, Any]:
    """The World page's saved switches. No publisher is contacted to read them."""
    try:
        from .hud.world.core import settings as world_settings

        cfg = world_settings(state.workspace)
        key = getattr(state, "weatherapi_key", "")
        if not key:
            try:
                from .hud.world.weatherapi import credential

                key = credential(state.workspace)
            except (OSError, ValueError, KeyError, TypeError):
                key = ""
        sources = cfg.get("sources") if isinstance(cfg.get("sources"), list) else []
        authorized = cfg.get("authorized_sources") if isinstance(cfg.get("authorized_sources"), list) else []
        return {
            "available": True,
            "enabled": cfg.get("enabled") is True,
            "provider": str(cfg.get("weather_provider") or ""),
            "language": cfg.get("language") if cfg.get("language") in {"en", "bn"} else "en",
            "sources": len(sources),
            "authorized_sources": len(authorized),
            "weather_key_present": bool(key),
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"available": False, "reason": "configuration_unavailable"}


def summary(state) -> dict[str, Any]:
    """Everything the Settings page shows about this workspace, at one instant."""
    config = getattr(state, "config", None)
    roles = getattr(config, "roles", {}) or {}
    agents = getattr(config, "agents", {}) or {}
    pipelines = getattr(config, "pipelines", {}) or {}
    return {
        "schema": SCHEMA,
        "observed_at": time.time(),
        "build": {
            "version": __version__,
            "demo": bool(getattr(state, "demo", False)),
            "auth_required": bool(getattr(state, "token", "")),
            "roles": len(roles),
            "agents": len(agents),
            "pipelines": len(pipelines),
        },
        "workspace": _workspace_prefs(state),
        "world": _world_switches(state),
        "display": {"stored_in": "this browser", "keys": list(DISPLAY_KEYS)},
        "notes": list(NOTES),
    }


def audit_read(state, client: str, every: float = 120.0) -> None:
    """Sample the read into the audit trail; a missing trail must not break the page."""
    from . import securitycenter

    try:
        securitycenter.audit_read(state, "settings.summary", client or "unknown", every=every)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
