"""Personal data integrations via the operator-owned MCP layer.

All integrations default to OFF and are scoped per capability in ``mcp.yaml``.
OAuth tokens and granted scopes live strictly in the OS keyring (never in
YAML, git, logs, or plaintext files). Fetched external content always flows
through ``nexus.untrusted`` before reaching the model or user output.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .extensions.catalog import Catalog
from .extensions.tools import redact_output
from .extensions.transport import LIMIT as MCP_LIMIT
from .extensions.transport import VERSION as MCP_VERSION
from .extensions.transport import Session
from .github_safety import scan_text
from .jarvis.sources import bounded
from .redaction import mask
from .types import Risk
from .untrusted import escape_delimiters, flag_names, wrap_mcp_result
from .untrusted import wrap as wrap_untrusted

_ROLE_TAG_RE = re.compile(r"</?\s*(?:system|assistant|developer|instruction|admin)\s*>|\[\s*(?:system|assistant|developer|instruction|admin)\s*\]", re.I)


def _sanitize_for_speech(text: str, *, max_len: int = 1200) -> str:
    cleaned = escape_delimiters(str(text or ""))
    cleaned = _ROLE_TAG_RE.sub("", cleaned)
    cleaned = "".join(ch if (ch >= " " or ch in "\n\t") else " " for ch in cleaned)
    return " ".join(cleaned.split())[:max_len].strip()

KEYRING_SERVICE = "nexus-integrations"
INTEGRATION_PROVIDERS = ("gmail", "calendar", "contacts", "drive", "docs")
ALL_INTEGRATION_SECTIONS = (*INTEGRATION_PROVIDERS, "notes")

_SERVER_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_INLINE_SECRET_KEYS = frozenset({
    "token", "access_token", "refresh_token", "client_secret", "api_key",
    "secret", "password", "pin", "authorization", "credentials",
})

# Minimal OAuth scopes per provider and canonical short aliases.
_SCOPE_ALIASES: dict[str, dict[str, str]] = {
    "gmail": {
        "gmail.readonly": "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.readonly": "https://www.googleapis.com/auth/gmail.readonly",
        "gmail.compose": "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.compose": "https://www.googleapis.com/auth/gmail.compose",
        "gmail.modify": "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.modify": "https://www.googleapis.com/auth/gmail.modify",
        "gmail.send": "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.send": "https://www.googleapis.com/auth/gmail.send",
        "gmail.labels": "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/gmail.labels": "https://www.googleapis.com/auth/gmail.labels",
    },
    "calendar": {
        "calendar.readonly": "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.readonly": "https://www.googleapis.com/auth/calendar.readonly",
        "calendar.events": "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.events": "https://www.googleapis.com/auth/calendar.events",
    },
    "contacts": {
        "contacts.readonly": "https://www.googleapis.com/auth/contacts.readonly",
        "https://www.googleapis.com/auth/contacts.readonly": "https://www.googleapis.com/auth/contacts.readonly",
    },
    "drive": {
        "drive.readonly": "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.readonly": "https://www.googleapis.com/auth/drive.readonly",
        "drive.metadata.readonly": "https://www.googleapis.com/auth/drive.metadata.readonly",
        "https://www.googleapis.com/auth/drive.metadata.readonly": "https://www.googleapis.com/auth/drive.metadata.readonly",
    },
    "docs": {
        "documents.readonly": "https://www.googleapis.com/auth/documents.readonly",
        "https://www.googleapis.com/auth/documents.readonly": "https://www.googleapis.com/auth/documents.readonly",
        "drive.readonly": "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.readonly": "https://www.googleapis.com/auth/drive.readonly",
    },
}

# Scopes that satisfy each operation (narrowest listed first).
_OPERATION_REQUIRED_SCOPES: dict[tuple[str, str], tuple[str, ...]] = {
    ("gmail", "search"): (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
    ),
    ("gmail", "read"): (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
    ),
    ("gmail", "summarize"): (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
    ),
    ("gmail", "draft"): (
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.modify",
    ),
    ("gmail", "label"): (
        "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/gmail.modify",
    ),
    ("gmail", "archive"): (
        "https://www.googleapis.com/auth/gmail.modify",
    ),
    ("gmail", "send"): (
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.modify",
    ),
    ("calendar", "read"): (
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
    ),
    ("calendar", "write"): (
        "https://www.googleapis.com/auth/calendar.events",
    ),
    ("calendar", "invites"): (
        "https://www.googleapis.com/auth/calendar.events",
    ),
    ("contacts", "read"): (
        "https://www.googleapis.com/auth/contacts.readonly",
    ),
    ("drive", "read"): (
        "https://www.googleapis.com/auth/drive.readonly",
    ),
    ("drive", "search"): (
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
    ),
    ("docs", "read"): (
        "https://www.googleapis.com/auth/documents.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ),
}


def default_integrations_policy() -> dict[str, Any]:
    return {
        "enabled": False,
        "gmail": {
            "enabled": False,
            "server": "",
            "scopes": [],
            "allow_search": False,
            "allow_read": False,
            "allow_draft": False,
            "allow_labels": False,
            "allow_archive": False,
            "allow_send": False,
            "max_results": 20,
        },
        "calendar": {
            "enabled": False,
            "server": "",
            "scopes": [],
            "allow_read": False,
            "allow_write": False,
            "allow_invites": False,
            "max_results": 25,
        },
        "contacts": {
            "enabled": False,
            "server": "",
            "scopes": [],
            "allow_read": False,
            "max_results": 20,
        },
        "drive": {
            "enabled": False,
            "server": "",
            "scopes": [],
            "allow_read": False,
            "max_results": 20,
        },
        "docs": {
            "enabled": False,
            "server": "",
            "scopes": [],
            "allow_read": False,
        },
        "notes": {
            "enabled": False,
            "allow_read": False,
            "obsidian_folders": [],
            "notion_folders": [],
            "max_results": 20,
        },
    }


def normalize_scope(provider: str, scope: str) -> str:
    if provider not in _SCOPE_ALIASES:
        raise ValueError(f"Unknown OAuth provider: {provider}")
    raw = str(scope or "").strip()
    if not raw or any(ch.isspace() for ch in raw):
        raise ValueError(f"Invalid OAuth scope for {provider}: {scope!r}")
    canonical = _SCOPE_ALIASES[provider].get(raw)
    if canonical is None:
        raise ValueError(
            f"Overly broad or unsupported scope {raw!r} for {provider}; "
            f"only minimal scoped permissions are allowed"
        )
    return canonical


def validate_minimal_scopes(provider: str, scopes: Any) -> list[str]:
    if not isinstance(scopes, list) or len(scopes) > 10:
        raise ValueError(f"{provider}.scopes must be a list of at most 10 minimal OAuth scopes")
    normalized: list[str] = []
    for item in scopes:
        if not isinstance(item, str):
            raise ValueError(f"{provider}.scopes entries must be strings")
        canon = normalize_scope(provider, item)
        if canon not in normalized:
            normalized.append(canon)
    return normalized


def _validate_note_folders(workspace: Path, raw_list: Any, field_name: str) -> list[str]:
    if not isinstance(raw_list, list) or len(raw_list) > 10:
        raise ValueError(f"notes.{field_name} must be a list of at most 10 folder paths")
    root = workspace.resolve()
    cleaned: list[str] = []
    for item in raw_list:
        if not isinstance(item, str) or not item.strip() or len(item) > 400 or "\x00" in item:
            raise ValueError(f"notes.{field_name} contains an invalid path")
        raw = item.strip()
        candidate = Path(raw).expanduser()
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        if resolved.is_symlink():
            raise ValueError(f"notes.{field_name} cannot target a symlink")
        if not candidate.is_absolute() and not resolved.is_relative_to(root):
            raise ValueError(f"notes.{field_name} relative path escapes workspace")
        scan_text(str(resolved), workspace=root)
        cleaned.append(str(resolved) if candidate.is_absolute() else str(resolved.relative_to(root)))
    return cleaned


def validate_integrations_policy(value: Any, workspace: Path | str) -> dict[str, Any]:
    defaults = default_integrations_policy()
    if value is None:
        return defaults
    if not isinstance(value, dict):
        raise ValueError("mcp.yaml integrations must be a mapping")
    unknown = set(value) - set(defaults)
    if unknown:
        raise ValueError(f"Unknown integrations policy key(s): {', '.join(sorted(unknown))}")
    if _INLINE_SECRET_KEYS & {str(k).casefold() for k in value}:
        raise ValueError("Inline credentials are forbidden in mcp.yaml integrations")
    root = Path(workspace).resolve()
    result: dict[str, Any] = {"enabled": defaults["enabled"]}
    enabled_top = value.get("enabled", False)
    if type(enabled_top) is not bool:
        raise ValueError("integrations.enabled must be true or false")
    result["enabled"] = enabled_top

    for section in ALL_INTEGRATION_SECTIONS:
        sec_default = dict(defaults[section])
        sec_raw = value.get(section)
        if sec_raw is None:
            result[section] = sec_default
            continue
        if not isinstance(sec_raw, dict):
            raise ValueError(f"integrations.{section} must be a mapping")
        if _INLINE_SECRET_KEYS & {str(k).casefold() for k in sec_raw}:
            raise ValueError(f"Inline credentials are forbidden in integrations.{section}")
        sec_unknown = set(sec_raw) - set(sec_default)
        if sec_unknown:
            raise ValueError(f"Unknown integrations.{section} field(s): {', '.join(sorted(sec_unknown))}")
        merged = {**sec_default, **sec_raw}
        for bool_key, val in merged.items():
            if bool_key == "enabled" or bool_key.startswith("allow_"):
                if type(val) is not bool:
                    raise ValueError(f"integrations.{section}.{bool_key} must be true or false")
        if "max_results" in merged:
            mr = merged["max_results"]
            if type(mr) is not int or not 1 <= mr <= 50:
                raise ValueError(f"integrations.{section}.max_results must be between 1 and 50")
        if section in INTEGRATION_PROVIDERS:
            server_name = merged.get("server", "")
            if not isinstance(server_name, str) or (server_name and not _SERVER_IDENT_RE.fullmatch(server_name)):
                raise ValueError(f"integrations.{section}.server must be a valid MCP server identifier")
            merged["scopes"] = validate_minimal_scopes(section, merged.get("scopes", []))
        else:
            merged["obsidian_folders"] = _validate_note_folders(root, merged.get("obsidian_folders", []), "obsidian_folders")
            merged["notion_folders"] = _validate_note_folders(root, merged.get("notion_folders", []), "notion_folders")
        result[section] = merged
    return result


def load_integrations_policy(workspace: Path | str) -> dict[str, Any]:
    root = Path(workspace).resolve()
    path = root / "mcp.yaml"
    if not path.exists():
        return default_integrations_policy()
    try:
        raw = yaml.safe_load(bounded(path, 64_000)) or {}
    except yaml.YAMLError:
        raise ValueError("Invalid MCP YAML") from None
    if not isinstance(raw, dict):
        raise ValueError("Invalid MCP policy")
    return validate_integrations_policy(raw.get("integrations"), root)


# ── Keyring-only OAuth token store ──────────────────────────────────────── #


def _load_keyring_module(keyring_module: Any = None) -> Any:
    if keyring_module is not None:
        return keyring_module
    try:
        import keyring
        return keyring
    except ImportError as exc:
        raise ImportError(
            "Personal integrations OAuth storage requires keyring (`pip install -e '.[mcp]'`)."
        ) from exc


class OAuthTokenVault:
    """Stores OAuth tokens & granted scopes exclusively in the OS keyring."""

    def __init__(self, workspace: Path | str, *, keyring_module: Any = None, service: str = KEYRING_SERVICE) -> None:
        self.workspace = Path(workspace).resolve()
        self.service = service
        self._keyring_override = keyring_module
        self._ws_hash = hashlib.sha256(str(self.workspace).encode("utf-8")).hexdigest()[:16]

    def _kr(self) -> Any:
        return _load_keyring_module(self._keyring_override)

    def _key(self, provider: str) -> str:
        return f"oauth:{self._ws_hash}:{provider}"

    def _index_key(self) -> str:
        return f"oauth-index:{self._ws_hash}"

    def _read_index(self) -> list[str]:
        raw = self._kr().get_password(self.service, self._index_key())
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [str(x) for x in data if isinstance(x, str) and x in INTEGRATION_PROVIDERS]

    def _write_index(self, providers: list[str]) -> None:
        clean = sorted({p for p in providers if p in INTEGRATION_PROVIDERS})
        if clean:
            self._kr().set_password(self.service, self._index_key(), json.dumps(clean))
        else:
            try:
                self._kr().delete_password(self.service, self._index_key())
            except Exception:
                pass

    def grant(
        self,
        provider: str,
        access_token: str,
        *,
        scopes: list[str],
        refresh_token: str = "",
        expires_at: float | None = None,
    ) -> dict[str, Any]:
        if provider not in INTEGRATION_PROVIDERS:
            raise ValueError(f"Unsupported OAuth integration provider: {provider}")
        token = str(access_token or "")
        if not token or len(token) > 4096 or "\n" in token or "\r" in token or "\x00" in token:
            raise ValueError("Invalid OAuth access token")
        rtok = str(refresh_token or "")
        if rtok and (len(rtok) > 4096 or "\n" in rtok or "\r" in rtok or "\x00" in rtok):
            raise ValueError("Invalid OAuth refresh token")
        canon_scopes = validate_minimal_scopes(provider, scopes)
        if not canon_scopes:
            raise ValueError("At least one minimal OAuth scope is required")
        record = {
            "provider": provider,
            "access_token": token,
            "refresh_token": rtok,
            "scopes": canon_scopes,
            "granted_at": round(time.time(), 3),
            "expires_at": float(expires_at) if expires_at is not None else None,
        }
        self._kr().set_password(self.service, self._key(provider), json.dumps(record))
        idx = self._read_index()
        if provider not in idx:
            idx.append(provider)
            self._write_index(idx)
        return {
            "provider": provider,
            "token_stored": True,
            "granted_scopes": canon_scopes,
            "granted_at": record["granted_at"],
        }

    def get_record(self, provider: str) -> dict[str, Any] | None:
        if provider not in INTEGRATION_PROVIDERS:
            raise ValueError(f"Unsupported OAuth integration provider: {provider}")
        raw = self._kr().get_password(self.service, self._key(provider))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or not data.get("access_token"):
            return None
        return data

    def get_token(self, provider: str) -> str | None:
        rec = self.get_record(provider)
        return str(rec["access_token"]) if rec else None

    def granted_scopes(self, provider: str) -> list[str]:
        rec = self.get_record(provider)
        if not rec:
            return []
        scopes = rec.get("scopes") or []
        return [str(s) for s in scopes if isinstance(s, str)]

    def revoke(self, provider: str) -> dict[str, Any]:
        if provider == "all":
            targets = sorted(set(self._read_index()) | set(INTEGRATION_PROVIDERS))
            revoked: list[str] = []
            for item in targets:
                if self._delete_single(item):
                    revoked.append(item)
            self._write_index([])
            return {"revoked": revoked, "all": True, "granted_scopes": []}
        if provider not in INTEGRATION_PROVIDERS:
            raise ValueError(f"Unsupported OAuth integration provider: {provider}")
        existed = self._delete_single(provider)
        idx = [p for p in self._read_index() if p != provider]
        self._write_index(idx)
        return {"provider": provider, "revoked": existed, "granted_scopes": []}

    def _delete_single(self, provider: str) -> bool:
        existed = self.get_record(provider) is not None
        try:
            self._kr().delete_password(self.service, self._key(provider))
        except Exception:
            if existed:
                # Fallback overwrite empty/clear if delete unsupported by test stub
                try:
                    self._kr().set_password(self.service, self._key(provider), "")
                except Exception:
                    pass
        return existed

    def secret_values(self) -> list[str]:
        values: list[str] = []
        for provider in INTEGRATION_PROVIDERS:
            try:
                rec = self.get_record(provider)
            except Exception:
                continue
            if rec:
                for k in ("access_token", "refresh_token"):
                    val = rec.get(k)
                    if isinstance(val, str) and val:
                        values.append(val)
        return values

    def status_summary(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for provider in INTEGRATION_PROVIDERS:
            try:
                rec = self.get_record(provider)
            except ImportError as exc:
                out[provider] = {"token_stored": False, "granted_scopes": [], "keyring_error": str(exc)}
                continue
            if rec:
                out[provider] = {
                    "token_stored": True,
                    "granted_scopes": list(rec.get("scopes") or []),
                    "granted_at": rec.get("granted_at"),
                    "expires_at": rec.get("expires_at"),
                }
            else:
                out[provider] = {"token_stored": False, "granted_scopes": []}
        return out


# ── Bounded MCP execution & scope/policy guards ─────────────────────────── #


def _untrusted_clean_label(value: Any, max_len: int = 240) -> str:
    return _sanitize_for_speech(str(value or "").replace("\r", " ").replace("\n", " "), max_len=max_len).strip()


def _parse_mcp_json_payload(result: Any) -> Any:
    """Extract structured JSON or text from an MCP tools/call result dict."""
    if isinstance(result, dict):
        if "structuredContent" in result and result["structuredContent"] is not None:
            return result["structuredContent"]
        content = result.get("content")
        if isinstance(content, list):
            texts = [
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
            ]
            if len(texts) == 1:
                raw = texts[0].strip()
                if raw.startswith("{") or raw.startswith("["):
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return raw
                return raw
            if texts:
                joined = "\n".join(texts)
                try:
                    return json.loads(joined)
                except json.JSONDecodeError:
                    return joined
    return result


class IntegrationError(ValueError):
    """Raised when an integration policy, scope, approval, or MCP check fails."""


class PersonalIntegrations:
    """High-level scoped MCP client for Gmail, Calendar, Contacts, Drive, Docs, and Notes."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        keyring_module: Any = None,
        policy: dict[str, Any] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.keyring_module = keyring_module
        self.vault = OAuthTokenVault(self.workspace, keyring_module=keyring_module)
        self._policy_override = policy

    def policy(self) -> dict[str, Any]:
        if self._policy_override is not None:
            return validate_integrations_policy(self._policy_override, self.workspace)
        return load_integrations_policy(self.workspace)

    def _require_enabled(self, provider: str, capability: str) -> tuple[dict[str, Any], dict[str, Any]]:
        pol = self.policy()
        if not pol["enabled"]:
            raise IntegrationError("Personal integrations are disabled in mcp.yaml (integrations.enabled: false)")
        sec = pol[provider]
        if not sec.get("enabled"):
            raise IntegrationError(f"{provider} integration is disabled in mcp.yaml")
        if capability and not sec.get(f"allow_{capability}", False):
            raise IntegrationError(f"{provider} capability '{capability}' is disabled in mcp.yaml")
        return pol, sec

    def _require_scopes_and_token(self, provider: str, operation: str, sec: dict[str, Any]) -> tuple[str, list[str]]:
        configured = validate_minimal_scopes(provider, sec.get("scopes", []))
        granted = self.vault.granted_scopes(provider)
        token = self.vault.get_token(provider)
        if not token:
            raise IntegrationError(
                f"No OAuth token in keyring for {provider}. Store a minimal-scope token via `nexus integrations grant {provider}`."
            )
        allowed_required = _OPERATION_REQUIRED_SCOPES[(provider, operation)]
        if not any(req in configured for req in allowed_required):
            raise IntegrationError(
                f"mcp.yaml does not configure a minimal required scope for {provider}.{operation} ({allowed_required[0]})"
            )
        if not any(req in granted for req in allowed_required):
            raise IntegrationError(
                f"Keyring token for {provider} lacks required minimal scope for {provider}.{operation}"
            )
        return token, granted

    def _call_mcp(
        self,
        provider: str,
        operation: str,
        tool_candidates: tuple[str, ...],
        arguments: dict[str, Any],
        *,
        capability: str,
    ) -> tuple[Any, str, list[str]]:
        _, sec = self._require_enabled(provider, capability)
        token, _scopes = self._require_scopes_and_token(provider, operation, sec)
        server_name = str(sec.get("server") or "").strip()
        if not server_name:
            raise IntegrationError(f"mcp.yaml integrations.{provider}.server is not configured")
        entries = Catalog(self.workspace).load()
        entry = entries.get(server_name)
        if entry is None:
            raise IntegrationError(
                f"MCP server '{server_name}' for {provider} is unavailable or mcp.enabled is false"
            )

        cfg = json.loads(json.dumps(entry["config"]))
        declared_tools = {t["name"]: t for t in cfg.get("tools", [])}
        chosen_name = next((name for name in tool_candidates if name in declared_tools), None)
        if chosen_name is None:
            raise IntegrationError(
                f"MCP server '{server_name}' does not declare any supported tool from {list(tool_candidates)}"
            )
        declared_tool = declared_tools[chosen_name]
        from jsonschema import ValidationError, validate
        try:
            validate(arguments, declared_tool["inputSchema"])
        except ValidationError as exc:
            raise IntegrationError(f"Invalid arguments for {provider}.{chosen_name}: {exc.message}") from None

        secrets = list({token, *self.vault.secret_values()})
        scan_text(json.dumps(arguments, allow_nan=False), workspace=self.workspace)

        from .jarvis.queue import Queue
        from .server import ServerState

        studio = ServerState(self.workspace, quiet=True).workbench.studio
        lock = Queue(studio)
        ident = "mcp-budget-" + hashlib.sha256(
            (str(self.workspace) + ":" + server_name + ":" + chosen_name).encode()
        ).hexdigest()
        now = time.time()
        with lock.locked():
            row = next((r for r in studio.items("mcp-budget") if r["id"] == ident), {})
            minute, day = int(now // 60), int(now // 86400)
            calls = row.get("calls", 0) if row.get("minute") == minute else 0
            daily = row.get("daily", 0) if row.get("day") == day else 0
            if calls >= cfg.get("rate_per_minute", 5) or daily >= cfg.get("max_calls_day", 20):
                raise IntegrationError("Persistent extension budget exhausted")
            studio.put(
                "mcp-budget",
                {"minute": minute, "calls": calls + 1, "day": day, "daily": daily + 1},
                ident,
            )

        # Inject OAuth token only inside the bounded child env / loopback header, never in args or logs.
        session_cfg = json.loads(json.dumps(cfg))
        with _SessionWithOAuth(self.workspace, session_cfg, token) as session:
            init = session.exchange("initialize", {
                "protocolVersion": MCP_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "nexus-integrations", "version": "1.0"},
            })
            if not isinstance(init, dict) or init.get("protocolVersion") != MCP_VERSION:
                raise IntegrationError("MCP server protocol version mismatch")
            session.exchange("notifications/initialized", notification=True)
            listed = session.exchange("tools/list")
            remote = {
                item.get("name"): item
                for item in (listed or {}).get("tools", [])
                if isinstance(item, dict)
            }
            if chosen_name not in remote or remote[chosen_name].get("inputSchema") != declared_tool["inputSchema"]:
                raise IntegrationError("MCP server live tool schema does not match reviewed manifest")
            raw_result = session.exchange("tools/call", {"name": chosen_name, "arguments": arguments})
            secrets = list({*secrets, *session.values})

        if len(json.dumps(raw_result, allow_nan=False)) > MCP_LIMIT * 2:
            raise IntegrationError("MCP result exceeds bounded budget")
        redacted_obj = redact_output(raw_result, secrets)
        wrapped_json, flags = wrap_mcp_result(server_name, chosen_name, redacted_obj, secrets)
        parsed = _parse_mcp_json_payload(redacted_obj)
        return parsed, wrapped_json, flags

    # ── Gmail ───────────────────────────────────────────────────────────── #

    def gmail_search(self, query: str, *, max_results: int | None = None) -> dict[str, Any]:
        _, sec = self._require_enabled("gmail", "search")
        limit = max(1, min(int(max_results or sec.get("max_results", 20)), 50))
        q = str(query or "").strip()[:400]
        parsed, wrapped, flags = self._call_mcp(
            "gmail",
            "search",
            ("gmail_search", "search_messages", "search_emails"),
            {"query": q, "max_results": limit},
            capability="search",
        )
        rows = _extract_list(parsed, ("messages", "emails", "items"))[:limit]
        sanitized_rows = [_sanitize_email_summary(row) for row in rows]
        all_flags = sorted({*flags, *[f for r in sanitized_rows for f in r["injection_flags"]]})
        return {
            "provider": "gmail",
            "query": q,
            "count": len(sanitized_rows),
            "messages": sanitized_rows,
            "untrusted": True,
            "wrapped": wrapped,
            "injection_flags": all_flags,
        }

    def gmail_read(self, message_id: str) -> dict[str, Any]:
        msg_id = _validate_external_id(message_id, "message_id")
        parsed, wrapped, flags = self._call_mcp(
            "gmail",
            "read",
            ("gmail_read", "get_message", "read_email"),
            {"message_id": msg_id},
            capability="read",
        )
        msg = _sanitize_email_full(parsed, fallback_id=msg_id)
        raw_body = msg.pop("raw_body", "")
        body_wrapped, body_flags = wrap_untrusted(
            raw_body,
            source=f"gmail:{msg_id}",
            kind="email",
        )
        all_flags = sorted({*flags, *msg["injection_flags"], *body_flags})
        msg["body"] = body_wrapped
        msg["wrapped_body"] = body_wrapped
        msg["injection_flags"] = all_flags
        msg["wrapped"] = wrapped
        return msg

    def gmail_summarize(self, query: str = "is:important", *, max_results: int = 3) -> dict[str, Any]:
        _, sec = self._require_enabled("gmail", "read")
        limit = max(1, min(int(max_results), 20))
        q = str(query or "is:important").strip()[:400]
        parsed, wrapped, flags = self._call_mcp(
            "gmail",
            "summarize",
            ("gmail_search", "search_messages", "search_emails"),
            {"query": q, "max_results": limit},
            capability="read",
        )
        rows = _extract_list(parsed, ("messages", "emails", "items"))[:limit]
        items: list[dict[str, Any]] = []
        all_flags = set(flags)
        for row in rows:
            item = _sanitize_email_summary(row)
            all_flags.update(item["injection_flags"])
            items.append(item)
        summary_lines = [
            f"{idx}. From {item['sender'] or 'unknown'} — {item['subject'] or '(no subject)'}: {item['snippet']}"
            for idx, item in enumerate(items, start=1)
        ]
        summary_text = "\n".join(summary_lines) if summary_lines else "No matching emails found."
        wrapped_summary, sum_flags = wrap_untrusted(summary_text, source="gmail:summary", kind="email")
        all_flags.update(sum_flags)
        return {
            "provider": "gmail",
            "query": q,
            "count": len(items),
            "emails": items,
            "summary": summary_text,
            "wrapped_summary": wrapped_summary,
            "wrapped": wrapped,
            "untrusted": True,
            "injection_flags": sorted(all_flags),
        }

    def gmail_draft(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        thread_id: str = "",
        in_reply_to: str = "",
    ) -> dict[str, Any]:
        resolved_to = self._resolve_recipient_or_question(to)
        if resolved_to.get("needs_disambiguation"):
            return {
                "drafted": False,
                "needs_disambiguation": True,
                "question": resolved_to["question"],
                "candidates": resolved_to["matches"],
            }
        addr = resolved_to["email"]
        clean_subject = _sanitize_for_speech(str(subject or ""), max_len=240).strip()
        clean_body = str(body or "")[:20_000]
        if not clean_subject or not clean_body.strip():
            raise IntegrationError("Draft requires non-empty subject and body")
        payload: dict[str, Any] = {"to": addr, "subject": clean_subject, "body": clean_body}
        if thread_id:
            payload["thread_id"] = _validate_external_id(thread_id, "thread_id")
        if in_reply_to:
            payload["in_reply_to"] = _validate_external_id(in_reply_to, "in_reply_to")
        parsed, wrapped, flags = self._call_mcp(
            "gmail",
            "draft",
            ("gmail_draft", "create_draft", "draft_reply"),
            payload,
            capability="draft",
        )
        return {
            "drafted": True,
            "sent": False,
            "to": addr,
            "subject": clean_subject,
            "result": parsed if isinstance(parsed, dict) else {"raw": parsed},
            "wrapped": wrapped,
            "injection_flags": flags,
        }

    def gmail_label(self, message_id: str, *, add_labels: list[str] | None = None, remove_labels: list[str] | None = None) -> dict[str, Any]:
        msg_id = _validate_external_id(message_id, "message_id")
        add = [_validate_label(x) for x in (add_labels or [])][:10]
        remove = [_validate_label(x) for x in (remove_labels or [])][:10]
        if not add and not remove:
            raise IntegrationError("Specify at least one label to add or remove")
        parsed, wrapped, flags = self._call_mcp(
            "gmail",
            "label",
            ("gmail_label", "modify_labels", "label_message"),
            {"message_id": msg_id, "add_labels": add, "remove_labels": remove},
            capability="labels",
        )
        return {"labeled": True, "message_id": msg_id, "result": parsed, "wrapped": wrapped, "injection_flags": flags}

    def gmail_archive(self, message_id: str) -> dict[str, Any]:
        msg_id = _validate_external_id(message_id, "message_id")
        parsed, wrapped, flags = self._call_mcp(
            "gmail",
            "archive",
            ("gmail_archive", "archive_message"),
            {"message_id": msg_id},
            capability="archive",
        )
        return {"archived": True, "message_id": msg_id, "result": parsed, "wrapped": wrapped, "injection_flags": flags}

    def gmail_send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        thread_id: str = "",
        in_reply_to: str = "",
    ) -> dict[str, Any]:
        resolved_to = self._resolve_recipient_or_question(to)
        if resolved_to.get("needs_disambiguation"):
            raise IntegrationError(resolved_to["question"])
        addr = resolved_to["email"]
        clean_subject = _sanitize_for_speech(str(subject or ""), max_len=240).strip()
        clean_body = str(body or "")[:20_000]
        if not clean_subject or not clean_body.strip():
            raise IntegrationError("Sending email requires recipient, subject, and body")
        payload: dict[str, Any] = {"to": addr, "subject": clean_subject, "body": clean_body}
        if thread_id:
            payload["thread_id"] = _validate_external_id(thread_id, "thread_id")
        if in_reply_to:
            payload["in_reply_to"] = _validate_external_id(in_reply_to, "in_reply_to")
        parsed, wrapped, flags = self._call_mcp(
            "gmail",
            "send",
            ("gmail_send", "send_message", "send_email"),
            payload,
            capability="send",
        )
        return {
            "sent": True,
            "to": addr,
            "subject": clean_subject,
            "body_preview": clean_body[:200],
            "result": parsed if isinstance(parsed, dict) else {"raw": parsed},
            "wrapped": wrapped,
            "injection_flags": flags,
        }

    # ── Google Calendar ─────────────────────────────────────────────────── #

    def calendar_agenda(self, *, time_min: str = "", time_max: str = "", max_results: int | None = None) -> dict[str, Any]:
        _, sec = self._require_enabled("calendar", "read")
        limit = max(1, min(int(max_results or sec.get("max_results", 25)), 50))
        args: dict[str, Any] = {"max_results": limit}
        if time_min:
            args["time_min"] = _clean_iso_str(time_min, "time_min")
        if time_max:
            args["time_max"] = _clean_iso_str(time_max, "time_max")
        parsed, wrapped, flags = self._call_mcp(
            "calendar",
            "read",
            ("calendar_agenda", "list_events"),
            args,
            capability="read",
        )
        events = [_sanitize_calendar_event(ev) for ev in _extract_list(parsed, ("events", "items"))[:limit]]
        all_flags = sorted({*flags, *[f for ev in events for f in ev["injection_flags"]]})
        return {
            "provider": "calendar",
            "count": len(events),
            "events": events,
            "untrusted": True,
            "wrapped": wrapped,
            "injection_flags": all_flags,
        }

    def calendar_free_time(self, *, time_min: str, time_max: str, duration_minutes: int = 30) -> dict[str, Any]:
        dur = int(duration_minutes)
        if not 5 <= dur <= 480:
            raise IntegrationError("duration_minutes must be between 5 and 480")
        args = {
            "time_min": _clean_iso_str(time_min, "time_min"),
            "time_max": _clean_iso_str(time_max, "time_max"),
            "duration_minutes": dur,
        }
        parsed, wrapped, flags = self._call_mcp(
            "calendar",
            "read",
            ("calendar_free_time", "find_free_time", "freebusy"),
            args,
            capability="read",
        )
        slots = _extract_list(parsed, ("slots", "free_slots", "items"))[:50]
        return {
            "provider": "calendar",
            "slots": slots,
            "untrusted": True,
            "wrapped": wrapped,
            "injection_flags": flags,
        }

    def calendar_create_event(
        self,
        *,
        summary: str,
        start: str,
        end: str,
        description: str = "",
        attendees: list[str] | None = None,
    ) -> dict[str, Any]:
        clean_summary = _sanitize_for_speech(str(summary or ""), max_len=240).strip()
        if not clean_summary:
            raise IntegrationError("Calendar event requires summary")
        clean_attendees = [_validate_email(a) for a in (attendees or [])][:25]
        capability = "invites" if clean_attendees else "write"
        operation = "invites" if clean_attendees else "write"
        if clean_attendees:
            # Invites require BOTH allow_write and allow_invites.
            self._require_enabled("calendar", "write")
        payload: dict[str, Any] = {
            "summary": clean_summary,
            "start": _clean_iso_str(start, "start"),
            "end": _clean_iso_str(end, "end"),
            "description": str(description or "")[:4000],
        }
        if clean_attendees:
            payload["attendees"] = clean_attendees
        parsed, wrapped, flags = self._call_mcp(
            "calendar",
            operation,
            ("calendar_create", "create_event"),
            payload,
            capability=capability,
        )
        return {
            "created": True,
            "has_invites": bool(clean_attendees),
            "attendees": clean_attendees,
            "result": parsed,
            "wrapped": wrapped,
            "injection_flags": flags,
        }

    def calendar_update_event(
        self,
        *,
        event_id: str,
        summary: str = "",
        start: str = "",
        end: str = "",
        description: str = "",
        attendees: list[str] | None = None,
    ) -> dict[str, Any]:
        ev_id = _validate_external_id(event_id, "event_id")
        clean_attendees = [_validate_email(a) for a in (attendees or [])][:25]
        capability = "invites" if clean_attendees else "write"
        operation = "invites" if clean_attendees else "write"
        if clean_attendees:
            self._require_enabled("calendar", "write")
        payload: dict[str, Any] = {"event_id": ev_id}
        if summary:
            payload["summary"] = _sanitize_for_speech(summary, max_len=240).strip()
        if start:
            payload["start"] = _clean_iso_str(start, "start")
        if end:
            payload["end"] = _clean_iso_str(end, "end")
        if description:
            payload["description"] = str(description)[:4000]
        if clean_attendees:
            payload["attendees"] = clean_attendees
        parsed, wrapped, flags = self._call_mcp(
            "calendar",
            operation,
            ("calendar_update", "update_event"),
            payload,
            capability=capability,
        )
        return {
            "updated": True,
            "event_id": ev_id,
            "has_invites": bool(clean_attendees),
            "attendees": clean_attendees,
            "result": parsed,
            "wrapped": wrapped,
            "injection_flags": flags,
        }

    # ── Contacts ────────────────────────────────────────────────────────── #

    def resolve_contact(self, query: str) -> dict[str, Any]:
        _, sec = self._require_enabled("contacts", "read")
        q = _sanitize_for_speech(str(query or ""), max_len=160).strip()
        if not q:
            raise IntegrationError("Contact query cannot be empty")
        limit = max(1, min(int(sec.get("max_results", 20)), 50))
        parsed, wrapped, flags = self._call_mcp(
            "contacts",
            "read",
            ("contacts_resolve", "search_contacts", "resolve_contact"),
            {"query": q, "max_results": limit},
            capability="read",
        )
        rows = _extract_list(parsed, ("contacts", "matches", "items"))[:limit]
        matches: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = _untrusted_clean_label(row.get("name") or row.get("displayName") or "", 120)
            addr = str(row.get("email") or "").strip()
            if not addr and isinstance(row.get("emails"), list) and row["emails"]:
                addr = str(row["emails"][0] or "").strip()
            if not addr:
                continue
            org = _untrusted_clean_label(row.get("org") or row.get("organization") or "", 80)
            matches.append({"name": name or addr, "email": addr, "org": org})
        if not matches:
            return {
                "status": "not_found",
                "query": q,
                "matches": [],
                "needs_disambiguation": True,
                "clarification_question": f"I couldn't find a contact matching '{q}'. Which email address should I use?",
                "untrusted": True,
                "wrapped": wrapped,
                "injection_flags": flags,
            }
        if len(matches) == 1:
            return {
                "status": "resolved",
                "query": q,
                "contact": matches[0],
                "matches": matches,
                "needs_disambiguation": False,
                "clarification_question": "",
                "untrusted": True,
                "wrapped": wrapped,
                "injection_flags": flags,
            }
        options = ", ".join(
            f"{idx}) {m['name']} ({m['email']})" for idx, m in enumerate(matches[:5], start=1)
        )
        return {
            "status": "ambiguous",
            "query": q,
            "matches": matches,
            "needs_disambiguation": True,
            "clarification_question": f"Several contacts match {q}: {options}. Which one should I use?",
            "untrusted": True,
            "wrapped": wrapped,
            "injection_flags": flags,
        }

    def _resolve_recipient_or_question(self, recipient: str) -> dict[str, Any]:
        raw = str(recipient or "").strip()
        parsed_addr = email.utils.parseaddr(raw)[1]
        if parsed_addr and "@" in parsed_addr and "." in parsed_addr.split("@", 1)[1]:
            return {"needs_disambiguation": False, "email": _validate_email(parsed_addr)}
        res = self.resolve_contact(raw)
        if res["status"] == "resolved":
            return {"needs_disambiguation": False, "email": _validate_email(res["contact"]["email"])}
        return {
            "needs_disambiguation": True,
            "question": res["clarification_question"],
            "matches": res.get("matches", []),
        }

    # ── Drive & Docs (MCP) ──────────────────────────────────────────────── #

    def drive_search(self, query: str, *, max_results: int | None = None) -> dict[str, Any]:
        _, sec = self._require_enabled("drive", "read")
        limit = max(1, min(int(max_results or sec.get("max_results", 20)), 50))
        q = _sanitize_for_speech(str(query or ""), max_len=300).strip()
        parsed, wrapped, flags = self._call_mcp(
            "drive",
            "search",
            ("drive_search", "search_files"),
            {"query": q, "max_results": limit},
            capability="read",
        )
        files = _extract_list(parsed, ("files", "items"))[:limit]
        cleaned = []
        all_flags = set(flags)
        for item in files:
            if not isinstance(item, dict):
                continue
            fid = str(item.get("id") or item.get("file_id") or "")
            name = _untrusted_clean_label(item.get("name") or item.get("title") or fid, 180)
            snippet = _untrusted_clean_label(item.get("snippet") or item.get("excerpt") or "", 600)
            mime = _untrusted_clean_label(item.get("mimeType") or item.get("mime_type") or "", 100)
            url = _untrusted_clean_label(item.get("url") or item.get("webViewLink") or "", 300)
            all_flags.update(flag_names(f"{name}\n{snippet}"))
            cleaned.append({
                "id": fid,
                "name": name,
                "mime_type": mime,
                "url": url,
                "snippet": snippet,
                "source_citation": f"drive:{fid} ({name})",
            })
        return {
            "provider": "drive",
            "query": q,
            "files": cleaned,
            "untrusted": True,
            "wrapped": wrapped,
            "injection_flags": sorted(all_flags),
        }

    def drive_read(self, file_id: str) -> dict[str, Any]:
        fid = _validate_external_id(file_id, "file_id")
        parsed, wrapped, flags = self._call_mcp(
            "drive",
            "read",
            ("drive_read", "read_file", "get_file"),
            {"file_id": fid},
            capability="read",
        )
        title = _untrusted_clean_label(
            parsed.get("name") or parsed.get("title") if isinstance(parsed, dict) else fid,
            180,
        ) or fid
        raw_text = (
            str(parsed.get("content") or parsed.get("text") or "")
            if isinstance(parsed, dict)
            else str(parsed or "")
        )[:80_000]
        url = _untrusted_clean_label(parsed.get("url") if isinstance(parsed, dict) else "", 300)
        wrapped_text, text_flags = wrap_untrusted(raw_text, source=f"drive:{fid}", kind="drive")
        return {
            "provider": "drive",
            "file_id": fid,
            "title": title,
            "url": url,
            "content": wrapped_text,
            "source_citation": f"drive:{fid} ({title})",
            "untrusted": True,
            "wrapped": wrapped,
            "injection_flags": sorted({*flags, *text_flags}),
        }

    def docs_read(self, document_id: str) -> dict[str, Any]:
        did = _validate_external_id(document_id, "document_id")
        parsed, wrapped, flags = self._call_mcp(
            "docs",
            "read",
            ("docs_read", "read_document", "get_document"),
            {"document_id": did},
            capability="read",
        )
        title = _untrusted_clean_label(
            parsed.get("title") or parsed.get("name") if isinstance(parsed, dict) else did,
            180,
        ) or did
        raw_text = (
            str(parsed.get("content") or parsed.get("text") or "")
            if isinstance(parsed, dict)
            else str(parsed or "")
        )[:80_000]
        url = _untrusted_clean_label(parsed.get("url") if isinstance(parsed, dict) else "", 300)
        wrapped_text, text_flags = wrap_untrusted(raw_text, source=f"docs:{did}", kind="docs")
        return {
            "provider": "docs",
            "document_id": did,
            "title": title,
            "url": url,
            "content": wrapped_text,
            "source_citation": f"docs:{did} ({title})",
            "untrusted": True,
            "wrapped": wrapped,
            "injection_flags": sorted({*flags, *text_flags}),
        }

    # ── Local Notes (Obsidian & Notion exports) ─────────────────────────── #

    def notes_search(self, query: str, *, limit: int | None = None) -> dict[str, Any]:
        _, sec = self._require_enabled("notes", "read")
        max_n = max(1, min(int(limit or sec.get("max_results", 20)), 50))
        q = _sanitize_for_speech(str(query or ""), max_len=300).strip()
        if not q:
            raise IntegrationError("Search query cannot be empty")
        terms = [t for t in re.findall(r"[a-z0-9_]+", q.casefold()) if len(t) >= 2] or [q.casefold()]
        scored: list[tuple[int, str, dict[str, Any]]] = []
        all_flags: set[str] = set()
        for kind, folder in self._iter_note_folders(sec):
            for path in self._iter_note_files(folder):
                try:
                    raw_text = path.read_text(encoding="utf-8", errors="replace")[:120_000]
                except OSError:
                    continue
                folded = raw_text.casefold()
                hits = sum(folded.count(term) for term in terms)
                if not hits:
                    continue
                rel = str(path.relative_to(folder))
                lines = raw_text.splitlines()
                hit_line = 1
                for idx, line in enumerate(lines, start=1):
                    if any(t in line.casefold() for t in terms):
                        hit_line = idx
                        break
                start_l = max(1, hit_line - 2)
                end_l = min(len(lines) or 1, hit_line + 8)
                excerpt = "\n".join(lines[start_l - 1 : end_l])[:3000]
                wrapped_excerpt, e_flags = wrap_untrusted(excerpt, source=f"{kind}:{rel}", kind="notes")
                all_flags.update(e_flags)
                citation = f"{kind}:{rel}:{start_l}-{end_l}"
                scored.append((
                    hits,
                    citation,
                    {
                        "source_type": kind,
                        "path": str(path),
                        "relative_path": rel,
                        "start_line": start_l,
                        "end_line": end_l,
                        "score": hits,
                        "excerpt": wrapped_excerpt,
                        "source_citation": citation,
                        "injection_flags": e_flags,
                    },
                ))
        scored.sort(key=lambda row: (-row[0], row[1]))
        results = [row[2] for row in scored[:max_n]]
        return {
            "provider": "notes",
            "query": q,
            "count": len(results),
            "results": results,
            "untrusted": True,
            "injection_flags": sorted(all_flags),
        }

    def notes_read(self, relative_or_path: str) -> dict[str, Any]:
        _, sec = self._require_enabled("notes", "read")
        raw_req = str(relative_or_path or "").strip()
        if not raw_req or "\x00" in raw_req:
            raise IntegrationError("Note path is required")
        # Accept prefixed citation like "obsidian:folder/file.md" or "notion:page.md"
        stripped = raw_req
        for prefix in ("obsidian:", "notion:"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
                break
        if ":" in stripped and re.search(r":\d+-\d+$", stripped):
            stripped = re.sub(r":\d+-\d+$", "", stripped)
        for kind, folder in self._iter_note_folders(sec):
            candidate = (folder / stripped).resolve() if not Path(stripped).is_absolute() else Path(stripped).resolve()
            if not candidate.is_relative_to(folder) or candidate.is_symlink() or not candidate.is_file():
                continue
            if candidate.stat().st_size > 1_000_000:
                raise IntegrationError("Note file exceeds 1 MB limit")
            if _looks_secret_note(candidate):
                raise IntegrationError("Refusing to read potential secret file from notes folder")
            raw_text = candidate.read_text(encoding="utf-8", errors="replace")[:80_000]
            rel = str(candidate.relative_to(folder))
            wrapped_text, e_flags = wrap_untrusted(raw_text, source=f"{kind}:{rel}", kind="notes")
            return {
                "provider": kind,
                "path": str(candidate),
                "relative_path": rel,
                "content": wrapped_text,
                "source_citation": f"{kind}:{rel}",
                "untrusted": True,
                "injection_flags": e_flags,
            }
        raise IntegrationError("Requested note was not found in configured Obsidian or Notion folders")

    def _iter_note_folders(self, sec: dict[str, Any]) -> list[tuple[str, Path]]:
        folders: list[tuple[str, Path]] = []
        for kind, key in (("obsidian", "obsidian_folders"), ("notion", "notion_folders")):
            for raw in sec.get(key, []):
                candidate = Path(raw).expanduser()
                resolved = candidate.resolve() if candidate.is_absolute() else (self.workspace / candidate).resolve()
                if resolved.is_dir() and not resolved.is_symlink():
                    folders.append((kind, resolved))
        return folders

    @staticmethod
    def _iter_note_files(folder: Path) -> list[Path]:
        found: list[Path] = []
        for path in sorted(folder.rglob("*")):
            if len(found) >= 500:
                break
            if path.is_symlink() or not path.is_file():
                continue
            rel_parts = path.relative_to(folder).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            if path.suffix.lower() not in {".md", ".markdown", ".txt", ".csv"}:
                continue
            if _looks_secret_note(path):
                continue
            try:
                if path.stat().st_size > 1_000_000:
                    continue
            except OSError:
                continue
            found.append(path)
        return found


class _OAuthSession(Session):
    """Session subclass that passes keyring OAuth token strictly via child env/header and masks it."""

    def __init__(self, root: Path, config: dict[str, Any], oauth_token: str) -> None:
        self._oauth_token = oauth_token
        super().__init__(root, config, lambda: None)

    def __enter__(self):
        if self.cfg.get("transport") == "http":
            try:
                import httpx

                from .extensions.transport import secret

                self.guard()
                headers = {
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": MCP_VERSION,
                    "Accept-Encoding": "identity",
                }
                for name, ref in self.cfg.get("headers", {}).items():
                    value = secret(ref)
                    self.values.append(value)
                    headers[name] = value
                if self._oauth_token and not any(k.lower() == "authorization" for k in headers):
                    bearer = f"Bearer {self._oauth_token}"
                    headers["Authorization"] = bearer
                    self.values.extend([self._oauth_token, bearer])
                self.client = httpx.Client(headers=headers, trust_env=False, follow_redirects=False)
                return self
            except BaseException:
                self.__exit__()
                raise

        import queue as _queue
        import subprocess
        import threading

        from .extensions.transport import secret

        try:
            self.guard()
            allowed = {"PATH", "SYSTEMROOT", "WINDIR", "USERPROFILE", "HOME", "TEMP", "TMP", "LANG"}
            env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
            for name, ref in self.cfg.get("env", {}).items():
                value = secret(ref)
                self.values.append(value)
                env[name] = value
            if self._oauth_token:
                env.setdefault("NEXUS_OAUTH_TOKEN", self._oauth_token)
                self.values.append(self._oauth_token)
            self.process = subprocess.Popen(
                self.cfg["command"],
                cwd=self.root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )

            def read():
                try:
                    while not self.stop.is_set():
                        line = self.process.stdout.readline(MCP_LIMIT + 1)
                        if not line or len(line) > MCP_LIMIT:
                            self.stop.set()
                            break
                        try:
                            self.messages.put_nowait(json.loads(line))
                        except (ValueError, _queue.Full):
                            self.stop.set()
                except Exception:
                    self.stop.set()

            def watchdog():
                while not self.closed.wait(0.05):
                    try:
                        self.remaining()
                    except Exception:
                        self.stop.set()
                        try:
                            self.terminate()
                        except (OSError, subprocess.TimeoutExpired):
                            pass
                        return

            threading.Thread(target=read, daemon=True, name="mcp-bounded-output").start()
            threading.Thread(target=watchdog, daemon=True, name="mcp-deadline").start()
            return self
        except BaseException:
            self.__exit__()
            raise


_SessionWithOAuth = _OAuthSession


# ── Helpers & sanitization ──────────────────────────────────────────────── #


def _looks_secret_note(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith(".env") or name in {"secrets.md", "credentials.md", "id_rsa"} or path.suffix.lower() in {".pem", ".key", ".p12"}


def _validate_external_id(value: Any, label: str) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 200 or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", raw):
        raise IntegrationError(f"Invalid {label}")
    return raw


def _validate_label(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 64 or not re.fullmatch(r"[A-Za-z0-9_/-]{1,64}", raw):
        raise IntegrationError(f"Invalid Gmail label: {value!r}")
    return raw


def _validate_email(value: Any) -> str:
    raw = str(value or "").strip()
    addr = email.utils.parseaddr(raw)[1]
    if not addr or addr != raw or "\r" in addr or "\n" in addr or "@" not in addr or "." not in addr.split("@", 1)[1]:
        raise IntegrationError(f"Invalid email address: {value!r}")
    return addr


def _clean_iso_str(value: Any, label: str) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 64 or any(ch in raw for ch in "\r\n\x00") or not re.fullmatch(r"[0-9T:.+Z_-]{4,64}", raw):
        raise IntegrationError(f"Invalid ISO datetime for {label}")
    return raw


def _extract_list(parsed: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in keys:
            val = parsed.get(key)
            if isinstance(val, list):
                return val
    return []


def _sanitize_email_summary(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        row = {"snippet": str(row)}
    mid = str(row.get("id") or row.get("message_id") or "")
    sender = _untrusted_clean_label(row.get("from") or row.get("sender") or "", 140)
    subject = _untrusted_clean_label(row.get("subject") or "", 200)
    snippet = _untrusted_clean_label(row.get("snippet") or row.get("preview") or "", 400)
    raw_blob = "\n".join(str(row.get(k) or "") for k in ("from", "sender", "subject", "snippet", "body"))
    flags = flag_names(raw_blob)
    return {
        "id": mid,
        "sender": sender,
        "subject": subject,
        "snippet": snippet,
        "date": _untrusted_clean_label(row.get("date") or "", 64),
        "important": bool(row.get("important", True)),
        "injection_flags": flags,
    }


def _sanitize_email_full(parsed: Any, *, fallback_id: str) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        parsed = {"body": str(parsed or "")}
    mid = str(parsed.get("id") or parsed.get("message_id") or fallback_id)
    sender = _untrusted_clean_label(parsed.get("from") or parsed.get("sender") or "", 140)
    subject = _untrusted_clean_label(parsed.get("subject") or "", 200)
    raw_body = str(parsed.get("body") or parsed.get("text") or parsed.get("snippet") or "")[:40_000]
    flags = flag_names(f"{sender}\n{subject}\n{raw_body}")
    return {
        "id": mid,
        "sender": sender,
        "subject": subject,
        "date": _untrusted_clean_label(parsed.get("date") or "", 64),
        "raw_body": raw_body,
        "clean_body": _sanitize_for_speech(raw_body, max_len=12_000),
        "untrusted": True,
        "injection_flags": flags,
    }


def _sanitize_calendar_event(ev: Any) -> dict[str, Any]:
    if not isinstance(ev, dict):
        ev = {"summary": str(ev)}
    raw_desc = str(ev.get("description") or "")[:8000]
    wrapped_desc, d_flags = wrap_untrusted(raw_desc, source=f"calendar:{ev.get('id', 'event')}", kind="calendar")
    summary = _untrusted_clean_label(ev.get("summary") or "", 200)
    attendees = [
        _untrusted_clean_label(a, 140)
        for a in (ev.get("attendees") or [])[:25]
        if isinstance(a, str)
    ]
    return {
        "id": str(ev.get("id") or ""),
        "summary": summary,
        "start": _untrusted_clean_label(ev.get("start") or "", 64),
        "end": _untrusted_clean_label(ev.get("end") or "", 64),
        "attendees": attendees,
        "description": wrapped_desc,
        "clean_description": _sanitize_for_speech(raw_desc, max_len=2000),
        "injection_flags": sorted({*flag_names(summary), *d_flags}),
    }


# ── Voice Email Summaries & Triage State ────────────────────────────────── #


_NUMBER_WORDS_EN = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}
_NUMBER_WORDS_BN = {1: "একটা", 2: "দুইটা", 3: "তিনটা", 4: "চারটা", 5: "পাঁচটা"}

_VOICE_EMAIL_CHECK_RE = re.compile(
    r"\b(?:check|read|summari[sz]e|show|any|what(?:'s|\s+are)|get)\s+(?:my\s+)?(?:important\s+|unread\s+|recent\s+)?emails?\b"
    r"|\bimportant\s+emails?\b"
    r"|(?:আমার\s+)?(?:গুরুত্বপূর্ণ\s+)?(?:ইমেইল|মেইল)(?:গুলো)?\s*(?:দেখো|পড়ো|পড়ো|চেক|সামারি)",
    re.I,
)
_VOICE_EMAIL_READ_RE = re.compile(
    r"^\s*(?:read|open|tell\s+me)(?:\s+(?:it|that|this|them|email|message|the\s+)?(?:(?:first|second|third|last)(?:\s+one)?|#?\d+))?\s*[.!?]?\s*$"
    r"|(?:প্রথমটা|দ্বিতীয়টা|তৃতীয়টা|ওটা)\s*(?:পড়ো|পড়ো)",
    re.I,
)
_VOICE_EMAIL_REPLY_RE = re.compile(
    r"^\s*(?:reply|draft\s+(?:a\s+)?reply|respond)(?:\s+(?:to\s+)?(?:it|that|the\s+(?:first|second|third|last)(?:\s+one)?|#?\d+))?(?:\s+(?:saying|with|:)\s+(.+))?\s*$"
    r"|(?:উত্তর|রিপ্লাই)\s*(?:দাও|লেখো)",
    re.I,
)
_VOICE_EMAIL_IGNORE_RE = re.compile(
    r"^\s*(?:ignore|dismiss|skip|never\s*mind|none)(?:\s+(?:it|that|them|all|the\s+(?:first|second|third|last)(?:\s+one)?|#?\d+))?\s*[.!?]?\s*$"
    r"|(?:বাদ\s*দাও|উপেক্ষা\s*করো|লাগবে\s*না)",
    re.I,
)


def _extract_spoken_index(text: str, total: int) -> int:
    lowered = text.casefold()
    if any(k in lowered for k in ("second", "2", "দ্বিতীয়")):
        return min(1, total - 1)
    if any(k in lowered for k in ("third", "3", "তৃতীয়")):
        return min(2, total - 1)
    if any(k in lowered for k in ("last", "শেষ")):
        return max(0, total - 1)
    m = re.search(r"#?(\d+)\b", lowered)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < total:
            return idx
    return 0


def format_voice_email_summary(emails: list[dict[str, Any]], *, language: str = "en") -> str:
    count = len(emails)
    is_bn = str(language or "").startswith("bn")
    if count == 0:
        return "আপনার কোনো নতুন গুরুত্বপূর্ণ ইমেইল নেই।" if is_bn else "You have no important emails."
    count_word = (_NUMBER_WORDS_BN if is_bn else _NUMBER_WORDS_EN).get(count, str(count))
    items_parts = []
    for idx, item in enumerate(emails[:5], start=1):
        sender = _sanitize_for_speech(str(item.get("sender") or "unknown"), max_len=80).strip()
        subject = _sanitize_for_speech(str(item.get("subject") or "no subject"), max_len=120).strip()
        items_parts.append(f"{idx}) From {sender} about {subject}" if not is_bn else f"{idx}) {sender} থেকে: {subject}")
    listing = "; ".join(items_parts)
    if is_bn:
        return f"আপনার {count_word} গুরুত্বপূর্ণ ইমেইল আছে: {listing}। পড়তে, রিপ্লাই করতে, নাকি বাদ দিতে বলবেন?"
    noun = "email" if count == 1 else "emails"
    return f"You have {count_word} important {noun}: {listing}. Would you like to read, reply, or ignore?"


@dataclass
class VoiceEmailSession:
    """Tracks the current spoken email summary options (read, reply, ignore)."""

    workspace: Path
    keyring_module: Any = None
    emails: list[dict[str, Any]] = field(default_factory=list)
    active: bool = False
    ignored_ids: set[str] = field(default_factory=set)
    _pending_reply_idx: int | None = None

    def _client(self) -> PersonalIntegrations:
        return PersonalIntegrations(self.workspace, keyring_module=self.keyring_module)

    def _draft_reply_for_index(self, idx: int, body: str) -> tuple[str, str]:
        target = self.emails[idx]
        sender = str(target.get("sender") or "").strip()
        subject = str(target.get("subject") or "Reply").strip()
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        try:
            drafted = self._client().gmail_draft(
                to=sender,
                subject=subject,
                body=body,
                in_reply_to=str(target.get("id") or ""),
            )
        except IntegrationError as exc:
            return f"Could not draft reply: {exc}", "email_error"
        if drafted.get("needs_disambiguation"):
            return str(drafted["question"]), "email_disambiguate"
        return (
            f"Saved a reply draft to {drafted['to']} with subject {drafted['subject']}. "
            "Sending always requires fresh approval and second factor.",
            "email_drafted",
        )

    def handle_utterance(self, text: str, *, language: str = "en") -> tuple[str | None, str]:
        stripped = (text or "").strip()
        if not stripped:
            return None, ""
        is_bn = str(language or "").startswith("bn")

        if self._pending_reply_idx is not None and self.active and self.emails:
            idx = self._pending_reply_idx
            self._pending_reply_idx = None
            if _VOICE_EMAIL_IGNORE_RE.match(stripped):
                return ("Okay, cancelled the reply draft.", "email_ignore")
            return self._draft_reply_for_index(idx, stripped)

        if _VOICE_EMAIL_CHECK_RE.search(stripped):
            try:
                summary = self._client().gmail_summarize(query="is:important", max_results=3)
            except IntegrationError as exc:
                return f"Email integration is not ready: {exc}", "email_unavailable"
            self.emails = list(summary.get("emails") or [])
            self.active = bool(self.emails)
            self._pending_reply_idx = None
            return format_voice_email_summary(self.emails, language=language), "email_summary"

        if not self.active or not self.emails:
            return None, ""

        if _VOICE_EMAIL_IGNORE_RE.match(stripped):
            lowered = stripped.casefold()
            if "all" in lowered or "them" in lowered or "none" in lowered:
                self.ignored_ids.update(str(e.get("id") or "") for e in self.emails)
                self.active = False
                return (
                    "ঠিক আছে, সবগুলো ইমেইল বাদ দেওয়া হয়েছে।"
                    if is_bn
                    else "Okay, ignored those emails.",
                    "email_ignore",
                )
            idx = _extract_spoken_index(stripped, len(self.emails))
            target = self.emails.pop(idx)
            if target.get("id"):
                self.ignored_ids.add(str(target["id"]))
            self.active = bool(self.emails)
            sender = _sanitize_for_speech(str(target.get("sender") or "that email"), max_len=60).strip()
            return (
                f"ঠিক আছে, {sender}-এর ইমেইল বাদ দিয়েছি।" if is_bn else f"Okay, ignored the email from {sender}.",
                "email_ignore",
            )

        if _VOICE_EMAIL_READ_RE.match(stripped):
            idx = _extract_spoken_index(stripped, len(self.emails))
            target = self.emails[idx]
            msg_id = str(target.get("id") or "")
            if not msg_id:
                snippet = _sanitize_for_speech(str(target.get("snippet") or ""), max_len=500).strip()
                return (
                    f"From {target.get('sender', 'unknown')} about {target.get('subject', '')}: {snippet}. Reply or ignore?",
                    "email_read",
                )
            try:
                full = self._client().gmail_read(msg_id)
            except IntegrationError as exc:
                return f"Could not read that email: {exc}", "email_error"
            clean_body = full.get("clean_body") or _sanitize_for_speech(full.get("raw_body", ""), max_len=800)
            reply_text = (
                f"From {full['sender'] or 'unknown'} about {full['subject'] or 'no subject'}: "
                f"{clean_body}. Would you like to reply or ignore?"
            )
            return reply_text, "email_read"

        m_reply = _VOICE_EMAIL_REPLY_RE.match(stripped)
        if m_reply:
            idx = _extract_spoken_index(stripped, len(self.emails))
            target = self.emails[idx]
            body = (m_reply.group(1) or "").strip() if m_reply.lastindex else ""
            if not body:
                self._pending_reply_idx = idx
                sender = _sanitize_for_speech(str(target.get("sender") or "them"), max_len=80).strip()
                return (
                    f"What should I draft in the reply to {sender}?",
                    "email_reply_prompt",
                )
            return self._draft_reply_for_index(idx, body)

        return None, ""


# ── ToolRegistry registration ───────────────────────────────────────────── #


def _tool_output(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def register_personal_integrations(ctx: Any, registry: Any, *, read_only: bool = False) -> list[str]:
    """Register strictly enabled & individually scoped personal integration tools."""
    workspace = Path(ctx.workspace).resolve()
    try:
        pol = load_integrations_policy(workspace)
    except ValueError:
        return []
    if not pol.get("enabled"):
        return []

    client = PersonalIntegrations(workspace)
    registered: list[str] = []

    def _add(
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Any,
        *,
        risk: Risk,
    ) -> None:
        if read_only and risk is not Risk.READ:
            return
        registry.register(
            name,
            description,
            input_schema,
            handler,
            risk=risk,
            read_only=(risk is Risk.READ),
        )
        registered.append(name)

    gmail_cfg = pol["gmail"]
    if gmail_cfg["enabled"]:
        if gmail_cfg["allow_search"]:
            _add(
                "integration_gmail_search",
                "Search Gmail messages through the scoped MCP server (untrusted data).",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Gmail search query"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                lambda args: _tool_output(client.gmail_search(args["query"], max_results=args.get("max_results"))),
                risk=Risk.READ,
            )
        if gmail_cfg["allow_read"]:
            _add(
                "integration_gmail_read",
                "Read one Gmail message by ID (wrapped as untrusted data).",
                {
                    "type": "object",
                    "properties": {"message_id": {"type": "string"}},
                    "required": ["message_id"],
                    "additionalProperties": False,
                },
                lambda args: _tool_output(client.gmail_read(args["message_id"])),
                risk=Risk.READ,
            )
            _add(
                "integration_gmail_summarize",
                "Summarize matching Gmail messages safely (read-only, untrusted).",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "additionalProperties": False,
                },
                lambda args: _tool_output(
                    client.gmail_summarize(args.get("query", "is:important"), max_results=args.get("max_results", 3))
                ),
                risk=Risk.READ,
            )
        if gmail_cfg["allow_draft"]:
            _add(
                "integration_gmail_draft",
                "Create a Gmail draft reply without sending it.",
                {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient email or contact name"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                        "thread_id": {"type": "string"},
                        "in_reply_to": {"type": "string"},
                    },
                    "required": ["to", "subject", "body"],
                    "additionalProperties": False,
                },
                lambda args: _tool_output(
                    client.gmail_draft(
                        to=args["to"],
                        subject=args["subject"],
                        body=args["body"],
                        thread_id=args.get("thread_id", ""),
                        in_reply_to=args.get("in_reply_to", ""),
                    )
                ),
                risk=Risk.WRITE,
            )
        if gmail_cfg["allow_labels"]:
            _add(
                "integration_gmail_label",
                "Apply or remove labels on a Gmail message (MEDIUM, only when configured).",
                {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string"},
                        "add_labels": {"type": "array", "items": {"type": "string"}},
                        "remove_labels": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["message_id"],
                    "additionalProperties": False,
                },
                lambda args: _tool_output(
                    client.gmail_label(
                        args["message_id"],
                        add_labels=args.get("add_labels"),
                        remove_labels=args.get("remove_labels"),
                    )
                ),
                risk=Risk.WRITE,
            )
        if gmail_cfg["allow_archive"]:
            _add(
                "integration_gmail_archive",
                "Archive a Gmail message (MEDIUM, only when configured).",
                {
                    "type": "object",
                    "properties": {"message_id": {"type": "string"}},
                    "required": ["message_id"],
                    "additionalProperties": False,
                },
                lambda args: _tool_output(client.gmail_archive(args["message_id"])),
                risk=Risk.WRITE,
            )
        if gmail_cfg["allow_send"]:
            _add(
                "integration_gmail_send",
                (
                    "Send an email (DANGEROUS: always shows recipient, subject, and body and requires "
                    "fresh approval plus second factor; never auto-sent)."
                ),
                {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient email or contact name"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                        "thread_id": {"type": "string"},
                        "in_reply_to": {"type": "string"},
                    },
                    "required": ["to", "subject", "body"],
                    "additionalProperties": False,
                },
                lambda args: _tool_output(
                    client.gmail_send(
                        to=args["to"],
                        subject=args["subject"],
                        body=args["body"],
                        thread_id=args.get("thread_id", ""),
                        in_reply_to=args.get("in_reply_to", ""),
                    )
                ),
                risk=Risk.DANGEROUS,
            )

    cal_cfg = pol["calendar"]
    if cal_cfg["enabled"]:
        if cal_cfg["allow_read"]:
            _add(
                "integration_calendar_agenda",
                "Read Google Calendar agenda events (read-only, untrusted).",
                {
                    "type": "object",
                    "properties": {
                        "time_min": {"type": "string"},
                        "time_max": {"type": "string"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "additionalProperties": False,
                },
                lambda args: _tool_output(
                    client.calendar_agenda(
                        time_min=args.get("time_min", ""),
                        time_max=args.get("time_max", ""),
                        max_results=args.get("max_results"),
                    )
                ),
                risk=Risk.READ,
            )
            _add(
                "integration_calendar_free_time",
                "Find free time slots in Google Calendar (read-only).",
                {
                    "type": "object",
                    "properties": {
                        "time_min": {"type": "string"},
                        "time_max": {"type": "string"},
                        "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 480},
                    },
                    "required": ["time_min", "time_max"],
                    "additionalProperties": False,
                },
                lambda args: _tool_output(
                    client.calendar_free_time(
                        time_min=args["time_min"],
                        time_max=args["time_max"],
                        duration_minutes=args.get("duration_minutes", 30),
                    )
                ),
                risk=Risk.READ,
            )
        if cal_cfg["allow_write"]:
            _add(
                "integration_calendar_create",
                (
                    "Create a Google Calendar event (MEDIUM with notification; "
                    "invitations to attendees are DANGEROUS)."
                ),
                {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "description": {"type": "string"},
                        "attendees": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["summary", "start", "end"],
                    "additionalProperties": False,
                },
                lambda args: _tool_output(
                    client.calendar_create_event(
                        summary=args["summary"],
                        start=args["start"],
                        end=args["end"],
                        description=args.get("description", ""),
                        attendees=args.get("attendees"),
                    )
                ),
                risk=Risk.WRITE,
            )
            _add(
                "integration_calendar_update",
                (
                    "Modify a Google Calendar event (MEDIUM with notification; "
                    "invitations to attendees are DANGEROUS)."
                ),
                {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "summary": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "description": {"type": "string"},
                        "attendees": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["event_id"],
                    "additionalProperties": False,
                },
                lambda args: _tool_output(
                    client.calendar_update_event(
                        event_id=args["event_id"],
                        summary=args.get("summary", ""),
                        start=args.get("start", ""),
                        end=args.get("end", ""),
                        description=args.get("description", ""),
                        attendees=args.get("attendees"),
                    )
                ),
                risk=Risk.WRITE,
            )

    if pol["contacts"]["enabled"] and pol["contacts"]["allow_read"]:
        _add(
            "integration_contacts_resolve",
            "Resolve a person name via Contacts MCP and return disambiguation choices when multiple match.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            lambda args: _tool_output(client.resolve_contact(args["query"])),
            risk=Risk.READ,
        )

    if pol["drive"]["enabled"] and pol["drive"]["allow_read"]:
        _add(
            "integration_drive_search",
            "Search Google Drive files with source citations (read-only, untrusted).",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            lambda args: _tool_output(client.drive_search(args["query"], max_results=args.get("max_results"))),
            risk=Risk.READ,
        )
        _add(
            "integration_drive_read",
            "Read a Google Drive file by ID with source citations (read-only, untrusted).",
            {
                "type": "object",
                "properties": {"file_id": {"type": "string"}},
                "required": ["file_id"],
                "additionalProperties": False,
            },
            lambda args: _tool_output(client.drive_read(args["file_id"])),
            risk=Risk.READ,
        )

    if pol["docs"]["enabled"] and pol["docs"]["allow_read"]:
        _add(
            "integration_docs_read",
            "Read a Google Doc by ID with source citations (read-only, untrusted).",
            {
                "type": "object",
                "properties": {"document_id": {"type": "string"}},
                "required": ["document_id"],
                "additionalProperties": False,
            },
            lambda args: _tool_output(client.docs_read(args["document_id"])),
            risk=Risk.READ,
        )

    if pol["notes"]["enabled"] and pol["notes"]["allow_read"]:
        _add(
            "integration_notes_search",
            "Search configured local Obsidian or Notion export folders with line citations.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            lambda args: _tool_output(client.notes_search(args["query"], limit=args.get("limit"))),
            risk=Risk.READ,
        )
        _add(
            "integration_notes_read",
            "Read a note from configured local Obsidian or Notion export folders with source citations.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            lambda args: _tool_output(client.notes_read(args["path"])),
            risk=Risk.READ,
        )

    return registered


def integrations_status(workspace: Path | str, *, keyring_module: Any = None) -> dict[str, Any]:
    root = Path(workspace).resolve()
    pol = load_integrations_policy(root)
    vault = OAuthTokenVault(root, keyring_module=keyring_module)
    keyring_info = vault.status_summary()
    providers: dict[str, Any] = {}
    for name in INTEGRATION_PROVIDERS:
        sec = pol[name]
        kr = keyring_info.get(name, {})
        providers[name] = {
            "enabled": bool(pol["enabled"] and sec.get("enabled")),
            "configured_enabled": bool(sec.get("enabled")),
            "server": sec.get("server", ""),
            "configured_scopes": list(sec.get("scopes", [])),
            "granted_scopes": list(kr.get("granted_scopes", [])),
            "token_stored": bool(kr.get("token_stored", False)),
            "capabilities": {k: v for k, v in sec.items() if k.startswith("allow_")},
        }
        if "keyring_error" in kr:
            providers[name]["keyring_error"] = kr["keyring_error"]
    notes = pol["notes"]
    providers["notes"] = {
        "enabled": bool(pol["enabled"] and notes.get("enabled")),
        "configured_enabled": bool(notes.get("enabled")),
        "obsidian_folders": list(notes.get("obsidian_folders", [])),
        "notion_folders": list(notes.get("notion_folders", [])),
        "allow_read": bool(notes.get("allow_read")),
    }
    return {
        "workspace": str(root),
        "enabled": bool(pol["enabled"]),
        "keyring_service": KEYRING_SERVICE,
        "providers": providers,
    }


def grant_integration_token(
    workspace: Path | str,
    provider: str,
    token: str,
    *,
    scopes: list[str],
    refresh_token: str = "",
    expires_at: float | None = None,
    keyring_module: Any = None,
) -> dict[str, Any]:
    root = Path(workspace).resolve()
    vault = OAuthTokenVault(root, keyring_module=keyring_module)
    result = vault.grant(provider, token, scopes=scopes, refresh_token=refresh_token, expires_at=expires_at)
    from . import audit
    try:
        audit.record(
            root,
            "integrations.grant",
            who="user",
            target=provider,
            result="granted",
            risk="medium",
            detail={"scopes": result["granted_scopes"], "storage": "keyring"},
        )
    except (OSError, ValueError):
        pass
    return result


def revoke_integration_token(
    workspace: Path | str,
    provider: str = "all",
    *,
    keyring_module: Any = None,
) -> dict[str, Any]:
    root = Path(workspace).resolve()
    vault = OAuthTokenVault(root, keyring_module=keyring_module)
    result = vault.revoke(provider)
    from . import audit
    try:
        audit.record(
            root,
            "integrations.revoke",
            who="user",
            target=provider,
            result="revoked",
            risk="medium",
            detail={"provider": provider, "revoked": result.get("revoked")},
        )
    except (OSError, ValueError):
        pass
    return result


def mask_integration_secrets(workspace: Path | str, text: str, *, keyring_module: Any = None) -> str:
    vault = OAuthTokenVault(workspace, keyring_module=keyring_module)
    return mask(text, vault.secret_values())
