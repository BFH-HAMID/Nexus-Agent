"""Task 27 - translator (opt-in, ``translate.yaml``).

Offline language detection, a fixed glossary, protected code/links/placeholders
and one strict-JSON model call per translation.  See ``docs/translate.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import policy


def status(workspace: Path | str) -> dict[str, Any]:
    """Never-raising summary for ``nexus translate status``, ``doctor`` and the HUD."""
    info = policy.POLICY.describe(workspace)
    if not info.get("enabled"):
        return info
    try:
        from .core import Translator

        cfg = policy.load(workspace)
        info.update(agent=cfg["agent"] or "(default)", allow_cloud=cfg["allow_cloud"], default_target=cfg["default_target"],
                    targets=cfg["targets"] or "any", glossary_terms=sum(len(terms) for terms in cfg["glossary"].values()),
                    interpreter=cfg["interpreter"]["enabled"])
        mode = Translator(workspace).interpreter()
        info["interpreting"] = "-".join(mode["pair"]) if mode else None
    except (ValueError, OSError) as exc:
        info["error"] = str(exc)[:240]
    return info
