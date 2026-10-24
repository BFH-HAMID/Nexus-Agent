"""API keys, straight from the terminal - no ``.env`` file required.

Resolution order, first hit wins:

1. **environment variable** - ``GEMINI_API_KEY``, ``DEEPSEEK_API_KEY``,
   ``OPENROUTER_API_KEY`` (exported in your shell, or set by CI);
2. **the key store** - written by ``nexus keys set <provider>`` into
   ``~/.config/nexus/keys.json`` (chmod 600, outside any repository, git-ignored
   by construction);
3. an optional legacy ``.env`` file, if one already exists (still supported,
   never required).

The store is a tiny JSON document::

    {"version": 1, "keys": {"gemini": "AIza...", "deepseek": "sk-..."}}

Nothing in this module imports the rest of Nexus, so both :mod:`nexus.config`
and :mod:`nexus.providers` can use it without circular imports.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

STORE_VERSION = 1


@dataclass(frozen=True)
class ProviderSpec:
    """Everything needed to *use* and *document* one hosted provider."""

    family: str
    label: str
    env: tuple[str, ...]
    base_url: str
    console_url: str
    default_model: str
    cli_name: str = ""

    @property
    def command(self) -> str:
        return f"nexus keys set {self.cli_name or self.family}"


# The three supported API providers. All of them expose the OpenAI
# chat-completions dialect, so one adapter drives all three.
PROVIDERS: dict[str, ProviderSpec] = {
    "gemini": ProviderSpec(
        family="gemini",
        label="Google Gemini",
        env=("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        console_url="https://aistudio.google.com/apikey",
        default_model="gemini-3.8-flash",
    ),
    "deepseek": ProviderSpec(
        family="deepseek",
        label="DeepSeek",
        env=("DEEPSEEK_API_KEY",),
        base_url="https://api.deepseek.com/v1",
        console_url="https://platform.deepseek.com/api_keys",
        default_model="deepseek-flash",
    ),
    "openrouter": ProviderSpec(
        family="openrouter",
        label="OpenRouter",
        env=("OPENROUTER_API_KEY",),
        base_url="https://openrouter.ai/api/v1",
        console_url="https://openrouter.ai/keys",
        default_model="openrouter/auto",
    ),
}

# Names people (and agents.yaml) actually type.
ALIASES: dict[str, str] = {
    "gemini": "gemini",
    "google": "gemini",
    "google-gemini": "gemini",
    "googleai": "gemini",
    "google-ai": "gemini",
    "google-genai": "gemini",
    "deepseek": "deepseek",
    "deep-seek": "deepseek",
    "openrouter": "openrouter",
    "open-router": "openrouter",
    "or": "openrouter",
}

# Non-primary families still resolve their env var so an existing setup keeps
# working; they are not offered by `nexus keys set`.
EXTRA_ENV: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
}

_ENV_TO_FAMILY: dict[str, str] = {}
for _family, _spec in PROVIDERS.items():
    for _name in _spec.env:
        _ENV_TO_FAMILY[_name] = _family
for _family, _names in EXTRA_ENV.items():
    for _name in _names:
        _ENV_TO_FAMILY.setdefault(_name, _family)


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #


def nexus_home() -> Path:
    """``$NEXUS_HOME`` or ``~/.config/nexus`` (same rule as the config loader)."""
    raw = os.environ.get("NEXUS_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".config" / "nexus"


def store_path() -> Path:
    """Where ``nexus keys set`` writes. Override with ``NEXUS_KEYS_FILE``."""
    raw = os.environ.get("NEXUS_KEYS_FILE")
    return Path(raw).expanduser() if raw else nexus_home() / "keys.json"


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:  # pragma: no cover - Windows / exotic filesystems
        pass


def load_store() -> dict[str, str]:
    """Read the key store. Missing/corrupt files read as empty (never crash)."""
    path = store_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, dict):
        return {}
    return {str(k): str(v) for k, v in keys.items() if isinstance(v, str) and v.strip()}


def _write_store(keys: dict[str, str]) -> Path:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod(path.parent, 0o700)
    payload = json.dumps({"version": STORE_VERSION, "keys": keys}, indent=2, sort_keys=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    _chmod(tmp, 0o600)
    os.replace(tmp, path)
    _chmod(path, 0o600)
    return path


def save_key(family: str, key: str) -> Path:
    """Persist one key (masked in logs/UI) into the store, mode 600."""
    clean = canonical(family)
    value = normalize_key(key)
    if not clean:
        raise KeyError(f"unknown provider '{family}' (known: {', '.join(PROVIDERS)})")
    if not value:
        raise ValueError("empty API key")
    keys = load_store()
    keys[clean] = value
    return _write_store(keys)


def delete_key(family: str) -> bool:
    clean = canonical(family) or family
    keys = load_store()
    if clean not in keys:
        return False
    keys.pop(clean)
    if keys:
        _write_store(keys)
        return True
    path = store_path()
    try:
        path.unlink()
    except OSError:
        pass
    return True


# --------------------------------------------------------------------------- #
# lookup
# --------------------------------------------------------------------------- #


def canonical(name: str | None) -> str | None:
    """``Google`` / ``open-router`` / ``GOOGLE_API_KEY`` -> ``gemini`` ..."""
    if not name:
        return None
    raw = str(name).strip().lower()
    if raw in ALIASES:
        return ALIASES[raw]
    if raw in _ENV_TO_FAMILY:
        return _ENV_TO_FAMILY[raw]
    stripped = raw.removesuffix("_api_key").removesuffix("-api-key")
    if stripped in ALIASES:
        return ALIASES[stripped]
    return None


def family_for(provider: str | None, api_key_env: str | None = None) -> str | None:
    """Best-effort family for an ``agents.yaml`` entry."""
    return canonical(provider) or canonical(api_key_env)


def provider_spec(provider: str | None) -> ProviderSpec | None:
    family = canonical(provider)
    return PROVIDERS.get(family) if family else None


def env_names(provider: str | None, api_key_env: str | None = None) -> tuple[str, ...]:
    """Every env var that can carry the key, most specific first."""
    names: list[str] = []
    if api_key_env:
        names.append(api_key_env)
    family = canonical(provider) or canonical(api_key_env)
    if family in PROVIDERS:
        names.extend(PROVIDERS[family].env)
    elif family in EXTRA_ENV:
        names.extend(EXTRA_ENV[family])
    seen: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


def key_from_env(provider: str | None, api_key_env: str | None = None) -> tuple[str, str]:
    """``(key, "env:NAME")`` for the first env var that is set."""
    for name in env_names(provider, api_key_env):
        value = os.environ.get(name, "").strip()
        if value:
            return value, f"env:{name}"
    return "", ""


def stored_key(family: str | None) -> str:
    clean = canonical(family) if family else None
    if not clean:
        return ""
    return load_store().get(clean, "")


def get_key(family: str) -> str:
    """Key for a family from the environment first, then the store."""
    clean = canonical(family) or str(family or "").strip().lower()
    if not clean:
        return ""
    key, _ = key_from_env(clean)
    return key or stored_key(clean)


def key_source(provider: str | None, api_key_env: str | None = None) -> str:
    """Human readable origin of an agent's key: ``env:NAME``, ``keys.json`` or ``""``."""
    key, origin = key_from_env(provider, api_key_env)
    if key:
        return origin
    family = family_for(provider, api_key_env)
    if family and stored_key(family):
        return str(store_path())
    return ""


def any_key_available(families: list[str] | tuple[str, ...] = ()) -> bool:
    targets = [canonical(f) for f in (families or tuple(PROVIDERS)) if canonical(f)]
    return any(get_key(f) for f in targets)


def available_families() -> list[str]:
    return [f for f in PROVIDERS if get_key(f)]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def normalize_key(value: str | None) -> str:
    key = (value or "").strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in {"'", '"'}:
        key = key[1:-1].strip()
    if key.lower().startswith(("bearer ", "authorization:")):
        key = key.split(None, 1)[-1].strip()
    return key


def mask(key: str | None) -> str:
    """``AIzaSy...9fQ`` - enough to recognise a key, useless to an attacker."""
    value = (key or "").strip()
    if not value:
        return ""
    if len(value) <= 10:
        return value[:2] + "…" + value[-2:]
    return f"{value[:6]}…{value[-4:]}"


def looks_like_key(family: str | None, key: str) -> str:
    """Return a warning string ('' when the key looks plausible)."""
    clean = canonical(family) or ""
    value = normalize_key(key)
    if len(value) < 16:
        return "that key looks too short - double check you pasted all of it"
    prefixes = {"openrouter": ("sk-or-",), "deepseek": ("sk-",), "gemini": ("AIza", "AQ.")}
    expected = prefixes.get(clean, ())
    if expected and not value.startswith(expected):
        return f"'{clean}' keys usually start with {expected[0]} - saving anyway"
    return ""


def read_secret(prompt: str) -> str:
    """Read one secret from the terminal (hidden) or from a pipe."""
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        return normalize_key(line)
    try:
        import getpass

        return normalize_key(getpass.getpass(prompt))
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    except Exception:  # noqa: BLE001 - getpass can fail without a tty
        try:
            return normalize_key(input(prompt))
        except (EOFError, KeyboardInterrupt):
            return ""


def describe(family: str) -> str:
    spec = PROVIDERS.get(family)
    return spec.label if spec else family


def store_is_private() -> bool:
    """True when the store is (mis)missing or only readable by the owner."""
    path = store_path()
    if not path.exists():
        return True
    if os.name != "posix":  # pragma: no cover - Windows ACLs are different
        return True
    mode = path.stat().st_mode
    return not (mode & (stat.S_IRWXG | stat.S_IRWXO))
