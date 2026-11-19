"""Operator policy for document automation (``docs_auto.yaml``)."""
from __future__ import annotations

import re
from typing import Any

from .. import policyfile

NAME = "docs_auto.yaml"
TYPES = ("txt", "md", "pdf", "docx")
UNSORTED = "unsorted"
DEFAULT_CATEGORIES: dict[str, dict[str, Any]] = {
    "invoice": {"keywords": ["invoice", "bill to", "amount due", "invoice no", "tax invoice", "চালান"],
                "folder": "invoices/{year}"},
    "receipt": {"keywords": ["receipt", "payment received", "paid in full", "thank you for your purchase", "রসিদ"],
                "folder": "receipts/{year}"},
    "contract": {"keywords": ["agreement", "contract", "terms and conditions", "hereinafter", "the parties", "চুক্তি"],
                 "folder": "contracts"},
    "statement": {"keywords": ["bank statement", "account statement", "opening balance", "closing balance",
                               "statement period"], "folder": "statements/{year}"},
    "letter": {"keywords": ["dear", "sincerely", "yours faithfully", "kind regards", "subject:"],
               "folder": "letters/{year}"},
    "report": {"keywords": ["report", "executive summary", "findings", "conclusion", "recommendations"],
               "folder": "reports/{year}"},
}
DEFAULTS = {
    "schema": 1,
    "enabled": False,
    "encrypt_at_rest": True,
    "inbox": "inbox",
    "archive": "documents",
    "types": ["txt", "md", "pdf", "docx"],
    "naming": "{date}_{category}_{ref}",
    "dates": {"day_first": True},
    "limits": {"max_files_per_scan": 50, "max_file_bytes": 5_000_000, "max_text_chars": 200_000, "max_pdf_pages": 50,
               "max_records": 5000},
    "classify": {"mode": "rules", "agent": "", "allow_cloud": False, "max_tokens_per_call": 300, "min_score": 2},
    "categories": {},
    "templates": {"folder": "doc_templates", "output": "documents/generated"},
}
_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,60}")
_NAME = re.compile(r"[a-z][a-z0-9_]{1,24}")
_FOLDER_VARS = ("year", "month", "category")
_NAMING_VARS = ("date", "category", "ref", "name")


class DocsPolicyError(policyfile.PolicyError):
    """docs_auto.yaml is invalid or document automation is OFF."""


def _fail(message: str) -> None:
    raise DocsPolicyError(f"docs_auto.yaml: {message}")


def _folder(value: str, where: str, variables: tuple[str, ...] = ()) -> str:
    """A plain workspace-relative folder: at most 5 parts, no '..', no leading dot, only listed ``{variables}``."""
    parts = value.strip().strip("/").split("/")
    if not value.strip() or len(parts) > 5 or value.startswith(("/", "~")) or "\\" in value:
        _fail(f"'{where}' must be a relative folder of at most 5 levels")
    for part in parts:
        literal = re.sub(r"\{(" + "|".join(variables) + r")\}", "x", part) if variables else part
        if not _PART.fullmatch(literal) or literal.endswith((".", " ")):
            _fail(f"'{where}' has an unusable part '{part[:40]}'"
                  + (f" (variables: {' '.join('{' + name + '}' for name in variables)})" if variables else ""))
    return "/".join(parts)


def _inside(inner: str, outer: str) -> bool:
    return inner.casefold() == outer.casefold() or inner.casefold().startswith(outer.casefold() + "/")


def _validate(cfg: dict[str, Any]) -> None:
    cfg["inbox"] = _folder(cfg["inbox"], "inbox")
    cfg["archive"] = _folder(cfg["archive"], "archive")
    cfg["templates"]["folder"] = _folder(cfg["templates"]["folder"], "templates.folder")
    cfg["templates"]["output"] = _folder(cfg["templates"]["output"], "templates.output")
    if _inside(cfg["inbox"], cfg["archive"]) or _inside(cfg["archive"], cfg["inbox"]):
        _fail("'inbox' and 'archive' must be separate folders (neither inside the other)")
    for key in ("folder", "output"):
        if _inside(cfg["templates"][key], cfg["inbox"]) or _inside(cfg["inbox"], cfg["templates"][key]):
            _fail(f"'templates.{key}' and 'inbox' must be separate folders")
    if not cfg["types"]:
        _fail("'types' must list at least one file type")
    literal = re.sub(r"\{(" + "|".join(_NAMING_VARS) + r")\}", "x", cfg["naming"])
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", literal) or not re.search(r"\{(ref|name)\}", cfg["naming"]):
        _fail("'naming' uses letters, digits, . _ - and the variables {date} {category} {ref} {name}; "
              "it must contain {ref} or {name}")
    categories = cfg["categories"]
    if len(categories) > 24:
        _fail("at most 24 categories")
    clean: dict[str, dict[str, Any]] = {}
    for name, raw in categories.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name == UNSORTED:
            _fail("category names use a-z, 0-9 and _ (2-25 characters); 'unsorted' is reserved")
        if not isinstance(raw, dict) or set(raw) - {"keywords", "folder"}:
            _fail(f"category '{name}' takes 'keywords' and 'folder' only")
        keywords = raw.get("keywords")
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 40:
            _fail(f"category '{name}' needs 1-40 keywords")
        words = []
        for word in keywords:
            text = policyfile.clean_text(word, 60, label=f"docs_auto.yaml: category '{name}' keyword",
                                         error=DocsPolicyError).strip().casefold()
            if len(text) < 2:
                _fail(f"category '{name}' has a keyword shorter than 2 characters")
            words.append(text)
        folder = raw.get("folder", name)
        if not isinstance(folder, str):
            _fail(f"category '{name}': 'folder' must be text")
        clean[name] = {"keywords": words, "folder": _folder(folder, f"categories.{name}.folder", _FOLDER_VARS)}
    cfg["categories"] = clean


POLICY = policyfile.Policy(
    NAME, DEFAULTS, label="Document automation", error=DocsPolicyError, validate=_validate, free=("categories",),
    ranges={"limits.max_files_per_scan": (1, 500), "limits.max_file_bytes": (1000, 50_000_000),
            "limits.max_text_chars": (1000, 2_000_000), "limits.max_pdf_pages": (1, 500),
            "limits.max_records": (10, 20_000), "classify.max_tokens_per_call": (100, 2000),
            "classify.min_score": (1, 20)},
    choices={"types": TYPES, "classify.mode": ("rules", "model")},
    list_limits={"types": 4}, limit=64_000,
)
load = POLICY.load
require = POLICY.require
enabled = POLICY.enabled


def categories(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The operator's categories, or the built-in ones when none are configured."""
    return cfg["categories"] or DEFAULT_CATEGORIES
