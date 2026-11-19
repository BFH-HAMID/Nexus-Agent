"""Shared helpers for the Task 18-30 feature-package tests (all offline)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class MemoryKeyring:
    """In-memory stand-in for the OS keyring used by AES-GCM sealed state."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, value: str) -> None:
        self.data[(service, username)] = value

    def delete_password(self, service: str, username: str) -> None:
        self.data.pop((service, username), None)


class BrokenKeyring:
    """A keyring backend that is installed but unusable (headless box, locked wallet)."""

    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError("no keyring backend")

    def set_password(self, service: str, username: str, value: str) -> None:
        raise RuntimeError("no keyring backend")


def _deep_update(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def write_policy(root: Path, name: str, **settings: Any) -> Path:
    """Write an operator policy file: ``schema: 1`` plus the given settings (``enabled`` defaults to true)."""
    data: dict[str, Any] = {"schema": 1, "enabled": True}
    _deep_update(data, settings)
    path = Path(root) / name
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def write_memory_policy(root: Path, **overrides: Any) -> Path:
    """An enabled ``memory.yaml`` (profile + local-hashed vector memory, plaintext unless overridden)."""
    cfg: dict[str, Any] = {
        "schema": 1,
        "enabled": True,
        "vector_memory": {
            "enabled": True, "max_entries": 500, "max_chars": 200000, "compaction_threshold": 250,
            "compaction_keep_recent": 50, "embedding_provider": "local-hashed", "embedding_dimensions": 256,
            "top_k": 5,
        },
        "profile": {"enabled": True, "require_confirmation_for_inferred": True, "block_sensitive_categories": True},
        "context_awareness": {
            "enabled": True, "include_project": True, "include_mission": True, "include_recent_turns": 10,
            "include_time_of_day": True, "include_active_window": False,
        },
        "privacy": {"encrypt_at_rest": False, "voice_privacy_mode": False},
    }
    _deep_update(cfg, overrides)
    path = Path(root) / "memory.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


def tool_registry(root: Path, callback=None, *, mode: str = "auto", config: Any = None, providers: Any = None):
    """``(ctx, registry)`` wired like production: ApprovalGate + Permissions + build_tools."""
    from nexus.permissions import Permissions
    from nexus.tools import ApprovalGate, PathGuard, ToolContext, build_tools

    gate = ApprovalGate(mode=mode, callback=callback)
    gate.permissions = Permissions(root)
    ctx = ToolContext(root, PathGuard(root), gate, config=config, providers=providers)
    return ctx, build_tools(ctx)


def scripted_agents(config: Any, *fakes: Any, cloud: tuple[str, ...] = ()) -> Any:
    """Register scripted providers as agents and return the provider registry.

    Each gets a loopback endpoint, which is what "local agent" means to the
    feature policies (``allow_cloud: false``).  Names listed in ``cloud`` get a
    hosted endpoint instead.
    """
    from dataclasses import replace

    from .conftest import fake_registry

    providers = fake_registry(*fakes, config=config)
    for fake in fakes:
        url = "https://api.example.com/v1" if fake.name in cloud else "http://127.0.0.1:9/v1"
        agent = replace(providers.agents[fake.name], provider="openai-compatible", base_url=url)
        providers.agents[fake.name] = config.agents[fake.name] = agent
    return providers


def run_cli(argv: list[str]) -> int:
    """Run ``nexus <argv>`` in-process and return its exit code."""
    from nexus.cli import main

    try:
        return int(main(argv) or 0)
    except SystemExit as exc:  # argparse errors
        return int(exc.code or 0)
