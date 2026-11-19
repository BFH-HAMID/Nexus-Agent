"""Turn a model reply into validated file changes for the isolated checkout.

Nothing here writes. Every rule is deterministic: path shape, private and
operator-owned files, ``patch.allow`` / ``patch.protect``, size, text-only
content and the credential scan that ``write_file`` also applies.
"""
from __future__ import annotations

import fnmatch
import re
import stat
from pathlib import Path
from typing import Any

_BAD_PATH = re.compile(r"[\x00-\x1f\x7f\\:*?\"<>|]")
_TEST_PATH = re.compile(r"(?:^|/)(?:tests?|__tests__|specs?)/|(?:^|/)(?:test_[^/]+\.py|conftest\.py|[^/]+_test\.(?:py|go)"
                        r"|[^/]+\.(?:test|spec)\.[cm]?[jt]sx?)$", re.I)
# control characters, a carriage return that is not part of a line ending, and the "Trojan Source" direction controls
_SNEAKY = re.compile("[\x00-\x08\x0b\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]|\r(?!\n)")
MARKER = "[REDACTED"


class PatchError(ValueError):
    """The proposed patch breaks a rule. The message is safe to show to the model."""


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def clean_path(raw: Any) -> str:
    if (not isinstance(raw, str) or not 1 <= len(raw) <= 240 or _BAD_PATH.search(raw) or raw.startswith(("/", "~"))
            or not raw.isprintable()):
        raise PatchError("File paths are repository-relative, use '/' and contain no special characters")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} or part != part.strip() or part.endswith(".") for part in parts):
        raise PatchError(f"{raw}: not a plain repository-relative path")
    return "/".join(parts)


def matches(path: str, patterns: list[str]) -> bool:
    """``dir/**`` = everything below dir; a pattern without '/' also matches the file name at any depth."""
    lowered = path.casefold()
    name = lowered.rsplit("/", 1)[-1]
    for raw in patterns:
        pattern = raw.casefold()
        if pattern.endswith("/**") and (lowered == pattern[:-3] or lowered.startswith(pattern[:-2])):
            return True
        if fnmatch.fnmatchcase(lowered, pattern) or ("/" not in pattern and fnmatch.fnmatchcase(name, pattern)):
            return True
    return False


def locate(workspace: Path, tree: Path, rel: str, cfg: dict[str, Any], *, write: bool) -> Path:
    """The checkout path of ``rel`` once every path rule has passed."""
    from ..permissions import Permissions
    from ..workbench import safe_path

    try:
        target = safe_path(tree, rel)
    except ValueError:
        raise PatchError(f"{rel}: private, runtime and out-of-repository paths are off limits") from None
    if target != tree.resolve() / rel:
        raise PatchError(f"{rel}: the path goes through a symbolic link")
    if write:
        if matches(rel, cfg["patch"]["protect"]):
            raise PatchError(f"{rel}: protected by the operator (patch.protect)")
        if cfg["patch"]["allow"] and not matches(rel, cfg["patch"]["allow"]):
            raise PatchError(f"{rel}: outside the paths the operator allows (patch.allow)")
        try:
            controlled = Permissions(workspace).operator_control(workspace / rel)
        except (ValueError, OSError):
            controlled = True  # protections that cannot be checked are assumed to apply
        if controlled:
            raise PatchError(f"{rel}: operator-owned policy files are never editable")
    return target


def read_text(target: Path, rel: str, limit: int) -> bytes:
    """Bytes of an existing regular UTF-8 text file in the checkout."""
    try:
        info = target.lstat()
    except OSError:
        raise PatchError(f"{rel}: no such file") from None
    if not stat.S_ISREG(info.st_mode):
        raise PatchError(f"{rel}: not a regular file")
    if info.st_size > limit:
        raise PatchError(f"{rel}: larger than the {limit} byte limit")
    data = target.read_bytes()
    if b"\x00" in data:
        raise PatchError(f"{rel}: binary file")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise PatchError(f"{rel}: not UTF-8 text") from None
    return data


def has_secret(text: str, workspace: Path) -> bool:
    from ..github_safety import scan_text

    try:
        scan_text(text, workspace=workspace)
    except ValueError:
        return True
    return False


def _crlf(data: bytes | None) -> bool:
    return bool(data) and data.count(b"\r\n") == data.count(b"\n") > 0


def _encode(text: str, before: bytes | None) -> bytes:
    text = text.replace("\r\n", "\n")
    if text and not text.endswith("\n") and (before is None or before == b"" or before.endswith(b"\n")):
        text += "\n"  # models routinely drop the final newline of a file
    return text.replace("\n", "\r\n").encode("utf-8") if _crlf(before) else text.encode("utf-8")


def _edit(rel: str, before: bytes, edits: Any) -> bytes:
    if not isinstance(edits, list) or not 1 <= len(edits) <= 40:
        raise PatchError(f"{rel}: 'edits' must be a list of 1-40 {{find, replace}} objects")
    text = before.decode("utf-8").replace("\r\n", "\n") if _crlf(before) else before.decode("utf-8")
    for index, edit in enumerate(edits, 1):
        find = edit.get("find") if isinstance(edit, dict) else None
        replace = edit.get("replace") if isinstance(edit, dict) else None
        if not isinstance(find, str) or not find or not isinstance(replace, str):
            raise PatchError(f"{rel}: edit {index} needs a non-empty 'find' and a 'replace' string")
        find, replace = find.replace("\r\n", "\n"), replace.replace("\r\n", "\n")
        count = text.count(find)
        if count != 1:
            raise PatchError(f"{rel}: edit {index}: the 'find' text occurs {count} times in the current file; it must "
                             "occur exactly once (copy more surrounding lines, or send the whole file with action 'write')")
        text = text.replace(find, replace, 1)
    return text.replace("\n", "\r\n").encode("utf-8") if _crlf(before) else text.encode("utf-8")


def build(reply: dict[str, Any], *, workspace: Path, tree: Path, cfg: dict[str, Any],
          recorded: dict[str, bytes | None] | None = None, tracked: set[str] | None = None,
          shown: set[str] | None = None) -> tuple[str, list[dict[str, Any]], list[str]]:
    """``(summary, changes, need_files)``. A change is ``{path, before, data}``; ``data`` None means delete.

    ``recorded`` is the content of files the run already changed (None = deleted)
    and wins over what is on disk. ``tracked`` are the paths of the base commit:
    other files that exist in the checkout were left there by the checks and are
    not editable. ``shown`` are the files in the model's current message: an
    existing file outside it cannot be changed.
    """
    rules = cfg["patch"]
    recorded = recorded or {}
    summary = "".join(ch for ch in " ".join(str(reply.get("summary") or "").split()) if ch.isprintable())[:300]
    need = []
    for item in (reply.get("need_files") if isinstance(reply.get("need_files"), list) else [])[:8]:
        try:
            need.append(clean_path(item))
        except PatchError:
            continue
    files = reply.get("files") or []
    if not isinstance(files, list) or any(not isinstance(entry, dict) for entry in files):
        raise PatchError("'files' must be a list of objects")
    if not files and not need:
        raise PatchError("The reply contains no file changes")
    if len(files) > rules["max_files"]:
        raise PatchError(f"At most {rules['max_files']} files per patch")
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in files:
        rel = clean_path(entry.get("path"))
        if rel.casefold() in seen:
            raise PatchError(f"{rel}: listed twice")
        seen.add(rel.casefold())
        target = locate(workspace, tree, rel, cfg, write=True)
        if rel in recorded:
            before = recorded[rel]
        elif target.exists() or target.is_symlink():
            if tracked is not None and rel not in tracked:
                raise PatchError(f"{rel}: not a file of the repository (the checks created it, or the name differs "
                                 "in letter case from a tracked file)")
            before = read_text(target, rel, rules["max_file_bytes"])
        else:
            before = None
        if before is not None and shown is not None and rel not in shown:
            raise PatchError(f"{rel}: its content is not in 'files' of this message, so you cannot change it. "
                             "Name it in need_files and change it in your next reply")
        if before is not None and has_secret(before.decode("utf-8"), workspace):
            raise PatchError(f"{rel}: holds credential-like text and is not editable by DevPilot")
        action = entry.get("action", "write")
        if action == "delete":
            if not rules["allow_delete"]:
                raise PatchError(f"{rel}: deleting files is not allowed (patch.allow_delete)")
            if before is None:
                raise PatchError(f"{rel}: cannot delete a file that does not exist")
            data = None
        elif action == "write":
            if not isinstance(entry.get("content"), str):
                raise PatchError(f"{rel}: action 'write' needs the complete new file in 'content'")
            if before is None and not rules["allow_new_files"]:
                raise PatchError(f"{rel}: creating files is not allowed (patch.allow_new_files)")
            if before is None and target.parent != tree.resolve() and target.parent.exists() and not target.parent.is_dir():
                raise PatchError(f"{rel}: its folder is a file")
            data = _encode(entry["content"], before)
        elif action == "edit":
            if before is None:
                raise PatchError(f"{rel}: cannot edit a file that does not exist; use action 'write'")
            data = _edit(rel, before, entry.get("edits"))
        else:
            raise PatchError(f"{rel}: unknown action '{str(action)[:20]}' (use write, edit or delete)")
        if data == before:
            continue
        if data is not None:
            text = data.decode("utf-8")
            if len(data) > rules["max_file_bytes"]:
                raise PatchError(f"{rel}: new content is larger than {rules['max_file_bytes']} bytes")
            if "\x00" in text:
                raise PatchError(f"{rel}: binary content is not allowed")
            previous = (before or b"").decode("utf-8")
            if text.count(MARKER) > previous.count(MARKER):
                raise PatchError(f"{rel}: the content contains a redaction placeholder; never copy '[REDACTED...]' into files")
            if len(_SNEAKY.findall(text)) > len(_SNEAKY.findall(previous)):
                raise PatchError(f"{rel}: the new content adds control or text-direction characters, which can make "
                                 "a diff misleading; write them as escape sequences")
            if has_secret(text, workspace):
                raise PatchError(f"{rel}: the new content looks like it contains a credential")
        changes.append({"path": rel, "before": before, "data": data})
    if files and not changes:
        raise PatchError("The patch changes nothing: every file already has that content")
    return summary, changes, need
