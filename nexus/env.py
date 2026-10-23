"""`.env` loading.

``python-dotenv`` is used when available; otherwise a tiny built-in parser
handles the common ``KEY=value`` / ``export KEY="value"`` cases so the CLI
never hard-depends on it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(path: Path, *, override: bool = False) -> list[str]:
    """Load one .env file. Returns the keys that were set."""
    if not path.is_file():
        return []
    try:
        values = parse_env_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
    loaded: list[str] = []
    for key, value in values.items():
        if not value:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def load_dotenv_files(workspace: Path | None = None, *, override: bool = False) -> list[str]:
    """Load ./.env, <workspace>/.env and ~/.config/nexus/.env (first wins)."""
    ws = Path(workspace or Path.cwd()).resolve()
    home = Path(os.environ.get("NEXUS_HOME") or (Path.home() / ".config" / "nexus"))
    loaded: list[str] = []
    seen: set[str] = set()
    for path in (ws / ".env", Path.cwd() / ".env", home / ".env"):
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        for name in load_env_file(path, override=override):
            if name not in loaded:
                loaded.append(name)
    if not loaded:
        try:
            from dotenv import load_dotenv  # type: ignore

            load_dotenv(ws / ".env", override=override)
        except Exception:  # noqa: BLE001
            pass
    return loaded
