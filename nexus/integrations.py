"""Local integration discovery. Credentials remain in Ollama / GitHub CLI."""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

import httpx

from .config import AgentConfig, NexusConfig


def ollama_url() -> str:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    if "://" not in host:
        host = "http://" + host
    return host.removesuffix("/v1")


def ollama_models() -> dict[str, Any]:
    url = ollama_url()
    try:
        response = httpx.get(url + "/api/tags", timeout=5)
        response.raise_for_status()
        models = sorted({m["name"] for m in response.json().get("models", []) if isinstance(m.get("name"), str)})
        return {"connected": True, "url": url, "models": models}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return {"connected": False, "url": url, "models": [],
                "error": f"Ollama unavailable ({type(exc).__name__}). Run ollama serve on the machine running Nexus."}


def use_local_model(config: NexusConfig, model: str) -> None:
    """Pin the entire run (including routing/learning/peers) locally, no cloud fallback."""
    name = "selected-ollama"
    config.agents[name] = AgentConfig(
        name=name, provider="openai-compatible", model=model,
        base_url=ollama_url() + "/v1", api_key="ollama", timeout=420,
        max_tokens=4096, tool_mode="text", optional=True,
        description="Explicitly selected local Ollama model (no hosted fallback)",
    )
    config.default_agent = name
    config.settings.router_agent = name
    config.settings.learning_agent = name
    for role in config.roles.roles.values():
        role.agent = name


def github_status(workspace) -> dict[str, Any]:
    instructions = ["gh auth login --hostname github.com --web", "gh auth setup-git", "gh auth status"]
    if not shutil.which("gh"):
        return {"installed": False, "connected": False, "instructions": instructions,
                "message": "Install GitHub CLI, then run these commands in the Nexus terminal."}
    try:
        result = subprocess.run(["gh", "api", "user", "--jq", ".login"], cwd=workspace,
                                capture_output=True, text=True, timeout=10)
        login = result.stdout.strip() if result.returncode == 0 else ""
        return {"installed": True, "connected": bool(login), "login": login,
                "instructions": instructions,
                "message": "Connected via GitHub CLI" if login else "Not authenticated or GitHub is unreachable. Run gh auth status."}
    except (OSError, subprocess.TimeoutExpired):
        return {"installed": True, "connected": False, "instructions": instructions,
                "message": "GitHub check failed. Run gh auth status in the Nexus terminal."}
