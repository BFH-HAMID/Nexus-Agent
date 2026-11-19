"""Task 28 - document automation (opt-in, ``docs_auto.yaml``).

Scans one inbox folder, classifies each document by keywords, reads the
obvious fields, proposes a place in the archive and moves the file only after
approval.  Also fills operator-written text templates.  See ``docs/docs-auto.md``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from . import policy


def _installed(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def status(workspace: Path | str) -> dict[str, Any]:
    """Never-raising summary for ``nexus docs-auto status``, ``doctor`` and the HUD."""
    info = policy.POLICY.describe(workspace)
    if not info.get("enabled"):
        return info
    try:
        from .core import DocsAuto

        cfg = policy.load(workspace)
        info.update(inbox=cfg["inbox"], archive=cfg["archive"], types=cfg["types"], classify=cfg["classify"]["mode"],
                    categories=list(policy.categories(cfg)))
        missing = [package for kind, module, package in (("pdf", "pypdf", "pypdf"), ("docx", "docx", "python-docx"))
                   if kind in cfg["types"] and not _installed(module)]
        if missing:
            info["missing"] = missing
        docs = DocsAuto(workspace)
        rows = docs.records()
        info.update(indexed=len(rows), filed=sum(row["status"] == "filed" for row in rows),
                    waiting=sum(row["status"] != "filed" for row in rows))
        problem = docs.index.problem()
        if problem:
            info["error"] = f"the index cannot be sealed: {problem}"[:240]
    except (ValueError, OSError) as exc:
        info["error"] = str(exc)[:240]
    return info
