# Nexus Agent — Release Checklist

## Pre-release verification

### Tests
- [ ] `pytest -q` — all tests pass (except pre-existing known failures)
- [ ] `ruff check nexus tests` — clean
- [ ] `nexus --version` → `nexus 0.3.0`
- [ ] `nexus doctor` — runs without errors, shows all component states
- [ ] `nexus voice bench` — all latency budgets met

### CLI commands
- [ ] `nexus stop --all` → latches emergency stop, exit 0
- [ ] `nexus stop --json` → `{"stopped": true, "resume": "nexus resume"}`
- [ ] `nexus resume` → clears emergency stop, exit 0
- [ ] `nexus resume --json` → `{"stopped": false}`

### End-to-end scenarios (with fakes, no network/GPU/mic/API keys)
- [ ] Voice: 'build a landing page for X' → plan → approve → parallel team → critic → done
- [ ] Email injection attempt is ignored (untrusted data, never executed)
- [ ] Crash mid-mission → provider error handled, recovery works
- [ ] Emergency stop blocks all tool actions; resume clears it
- [ ] Mixed Bangla+English command understood and answered
- [ ] Morning brief OFF by default; enabling via `proactive.yaml` is explicit

### Optional component checks (`nexus doctor`)
- [ ] `opt:voice` — voice.yaml OFF by default
- [ ] `opt:chief` — chief.yaml OFF by default
- [ ] `opt:proactive` — proactive.yaml OFF by default
- [ ] `opt:vision` — vision.yaml OFF by default
- [ ] `opt:integrations` — mcp.yaml integrations OFF by default
- [ ] `security:autonomy` — level=L1 (default)
- [ ] `security:second_factor` — OFF (default)
- [ ] `security:defensive` — OFF (default)
- [ ] `security:runtime` — OFF (default)

### CI
- [ ] `.github/workflows/ci.yml` matrix includes Python 3.10, 3.11, 3.12, 3.13
- [ ] Eval job runs `nexus eval run --format json --fail-on-regression`

### Files
- [ ] `jarvis-mode.example/voice.yaml` — conservative example
- [ ] `jarvis-mode.example/persona.yaml` — conservative example
- [ ] `jarvis-mode.example/autonomy.yaml` — conservative example
- [ ] `jarvis-mode.example/permissions.yaml` — conservative example
- [ ] `jarvis-mode.example/security.yaml` — conservative example

### Docs
- [ ] `docs/proactive.md` — proactive assistant guide
- [ ] `docs/integrations-personal.md` — personal data integrations guide
- [ ] `docs/environment.md` — environment control guide
- [ ] `docs/security-assistant.md` — security assistant guide
- [ ] README 'Jarvis mode quick start' section
- [ ] Existing docs verified: `docs/voice.md`, `docs/chief.md`, `docs/vision.md`

## Release approval

All new capabilities are OFF by default and enabled only through operator-owned YAML files.
`--yes` does not bypass `permissions.yaml`. No secrets in code, YAML, logs, or git.

---

# Known Limitations

## Emergency stop
- Hotkey (Ctrl+Alt+Pause) is Windows-only; other platforms use CLI/voice/HUD/Telegram
- `nexus resume` unlatches the stop but does not auto-resume missions (operator approval required)
- Emergency stop pauses the Jarvis daemon best-effort; the latch itself is the hard stop

## Voice stop phrase
- 'Nexus, stop everything' is matched locally in the voice pipeline before any LLM call
- Works when LLM is unreachable, but requires `voice.yaml enabled: true`
- Confirmation is spoken (EN/BN); resume requires `nexus resume`

## Provider failover + timeouts
- Per-step timeouts run each provider call in a daemon thread; timed-out threads may continue but won't block process exit
- Failover follows the agent's configured `fallback:` chain (default: gemini→deepseek→openrouter)
- Offline mode (`profile: offline`) refuses cloud provider construction; local providers work

## End-to-end scenarios
- All E2E tests use fakes/mocks — no network, GPU, microphone, or API keys required
- Voice tests use `SimpleNamespace` config instead of full `VoiceConfig` dataclass
- Crash recovery tested at the provider registry level, not full `agent.run()` flow

## `nexus doctor`
- Component checks read YAML files directly; they don't verify the component can actually run
- Security posture checks are informational (ok=None) — they show state, not pass/fail
- Agent health checks require API keys or local endpoints; missing keys are warnings, not failures

## CI
- Python 3.13 matrix added; optional dependencies may have different availability per version
- Eval job runs on Python 3.11 only (existing behavior)
- Live GitHub/Windows/desktop/Docker acceptance is not claimed — scripted tests only

## Docs
- `docs/proactive.md`, `docs/integrations-personal.md`, `docs/environment.md`, `docs/security-assistant.md` are new
- Existing docs (`voice.md`, `chief.md`, `vision.md`) verified current
- `jarvis-mode.example/` configs are conservative starting points — review before use
