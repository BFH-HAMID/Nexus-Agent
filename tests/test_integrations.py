"""Integration discovery is offline/scripted in tests."""
import httpx

from nexus import integrations
from nexus.config import load_config


def test_ollama_discovery(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "localhost:11434/v1/")
    def get(url, timeout):
        assert url == "http://localhost:11434/api/tags"
        return httpx.Response(200, request=httpx.Request("GET", url),
                              json={"models": [{"name": "custom-dolphin:latest"}, {"name": "custom-dolphin:latest"}]})
    monkeypatch.setattr(httpx, "get", get)
    assert integrations.ollama_models()["models"] == ["custom-dolphin:latest"]


def test_ollama_down(monkeypatch):
    def get(*args, **kwargs):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "get", get)
    result = integrations.ollama_models()
    assert not result["connected"]
    assert result["models"] == []
    assert "ollama serve" in result["error"]


def test_selected_model_pins_roles_and_router(repo):
    config = load_config(repo)
    integrations.use_local_model(config, "my-custom-model:7b")
    name = config.default_agent
    assert config.agents[name].model == "my-custom-model:7b"
    assert config.agents[name].tool_mode == "text"
    assert config.agents[name].fallback is None
    assert all(r.agent == name for r in config.roles.roles.values())
    assert config.settings.router_agent == config.settings.learning_agent == name
    assert load_config(repo).default_agent != name  # no persistent/global mutation


def test_github_missing_cli(monkeypatch, repo):
    monkeypatch.setattr(integrations.shutil, "which", lambda _: None)
    result = integrations.github_status(repo)
    assert not result["connected"]
    assert "--web" in result["instructions"][0]
