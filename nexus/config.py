"""Configuration loading: ``agents.yaml``, ``roles.yaml``, ``pipelines.yaml``.

Resolution order (later wins):

1. bundled defaults next to this package (``<repo>/agents.yaml`` ...)
2. ``$NEXUS_HOME`` (default ``~/.config/nexus``)
3. the current project (``./.nexus/*.yaml`` then ``./agents.yaml`` ...)
4. environment variables (``${VAR}`` / ``${VAR:-default}`` expansion happens
   inside the YAML values, plus a few first-class ``NEXUS_*`` overrides)

Adding a role never requires a code change - drop a YAML block in
``roles.yaml`` (or ``.nexus/roles.yaml`` for project-local specialists).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import keys as keyring
from .types import Keyword, Role, sanitize_role_name

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
BUNDLED_CONFIG = PACKAGE_ROOT / "_config"   # where a pip-installed wheel keeps the defaults

DEFAULT_AGENTS_FILE = "agents.yaml"
DEFAULT_ROLES_FILE = "roles.yaml"
DEFAULT_PIPELINES_FILE = "pipelines.yaml"
DEFAULT_ROLES_DIR = "roles.d"          # drop-in packs (e.g. `nexus addagent` imports)

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

BUILTIN_ROLES: dict[str, Role] = {}  # filled by _ensure_builtin_roles()


# --------------------------------------------------------------------------- #
# env expansion
# --------------------------------------------------------------------------- #


def expand_env(value: Any) -> Any:
    """Recursively replace ``${VAR}`` / ``${VAR:-fallback}`` in strings."""
    if isinstance(value, str):
        def _sub(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            return os.environ.get(name) or (default if default is not None else "")
        return _ENV_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def nexus_home() -> Path:
    home = os.environ.get("NEXUS_HOME")
    return Path(home).expanduser() if home else Path.home() / ".config" / "nexus"


def candidate_files(filename: str, workspace: Path) -> list[Path]:
    """All config locations for ``filename``, lowest precedence first."""
    cands = [
        BUNDLED_CONFIG / filename,          # installed wheel defaults
        REPO_ROOT / filename,               # running from a checkout
        nexus_home() / filename,            # ~/.config/nexus
        workspace / ".nexus" / filename,    # project local
        workspace / filename,               # project root
    ]
    seen: list[Path] = []
    for c in cands:
        if c not in seen:
            seen.append(c)
    return seen


def candidate_role_dirs(workspace: Path) -> list[Path]:
    """All ``roles.d/`` drop-in directories, lowest precedence first.

    A pack written by ``nexus addagent`` (or by hand) is just a ``roles.yaml``
    layer, so importing an agent never requires a code change.
    """
    cands = [
        BUNDLED_CONFIG / DEFAULT_ROLES_DIR,     # installed wheel defaults
        REPO_ROOT / DEFAULT_ROLES_DIR,          # running from a checkout (committed packs)
        nexus_home() / DEFAULT_ROLES_DIR,       # ~/.config/nexus/roles.d
        workspace / ".nexus" / DEFAULT_ROLES_DIR,  # project local
        workspace / DEFAULT_ROLES_DIR,          # project root
    ]
    seen: list[Path] = []
    for c in cands:
        if c not in seen:
            seen.append(c)
    return seen


def load_role_packs(workspace: Path) -> list[tuple[dict[str, Any], str]]:
    """Every ``*.yaml``/``*.yml`` in every ``roles.d/`` dir, lowest precedence first."""
    layers: list[tuple[dict[str, Any], str]] = []
    for directory in candidate_role_dirs(workspace):
        if not directory.is_dir():
            continue
        for path in sorted(p for p in directory.iterdir() if p.suffix.lower() in {".yaml", ".yml"} and p.is_file()):
            data = _load_yaml_cached(path) or {}
            if isinstance(data, dict) and data:
                layers.append((expand_env(data), str(path)))
    return layers


# Parsing multi-MB role packs on every load_config() is wasteful (the server and
# the test suite call it constantly). Cache by (path, mtime, size) so an
# unchanged file is parsed once per process; any edit invalidates it.
_YAML_CACHE: dict[tuple[str, float, int], Any] = {}
_YAML_CACHE_MAX = 256


def _load_yaml_cached(path: Path) -> Any:
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime, st.st_size)
    if key in _YAML_CACHE:
        return _YAML_CACHE[key]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML ({exc})") from exc
    if len(_YAML_CACHE) >= _YAML_CACHE_MAX:
        _YAML_CACHE.clear()
    _YAML_CACHE[key] = data
    return data


def clear_yaml_cache() -> None:
    """Drop the parse cache (used after writing a pack so it is re-read fresh)."""
    _YAML_CACHE.clear()


def load_yaml_layers(filename: str, workspace: Path) -> tuple[dict[str, Any], list[Path]]:
    """Deep-merge every existing layer of ``filename``."""
    merged: dict[str, Any] = {}
    used: list[Path] = []
    for path in candidate_files(filename, workspace):
        if not path.is_file():
            continue
        data = _load_yaml_cached(path) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        merged = _deep_merge(merged, expand_env(data))
        used.append(path)
    return merged, used


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class ConfigError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# agents.yaml
# --------------------------------------------------------------------------- #


@dataclass
class AgentConfig:
    """One entry of ``agents.yaml`` = one callable model endpoint."""

    name: str
    provider: str = "anthropic"          # anthropic | openai-compatible
    model: str = ""
    base_url: str | None = None
    api_key_env: str | None = None
    api_key: str | None = None
    max_tokens: int = 8192
    temperature: float | None = None
    timeout: float = 180.0
    supports_tools: bool = True
    tool_mode: str = "native"            # native | text | none
    fallback: str | None = None
    label: str = ""
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # -- credentials ------------------------------------------------------- #
    @property
    def key_family(self) -> str | None:
        """``gemini`` / ``deepseek`` / ``openrouter`` (or None for a local box)."""
        return keyring.family_for(self.provider, self.api_key_env)

    @property
    def key_env_names(self) -> tuple[str, ...]:
        return keyring.env_names(self.provider, self.api_key_env)

    @property
    def resolved_api_key(self) -> str | None:
        """Inline key > environment variable > `nexus keys set` store."""
        if self.api_key:
            return self.api_key
        value, _origin = keyring.key_from_env(self.provider, self.api_key_env)
        if value:
            return value
        family = self.key_family
        if not family:
            return None
        return keyring.stored_key(family) or None

    @property
    def key_source(self) -> str:
        """Where the key came from: ``env:GEMINI_API_KEY``, ``keys.json`` or ``""``."""
        return keyring.key_source(self.provider, self.api_key_env)

    @property
    def effective_base_url(self) -> str:
        """Configured ``base_url``, else the provider's public default."""
        if self.base_url:
            return str(self.base_url)
        spec = keyring.provider_spec(self.provider)
        return spec.base_url if spec else ""

    def missing_key_hint(self) -> str:
        family = self.key_family
        if family and family in keyring.PROVIDERS:
            return f"run `nexus keys set {family}` (or export {self.key_env_names[0]})"
        names = self.key_env_names
        return f"set {names[0]}" if names else "set an API key"

    def describe(self) -> str:
        return self.label or f"{self.name}: {self.provider}/{self.model}"


DEFAULT_AGENT_YAML: dict[str, Any] = {
    "agents": {
        "gemini": {
            "provider": "gemini",
            "model": "gemini-3.8-flash",
            "api_key_env": "GEMINI_API_KEY",
            "max_tokens": 8192,
            "fallback": "deepseek",
        },
        "deepseek": {
            "provider": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "DEEPSEEK_API_KEY",
            "max_tokens": 8192,
            "fallback": "openrouter",
        },
        "openrouter": {
            "provider": "openrouter",
            "model": "openrouter/auto",
            "api_key_env": "OPENROUTER_API_KEY",
            "max_tokens": 8192,
        },
    },
    "default_agent": "gemini",
}


def parse_agents(data: dict[str, Any]) -> dict[str, AgentConfig]:
    raw = data.get("agents") or {}
    if not isinstance(raw, dict):
        raise ConfigError("agents.yaml: `agents` must be a mapping of name -> config")
    agents: dict[str, AgentConfig] = {}
    for name, cfg in raw.items():
        cfg = dict(cfg or {})
        known = {
            "provider", "model", "base_url", "api_key_env", "api_key", "max_tokens",
            "temperature", "timeout", "supports_tools", "tool_mode", "fallback",
            "label", "description",
        }
        extra = {k: v for k, v in cfg.items() if k not in known}
        agents[str(name)] = AgentConfig(
            name=str(name),
            provider=str(cfg.get("provider", "anthropic")),
            model=str(cfg.get("model", "")),
            base_url=cfg.get("base_url"),
            api_key_env=cfg.get("api_key_env"),
            api_key=cfg.get("api_key"),
            max_tokens=int(cfg.get("max_tokens", 8192)),
            temperature=cfg.get("temperature"),
            timeout=float(cfg.get("timeout", 180.0)),
            supports_tools=bool(cfg.get("supports_tools", True)),
            tool_mode=str(cfg.get("tool_mode", "native")),
            fallback=cfg.get("fallback"),
            label=str(cfg.get("label", "")),
            description=str(cfg.get("description", "")),
            extra=extra,
        )
    return agents


# --------------------------------------------------------------------------- #
# roles.yaml
# --------------------------------------------------------------------------- #


def parse_roles(data: dict[str, Any], source: str = "") -> list[Role]:
    raw = data.get("roles") or {}
    roles: list[Role] = []
    if isinstance(raw, dict):
        items: list[tuple[str, Any]] = list(raw.items())
    elif isinstance(raw, list):
        items = [(str(r.get("name", f"role-{i}")), r) for i, r in enumerate(raw) if isinstance(r, dict)]
    else:
        raise ConfigError("roles.yaml: `roles` must be a mapping or a list")

    for name, cfg in items:
        cfg = dict(cfg or {})
        role_name = sanitize_role_name(str(cfg.get("name", name)))
        if not role_name:
            continue
        keywords = [Keyword.parse(k) for k in (cfg.get("keywords") or [])]
        aliases = [sanitize_role_name(a) for a in (cfg.get("aliases") or []) if sanitize_role_name(a)]
        persona = str(cfg.get("persona") or cfg.get("system_prompt") or "").strip()
        roles.append(
            Role(
                name=role_name,
                persona=persona,
                agent=str(cfg.get("agent", "") or ""),
                category=str(cfg.get("category", "general")),
                title=str(cfg.get("title", "") or ""),
                keywords=keywords,
                aliases=aliases,
                tools=cfg.get("tools"),
                focus=[str(f) for f in (cfg.get("focus") or [])],
                model=cfg.get("model"),
                temperature=cfg.get("temperature"),
                extra_instructions=str(cfg.get("instructions") or ""),
                source=source,
            )
        )
    return roles


@dataclass
class RoleRegistry:
    roles: dict[str, Role] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.roles)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self._resolve(name) is not None

    def _resolve(self, name: str) -> Role | None:
        clean = sanitize_role_name(name)
        if clean in self.roles:
            return self.roles[clean]
        for role in self.roles.values():
            if clean in role.aliases:
                return role
        return None

    def get(self, name: str, default: str = "general") -> Role:
        role = self._resolve(name)
        if role is not None:
            return role
        fallback = self.roles.get(default)
        if fallback is None:
            fallback = Role(name=default, persona="You are a generalist senior software engineer.")
        return fallback

    def add(self, role: Role, *, override: bool = True) -> None:
        if role.name in self.roles and not override:
            return
        self.roles[role.name] = role

    def names(self) -> list[str]:
        return sorted(self.roles)

    def by_category(self) -> dict[str, list[Role]]:
        out: dict[str, list[Role]] = {}
        for role in self.roles.values():
            out.setdefault(role.category or "general", []).append(role)
        for value in out.values():
            value.sort(key=lambda r: r.name)
        return dict(sorted(out.items()))

    def search(self, needle: str) -> list[Role]:
        needle = needle.lower().strip()
        scored: list[tuple[int, Role]] = []
        for role in self.roles.values():
            score = 0
            if needle in role.name:
                score += 5
            if needle in (role.title or "").lower():
                score += 3
            if needle in (role.category or "").lower():
                score += 2
            if any(needle in k.text for k in role.keywords):
                score += 2
            if needle in (role.persona or "").lower():
                score += 1
            if score:
                scored.append((score, role))
        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        return [role for _, role in scored]


def _role_from_dict(name: str, cfg: dict[str, Any]) -> Role:
    return parse_roles({"roles": {name: cfg}})[0]


def builtin_general_role() -> Role:
    return _role_from_dict(
        "general",
        {
            "title": "Generalist Engineer",
            "category": "general",
            "agent": "",
            "persona": "You are a generalist senior software engineer.",
            "keywords": [],
        },
    )


def build_registry(role_data_layers: list[tuple[dict[str, Any], str]]) -> RoleRegistry:
    registry = RoleRegistry()
    for data, source in role_data_layers:
        for role in parse_roles(data, source=source):
            if not role.agent:
                role.agent = ""  # resolved later against default_agent
            registry.add(role)
    if "general" not in registry.roles:
        registry.add(builtin_general_role())
    return registry


# --------------------------------------------------------------------------- #
# Top-level config
# --------------------------------------------------------------------------- #


@dataclass
class Settings:
    """Behaviour knobs (``settings:`` block in agents.yaml + NEXUS_* env)."""

    auto_route: bool = True
    router_min_score: float = 1.0
    router_llm_fallback: bool = True
    router_agent: str = ""              # empty => use default_agent
    max_turns: int = 24
    auto_yes: bool = False
    allow_dangerous: bool = False
    command_timeout: float = 120.0
    max_output_chars: int = 24_000
    max_read_chars: int = 120_000
    context_char_budget: int = 240_000
    memory_enabled: bool = True
    memory_dir: str = ".nexus/memory"
    memory_max_chars: int = 4_000
    branch_prefix: str = "feat"
    auto_branch: bool = True
    auto_commit_message: bool = True
    pr_template: str = ".github/PULL_REQUEST_TEMPLATE.md"
    always_allow_tools: list[str] = field(default_factory=list)
    never_allow_tools: list[str] = field(default_factory=list)
    blocked_command_patterns: list[str] = field(default_factory=list)
    workspace: Path = field(default_factory=Path.cwd)

    @classmethod
    def from_dict(cls, data: dict[str, Any], workspace: Path) -> Settings:
        cfg = data.get("settings") or {}
        if not isinstance(cfg, dict):
            raise ConfigError("agents.yaml: `settings` must be a mapping")
        kwargs: dict[str, Any] = {}
        for key, value in cfg.items():
            if key in cls.__dataclass_fields__ and key != "workspace":
                kwargs[key] = value
        settings = cls(**kwargs)
        settings.workspace = Path(workspace).resolve()
        settings._apply_env()
        return settings

    def _apply_env(self) -> None:
        def _bool(name: str, current: bool) -> bool:
            raw = os.environ.get(name)
            if raw is None:
                return current
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        self.auto_route = _bool("NEXUS_AUTO_ROUTE", self.auto_route)
        self.router_llm_fallback = _bool("NEXUS_ROUTER_LLM", self.router_llm_fallback)
        self.auto_yes = _bool("NEXUS_YES", self.auto_yes)
        self.allow_dangerous = _bool("NEXUS_ALLOW_DANGEROUS", self.allow_dangerous)
        self.memory_enabled = _bool("NEXUS_MEMORY", self.memory_enabled)
        self.auto_branch = _bool("NEXUS_AUTO_BRANCH", self.auto_branch)
        for name, attr in (
            ("NEXUS_MAX_TURNS", "max_turns"),
            ("NEXUS_ROUTER_MIN_SCORE", "router_min_score"),
            ("NEXUS_COMMAND_TIMEOUT", "command_timeout"),
        ):
            raw = os.environ.get(name)
            if raw:
                try:
                    setattr(self, attr, type(getattr(self, attr))(float(raw) if "." in raw else int(raw)))
                except (TypeError, ValueError):
                    pass
        if os.environ.get("NEXUS_ROUTER_AGENT"):
            self.router_agent = os.environ["NEXUS_ROUTER_AGENT"]


@dataclass
class Pipeline:
    name: str
    description: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class NexusConfig:
    agents: dict[str, AgentConfig]
    roles: RoleRegistry
    settings: Settings
    default_agent: str = "gemini"
    pipelines: dict[str, Pipeline] = field(default_factory=dict)
    sources: dict[str, list[Path]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    # -- lookups ----------------------------------------------------------- #
    def agent(self, name: str | None) -> AgentConfig:
        target = name or self.default_agent
        if target in self.agents:
            return self.agents[target]
        if self.default_agent in self.agents:
            return self.agents[self.default_agent]
        raise ConfigError(
            f"agent '{target}' is not defined in agents.yaml (known: {', '.join(sorted(self.agents)) or 'none'})"
        )

    def agent_for_role(self, role: Role) -> AgentConfig:
        return self.agent(role.agent or self.default_agent)

    def role(self, name: str) -> Role:
        return self.roles.get(name, default=self.default_role_name())

    def default_role_name(self) -> str:
        return "general" if "general" in self.roles.roles else self.roles.names()[0]

    def pipeline(self, name: str) -> Pipeline | None:
        return self.pipelines.get(name)

    def key_families(self) -> list[str]:
        """Provider families this config can actually call, in priority order."""
        order = [self.default_agent, self.settings.router_agent, *sorted(self.agents)]
        families: list[str] = []
        for name in order:
            cfg = self.agents.get(name)
            if cfg is None:
                continue
            family = cfg.key_family
            if family and family not in families:
                families.append(family)
        return families

    def agents_missing_keys(self) -> list[AgentConfig]:
        """Agents that will fail before a fallback takes over."""
        missing: list[AgentConfig] = []
        for name in sorted(self.agents):
            cfg = self.agents[name]
            if cfg.key_family and not cfg.resolved_api_key:
                missing.append(cfg)
        return missing


def load_config(workspace: Path | str | None = None, *, load_pipelines: bool = True) -> NexusConfig:
    ws = Path(workspace or os.environ.get("NEXUS_WORKSPACE") or Path.cwd()).resolve()

    agent_data, agent_files = load_yaml_layers(DEFAULT_AGENTS_FILE, ws)
    if not agent_data:
        agent_data = dict(DEFAULT_AGENT_YAML)
    agents = parse_agents(agent_data)
    default_agent = str(agent_data.get("default_agent") or os.environ.get("NEXUS_DEFAULT_AGENT") or "gemini")
    if default_agent not in agents and agents:
        default_agent = sorted(agents)[0]

    role_layers: list[tuple[dict[str, Any], str]] = []
    role_files: list[Path] = []
    for path in candidate_files(DEFAULT_ROLES_FILE, ws):
        if path.is_file():
            parsed = _load_yaml_cached(path) or {}
            role_layers.append((expand_env(parsed), str(path)))
            role_files.append(path)
    # drop-in packs (roles.d/*.yaml) merge on top - `nexus addagent` writes here
    for data, source in load_role_packs(ws):
        role_layers.append((data, source))
        role_files.append(Path(source))
    registry = build_registry(role_layers)
    registry.sources = [source for _, source in role_layers]

    settings_raw = {**agent_data}
    settings = Settings.from_dict(settings_raw, ws)

    pipelines: dict[str, Pipeline] = {}
    pipeline_files: list[Path] = []
    if load_pipelines:
        pipe_data, pipeline_files = load_yaml_layers(DEFAULT_PIPELINES_FILE, ws)
        for name, cfg in (pipe_data.get("pipelines") or {}).items():
            cfg = dict(cfg or {})
            pipelines[str(name)] = Pipeline(
                name=str(name),
                description=str(cfg.get("description", "")),
                steps=[dict(s) for s in (cfg.get("steps") or []) if isinstance(s, dict)],
            )

    # roles without an explicit agent inherit the default
    for role in registry.roles.values():
        if not role.agent:
            role.agent = default_agent

    return NexusConfig(
        agents=agents,
        roles=registry,
        settings=settings,
        default_agent=default_agent,
        pipelines=pipelines,
        sources={
            "agents": agent_files,
            "roles": role_files,
            "pipelines": pipeline_files,
        },
        raw=agent_data,
    )
