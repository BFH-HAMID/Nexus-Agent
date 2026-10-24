"""API keys from the terminal: store, env precedence, provider wiring.

No network is touched here - providers are exercised through injected
transports, exactly like the rest of the suite.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus import keys as keyring
from nexus.config import AgentConfig, load_config


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Never touch the developer's real ~/.config/nexus/keys.json."""
    store = tmp_path / "nexus-home" / "keys.json"
    monkeypatch.setenv("NEXUS_KEYS_FILE", str(store))
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return store


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #


def test_save_and_read_a_key(isolated_store):
    path = keyring.save_key("gemini", "  AIzaTESTKEY1234567890  ")
    assert path == isolated_store
    assert json.loads(path.read_text())["keys"]["gemini"] == "AIzaTESTKEY1234567890"
    assert keyring.get_key("gemini") == "AIzaTESTKEY1234567890"
    assert keyring.stored_key("google") == "AIzaTESTKEY1234567890"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions only")
def test_store_is_owner_only(isolated_store):
    keyring.save_key("deepseek", "sk-test-1234567890abcdef")
    assert isolated_store.stat().st_mode & 0o077 == 0
    assert keyring.store_is_private() is True


def test_delete_key(isolated_store):
    keyring.save_key("openrouter", "sk-or-test-1234567890")
    assert isolated_store.exists()
    assert keyring.delete_key("openrouter") is True
    assert keyring.delete_key("openrouter") is False
    assert not isolated_store.exists()
    assert keyring.get_key("openrouter") == ""


def test_corrupt_store_reads_as_empty(isolated_store):
    isolated_store.parent.mkdir(parents=True)
    isolated_store.write_text("{not json", encoding="utf-8")
    assert keyring.load_store() == {}
    assert keyring.get_key("gemini") == ""


def test_env_beats_the_store(isolated_store, monkeypatch):
    keyring.save_key("gemini", "AIzaSTOREDKEY123456")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFROMENV123456789")
    assert keyring.get_key("gemini") == "AIzaFROMENV123456789"
    assert keyring.key_source("gemini") == "env:GEMINI_API_KEY"


def test_aliases_and_env_names():
    assert keyring.canonical("Google") == "gemini"
    assert keyring.canonical("open-router") == "openrouter"
    assert keyring.canonical("GEMINI_API_KEY") == "gemini"
    assert keyring.canonical("nope") is None
    assert keyring.env_names("gemini")[0] == "GEMINI_API_KEY"
    assert keyring.env_names("openai-compatible", "MY_KEY")[0] == "MY_KEY"


def test_mask_never_leaks_the_whole_key():
    masked = keyring.mask("AIzaTESTKEY1234567890")
    assert masked.startswith("AIzaTE") and masked.endswith("7890")
    assert "TESTKEY" not in masked
    assert keyring.mask("") == ""


def test_normalize_and_shape_hints():
    assert keyring.normalize_key(' "AIzaX" ') == "AIzaX"
    assert keyring.normalize_key("Bearer sk-or-abc123") == "sk-or-abc123"
    assert "too short" in keyring.looks_like_key("gemini", "abc")
    assert keyring.looks_like_key("gemini", "AIza" + "x" * 30) == ""
    assert keyring.looks_like_key("openrouter", "AIza" + "x" * 30)  # warns, does not raise


# --------------------------------------------------------------------------- #
# config wiring
# --------------------------------------------------------------------------- #


def _cfg(**kwargs) -> AgentConfig:
    defaults = dict(name="gemini", provider="gemini", model="gemini-3.8-flash")
    defaults.update(kwargs)
    return AgentConfig(**defaults)


def test_agent_reads_the_terminal_store(isolated_store):
    cfg = _cfg(api_key_env="GEMINI_API_KEY")
    assert cfg.resolved_api_key is None
    keyring.save_key("gemini", "AIzaSTOREDKEY123456")
    assert cfg.resolved_api_key == "AIzaSTOREDKEY123456"
    assert cfg.key_source == str(isolated_store)
    assert cfg.key_family == "gemini"


def test_agent_env_key_wins(isolated_store, monkeypatch):
    keyring.save_key("deepseek", "sk-stored-1234567890")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-1234567890")
    cfg = _cfg(name="deepseek", provider="deepseek")
    assert cfg.resolved_api_key == "sk-env-1234567890"
    assert cfg.key_source == "env:DEEPSEEK_API_KEY"


def test_effective_base_url_defaults_per_provider():
    assert _cfg().effective_base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert _cfg(provider="deepseek").effective_base_url == "https://api.deepseek.com/v1"
    assert _cfg(provider="openrouter").effective_base_url == "https://openrouter.ai/api/v1"
    assert _cfg(provider="openai-compatible", base_url="http://box:8000/v1").effective_base_url == (
        "http://box:8000/v1"
    )
    assert _cfg(provider="openai-compatible").effective_base_url == ""


def test_missing_key_hint_points_at_the_cli():
    assert "nexus keys set gemini" in _cfg().missing_key_hint()


def test_bundled_config_only_needs_the_three_api_providers(repo):
    config = load_config(repo)
    providers = {cfg.provider for cfg in config.agents.values()}
    assert providers <= {"gemini", "deepseek", "openrouter", "openai-compatible", "anthropic"}
    assert {"gemini", "deepseek", "openrouter"} <= providers
    assert config.default_agent == "gemini"
    # every agent that needs a key points at a family we can store
    for cfg in config.agents.values():
        if cfg.key_family:
            assert cfg.key_family in keyring.PROVIDERS
            assert cfg.resolved_api_key is None  # no keys in the test environment


def test_every_agent_can_reach_a_working_fallback(repo):
    """A single configured key must be enough to run the whole roster."""
    config = load_config(repo)
    expected = {"gemini", "deepseek", "openrouter"}
    for name in config.agents:
        seen: list[str] = []
        current = config.agents[name]
        while current.fallback and current.fallback not in seen:
            seen.append(current.fallback)
            assert current.fallback in config.agents, f"{name}: unknown fallback"
            current = config.agents[current.fallback]
        families = {config.agents[n].key_family for n in [name, *seen]}
        assert expected <= families, f"{name}: fallback chain only covers {families}"


# --------------------------------------------------------------------------- #
# provider endpoints
# --------------------------------------------------------------------------- #


def _provider(name="gemini", *, transport=None, **kwargs):
    from nexus.providers import OpenAICompatProvider

    return OpenAICompatProvider(_cfg(name=name, **kwargs), transport=transport)


def test_provider_urls_for_each_vendor():
    assert _provider("gemini", provider="gemini").chat_url == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert _provider("deepseek", provider="deepseek").models_url == "https://api.deepseek.com/v1/models"
    assert _provider("openrouter", provider="openrouter").chat_url == (
        "https://openrouter.ai/api/v1/chat/completions"
    )
    # hand-pasted full URL still works
    full = "https://example.test/v1/chat/completions"
    assert _provider("x", provider="openai-compatible", base_url=full).chat_url == full
    assert _provider("y", provider="openai-compatible", base_url="http://localhost:11434/v1").models_url == (
        "http://localhost:11434/v1/models"
    )


def test_openrouter_gets_attribution_headers(isolated_store):
    keyring.save_key("openrouter", "sk-or-1234567890abcdef")
    headers = _provider("openrouter", provider="openrouter")._headers()
    assert headers["Authorization"] == "Bearer sk-or-1234567890abcdef"
    assert headers["X-Title"] == "Nexus Agent"


def test_a_hosted_provider_without_a_key_fails_fast(isolated_store):
    from nexus.types import ProviderError

    provider = _provider("gemini", provider="gemini")
    with pytest.raises(ProviderError) as exc:
        provider.complete("sys", [])
    assert "nexus keys set gemini" in str(exc.value)
    ok, detail = provider.health()
    assert ok is False and "no API key" in detail


def test_local_endpoints_do_not_need_a_key():
    provider = _provider("local", provider="openai-compatible", base_url="http://127.0.0.1:11434/v1")
    provider._require_key()  # no raise
    assert provider.is_local is True


class _Transport:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload or {"data": [{"id": "models/gemini-3.8-flash"}]}
        self.calls: list[tuple[str, str]] = []

    def get(self, url, headers=None):  # noqa: D401 - tiny stub
        self.calls.append(("GET", url))
        return SimpleNamespace(status_code=self.status, text="", json=lambda: self.payload)

    def post(self, url, json=None, headers=None):  # pragma: no cover - not used here
        return SimpleNamespace(status_code=self.status, text="", json=lambda: self.payload)


def test_health_lists_models_and_strips_the_models_prefix(isolated_store):
    keyring.save_key("gemini", "AIzaHEALTHY1234567890")
    transport = _Transport()
    provider = _provider("gemini", provider="gemini", transport=transport)
    ok, detail = provider.health()
    assert ok is True
    assert "gemini-3.8-flash" in detail or "1 model" in detail
    assert provider.list_models() == ["gemini-3.8-flash"]
    assert transport.calls[0][1].endswith("/v1beta/openai/models")


def test_health_reports_a_rejected_key(isolated_store):
    keyring.save_key("gemini", "AIzaBAD1234567890")
    provider = _provider("gemini", provider="gemini", transport=_Transport(status=401))
    ok, detail = provider.health()
    assert ok is False
    assert "rejected" in detail and "nexus keys set gemini" in detail


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def run_cli(argv, capsys):
    import nexus.cli as cli

    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out + out.err


def test_cli_keys_list_shows_all_three_providers(capsys):
    code, out = run_cli(["keys"], capsys)
    assert code == 0
    for family in ("gemini", "deepseek", "openrouter"):
        assert family in out
    assert "no .env file needed" in out
    assert "run: nexus keys set gemini" in out


def test_cli_keys_set_then_list_then_remove(isolated_store, capsys):
    code, out = run_cli(["keys", "set", "gemini", "--key", "AIzaCLI1234567890abc"], capsys)
    assert code == 0 and "saved gemini key" in out
    assert keyring.stored_key("gemini") == "AIzaCLI1234567890abc"

    code, out = run_cli(["keys", "--json"], capsys)
    payload = json.loads(out)
    rows = {row["provider"]: row for row in payload["providers"]}
    assert rows["gemini"]["configured"] is True
    assert "AIzaCLI" not in out  # masked only

    code, out = run_cli(["keys", "remove", "gemini"], capsys)
    assert code == 0 and keyring.stored_key("gemini") == ""


def test_cli_keys_set_session_only_does_not_write(isolated_store, capsys):
    try:
        code, out = run_cli(["keys", "set", "deepseek", "--key", "sk-session-1234567890", "--session"], capsys)
        assert code == 0
        assert not isolated_store.exists()
        assert os.environ["DEEPSEEK_API_KEY"] == "sk-session-1234567890"
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)


def test_cli_keys_test_without_a_key_tells_you_what_to_do(capsys):
    code, out = run_cli(["keys", "test"], capsys)
    assert code == 1
    assert "nexus keys set gemini" in out


def test_cli_agents_reports_key_state(repo, isolated_store, capsys):
    code, out = run_cli(["agents", "-C", str(repo)], capsys)
    assert code == 0
    assert "https://generativelanguage.googleapis.com/v1beta/openai" in out
    assert "nexus keys set gemini" in out


def test_cli_init_never_writes_a_dotenv(tmp_path, capsys):
    target = tmp_path / "proj"
    target.mkdir()
    code, out = run_cli(["init", "-C", str(target)], capsys)
    assert code == 0
    assert not (target / ".env").exists()
    assert "nexus keys set" in out


def test_prompt_command_works_without_any_key(repo, capsys):
    """Read-only commands never prompt for a key (no TTY in tests anyway)."""
    code, out = run_cli(["prompt", "backend", "-C", str(repo)], capsys)
    assert code == 0
    assert "# Active role: backend" in out


def test_store_path_honours_nexus_home(monkeypatch, tmp_path):
    monkeypatch.delenv("NEXUS_KEYS_FILE", raising=False)
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "nx"))
    assert keyring.store_path() == tmp_path / "nx" / "keys.json"
    assert Path(keyring.store_path()).parent.name == "nx"
