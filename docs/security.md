# Security model & responsible use

Nexus runs an LLM with shell and git access inside your repository. This page is the honest
account of what protects you, what does not, and where the offensive-security roles fit.

## Threat model

| Risk | Mitigation in Nexus | Residual risk |
| --- | --- | --- |
| Agent deletes or overwrites something important | approval gate, `danger` class for deletes, diff in every `write_file` result, git as the undo button, optional `second_factor:` (PIN or Telegram) | `--yes` removes the human from clean workspace writes when `default: AUTO_ALLOW`; uncommitted work is not recoverable |
| Agent runs a destructive shell command | deny-list regexes (`rm -rf /`, `mkfs`, `dd of=/dev/`, fork bomb, `git push --force`, `curl \| sh`, `npm publish`, `drop database`, …), optional Docker sandbox (`shell_backend: docker`) | a determined model can phrase damage differently on host backend; use `shell_backend: docker` with a digest-pinned image for untrusted code |
| Agent escapes the workspace | `PathGuard` + `Permissions.path` reject `..` escapes, symlinks, hardlinks to `.env`/`permissions.yaml`, and private files | `--allow-outside` relaxes `PathGuard` when `permissions.yaml` is absent |
| Agent rewrites shared history | `git push --force` denied; `gh_merge_pr` always prompts even under `--yes` | you can still approve it |
| Secret exfiltration | API keys live outside the repo (environment, native OS keyring, or `~/.config/nexus/keys.json` mode `600`); `.env` is gitignored; `RedactingLogFilter`, `nexus/redaction.py`, and `nexus/audit.py` mask tokens/secrets before logs, tool outputs, and audit records | any tool with network access can be told to POST somewhere — review commands you approve |
| Runaway loop / token or cost spike | `limits:` in `permissions.yaml` (`actions_per_minute`, `daily_tokens`, `daily_cost_usd`) plus `costcenter` enforce caps | disabled by default until configured in `permissions.yaml` |
| Unattended server | `nexus serve` binds loopback by default; token auth when exposed; raises on `0.0.0.0` without `NEXUS_SERVE_TOKEN` | an exposed tokenless server = remote code execution as you |
| Prompt injection from files, web, email, vision, or MCP | untrusted delimiter wrapping (`<untrusted-data>`, `<untrusted-evidence>`), delimiter escaping, regex rule scanning (`nexus/untrusted.py`), and automatic **session taint escalation** that revokes `--yes` / `AUTO_ALLOW` / `session` approval on high-impact tools | a human can still explicitly approve a tainted action after reviewing the `[TAINTED SESSION]` warning |

## Hardened controls (opt-in via `permissions.yaml`)

All new runtime restrictions default to backwards-compatible settings (`default: ASK_EVERY_TIME`, `computer.shell_backend: host`, `second_factor.enabled: false`, `limits.enabled: false`). Enable stricter controls in `permissions.yaml`:

### 1. Prompt injection & taint escalation (`nexus/untrusted.py`)

* External and untrusted inputs (`read_file`, `gh`, `mcp_*` tools, `computer.browser`, `vision.describe`, `studio_execution.fetch_allowed`, and `assistant.UntrustedDocument` for web/email/PDF/Drive/Notion/Obsidian) are framed inside `<untrusted-data ...>` or `<untrusted-evidence ...>` boundaries with embedded tags escaped.
* Any read from an external source (`mcp_*`, `browser_action`, `gh`), out-of-workspace file, or any file/text matching prompt-injection rules (`override`, `exfiltrate`, `delimiter_escape`, `tool_coercion`, `hidden_directive`, `external_exfil`) marks the session **tainted**.
* Once a session is tainted, high-impact actions (`run_command`, `delete_file`, `write_file`, `edit_file`, `office_file`, `skill_manage`, `git`, `git_prepare`, `gh`, `gh_create_pr`, `gh_merge_pr`, `browser_action`, `vscode_open`, `project_action`, `mcp_*`) cannot use `--yes`, `AUTO_ALLOW`, `session`, or `always_allow` — they require fresh human confirmation with a `[TAINTED SESSION]` warning.

### 2. Docker sandbox for shell commands (`nexus/sandbox.py`, `nexus/computer.py`)

Set `computer.shell_backend: docker` with a **digest-pinned** image (`repo@sha256:<64-hex>`) and optional `sandbox:` policy:

```yaml
computer:
  shell_backend: docker
  image: python@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
sandbox:
  network: none
  read_only: true
  memory: 512m
  cpus: 1
  pids_limit: 128
  timeout_seconds: 60
  workspace_only: true
```

Invariants enforced on container launch: `--pull=never`, `--network=none`, `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--user 65534:65534`, single read-only staged workspace copy mounted at `/workspace`, bounded `--memory`/`--cpus`/`--pids-limit`/timeout. Verify readiness anytime with:

```bash
nexus sandbox check
```

### 3. Secrets, OS keyring, and log redaction (`nexus/redaction.py`, `nexus/keys.py`)

* Secrets are resolved from environment variables / `.env` (gitignored), unlocked native OS keyring (`SecretService`, macOS Keychain, Windows Credential Locker via `nexus/redaction.py`), or `~/.config/nexus/keys.json` (`0600`, outside the repo).
* `RedactingLogFilter` is installed across `nexus`, `httpx`, `uvicorn`, and root loggers in CLI and server startup, stripping env secrets (`*_TOKEN`, `*_API_KEY`, `*_SECRET`, `*_PASSWORD`, `*_PIN`) and token/key/Bearer patterns (`ghp_`, `github_pat_`, `sk-`, `AIza`, `AKIA`, Slack `xox*`, Telegram bot tokens, PEM private keys, `Bearer ...`).

### 4. Action rate limits & daily token/cost caps (`nexus/limits.py`)

Configure optional `limits:` in `permissions.yaml`:

```yaml
limits:
  enabled: true
  actions_per_minute: 30
  daily_tokens: 500000
  daily_cost_usd: 5.0
```

* `actions_per_minute` uses a locked sliding-window state file under `$NEXUS_HOME/limits/`.
* `daily_tokens` and `daily_cost_usd` enforce workspace caps across `agent.run` turns and `jarvis` background job claims, emitting a `limit.exceeded` audit event when triggered.

### 5. Optional second-factor confirmation (`nexus/second_factor.py`, `nexus/telegram/bot.py`)

Configure `second_factor:` in `permissions.yaml` (PINs must live in the environment — e.g. `NEXUS_APPROVAL_PIN` — never in YAML or Git):

```yaml
second_factor:
  enabled: true
  method: pin        # pin | telegram | either
  pin_env: NEXUS_APPROVAL_PIN
  risks: [dangerous]
  tools: [gh_merge_pr]
  timeout_seconds: 60
```

* PIN verification uses constant-time `hmac.compare_digest` with a 5-failure / 5-minute lockout persisted under `$NEXUS_HOME/security/`.
* `telegram` / `either` sends a one-time `/confirm <challenge_id> YOUR_PIN` challenge tied to the exact action digest.

### 6. Immutable hash-chained audit trail (`nexus/audit.py`, `nexus/securitycenter.py`)

Every permission decision, tool execution, `mission.step`, `jarvis.claim`/`jarvis.finish`, `limit.exceeded`, and `security.second_factor` event is appended to a SHA-256 hash-chained log (`$NEXUS_HOME/security/.../audit.jsonl`) with ISO-8601 `when`, `who`, `what`, `target`, `result`, `risk`, and redacted `detail` (`redact_args` masks secrets and replaces raw file bodies/edits/base64 payloads with character counts and SHA-256 prefixes).

Verify chain integrity or inspect recent events:

```bash
nexus audit verify
nexus audit tail -n 20
```

### 7. Voice policy, persona, TTS redaction, & speaker gating (`voice.yaml`, `persona.yaml`, `nexus/voice/`)

* Both `voice.yaml` and `persona.yaml` are operator-owned (`enabled: false` by default) and protected by `Permissions.operator_control()`; agent file tools cannot edit either file, and inline secrets are rejected.
* `nexus/voice/tts.py::sanitize_tts_text()` and `nexus/voice/persona.py` redact API keys, tokens, and environment/keyring secrets before any text reaches TTS providers, HUD message feeds, or whitelisted Telegram delivery.
* Spoken approval intents (`"yes do it"`, `"no cancel"`, `"wait"` via `SpokenApprovalManager`) state the exact planned action and risk class, issue single-use `scope="once"` confirmations, and **never** weaken `permissions.yaml`, `FORCED_REVIEW`, Task 2 second-factor challenges (`second_factor:` on `DANGEROUS` actions), or `Risk.DANGEROUS` speaker verification (`nexus voice enroll`, `[voice-speaker]`). See [voice setup and boundaries](voice.md).

### 8. Tool risk classes, live status narration & notification routing (`autonomy.yaml`, `nexus/autonomy.py`, `nexus/notifications.py`)

* Every tool declares a risk level mapped into `SAFE` (read/search/draft/summarise → automatic), `MEDIUM` (bounded edits/tests/branches/draft PRs → automatic with post-action notice when allowed by `permissions.yaml`), and `DANGEROUS` (push main/deploy/send/delete/spend/change permissions/unknown tools → always fresh approval). Unknown tools always default to `DANGEROUS`, and `autonomy.yaml` refuses to downgrade `DANGEROUS` away from `fresh_approval`.
* `status_narration` (`silent` / `milestones` / `chatty`) and `notifications` (presence-aware routing across voice, whitelisted Telegram, and OS desktop notifications) are **off by default** in `autonomy.yaml` and apply `redaction.mask` + `studio.redact` before any output.

### 9. Scheduling, crash idempotency & watchdogs (`autonomy.yaml`, `nexus/scheduler.py`, `nexus/idempotency.py`, `nexus/watchdog.py`)

* `scheduler.enabled` and `watchdog.enabled` in `autonomy.yaml` are **off by default**, and `level` defaults to `L1`. Due schedules enqueue/plan tasks under the existing approval gate (`L1` never executes unattended).
* `IdempotencyLedger` (`$NEXUS_HOME/idempotency/`, 0600, FileLock) prevents re-running already-completed external actions within the same `(mission_id, step_id)` scope on crash recovery without bypassing first-time `ApprovalGate` checks.
* Watchdogs terminate stuck steps, infinite identical tool loops, and runaway cost cleanly (`status: failed` / `aborted_cost_cap`) rather than retrying indefinitely.

## Rules of thumb

1. **Commit before you run.** Nexus assumes git is your undo history.
2. **Start with `nexus ask` / `--plan`.** Read-only, mutations refused at the registry level.
3. **Read the preview before `y`.** Every prompt shows the exact command or the file + size.
4. **`--yes` only on repos you own and can reset.** Danger-class tools still block unless you also
   pass `--allow-dangerous`.
5. **Use `never_allow_tools`** for things you never want an agent to do (e.g. `gh_merge_pr`).
6. **Keep the server loopback.** If you must expose it, set `NEXUS_SERVE_TOKEN` and put TLS in
   front of it.
7. **Audit role whitelists.** A role with `tools: [read_file, grep, finish]` cannot hurt you.

## Offensive-security roles

`pentesting`, `osint`, `malware-analysis`, `reverse-engineering`, `ctf-player` and `bug-bounty`
are routed to `openrouter-uncensored` (`cognitivecomputations/dolphin-mistral-24b-venice-edition`
on OpenRouter) because mainstream models refuse legitimate authorized security research — a lab
you own, a CTF box, an engagement with written permission.

What that routing **is**:

* a way to get direct, technical answers about standard methodology on **your own** targets;
* isolated: it is a separate agent and API key from your coding model, so a security run never
  changes the default model — and if you run the *local* twin, `ollama-uncensored`
  (Ollama + `dolphin-mixtral`, `tool_mode: text`), your engagement details never leave the
  machine at all. Switch per role (`agent: ollama-uncensored` in `roles.yaml`, or `/agent
  ollama-uncensored` in the REPL) or flip every tier with `NEXUS_PREFER_LOCAL=1`;
* still gated by every Nexus safety layer — the deny list, the approval gate, the path guard.

What it **is not**:

* authorization. A persona prompt grants no legal permission. Testing a system you do not own or
  have written permission to test is a crime in most jurisdictions, and no system prompt changes
  that.
* a jailbreak guarantee. The personas themselves say to refuse anything that clearly targets real
  third parties or causes real-world harm; models vary in how reliably they hold that line, which
  is exactly why the approval gate exists — **you** are the last control.
* a substitute for scope. Keep a scope document, work inside it, and log what you ran.

The personas in `roles.yaml` are written to be blunt about this:

> You are an offensive-security specialist doing authorized penetration testing / CTF work in a
> lab the user owns or has written permission to test. … Still refuse anything clearly illegal
> (targets the user doesn't control/own, real third-party harm).

If you want a stricter setup, point those roles at `gemini` instead
(`roles.yaml` → `agent: gemini`, or a project-local `.nexus/roles.yaml` override), or add them to
`settings.never_allow_tools` equivalents at the role level with a `tools:` whitelist.

## Supply chain & dependencies

* Runtime deps: `httpx`, `PyYAML`, `python-dotenv` — nothing else. Gemini, DeepSeek and
  OpenRouter all use the same OpenAI-dialect adapter. The server, panel and approval machinery
  are stdlib.
* `gh_create_pr` prefers the local `gh` CLI and its existing auth session; the REST fallback needs
  a `GITHUB_TOKEN` you provide.
* CI (`.github/workflows/ci.yml`) runs on ubuntu with `permissions: contents: read`, tests three
  Python versions, validates the role data and smoke-tests the CLI. No secrets are needed for the
  test suite — it is entirely offline.

## Reporting a problem

If you find a way to make Nexus damage a repo or leak a secret in a way these controls should have
prevented, open an issue with the exact task, flags and tool sequence. Deny-list gaps are treated
as bugs.


### Local-first perception

The [vision boundary](vision.md) is OFF by default. Pixels, PDF pages, OCR and
model answers are untrusted evidence; the VLM adapter has no tools and rejects
tool-bearing responses. Capture is audited before IO, and each owned device has
a durable capture lease/indicator. Vision hard off is independent of malformed
YAML and invalidates old request epochs; native operations already in progress
can take time to return and transmitted bytes cannot be recalled.

Camera/recognition/enrollment have no cloud route. Face templates and consent
metadata require AES-GCM and a native keyring, never plaintext fallback or
biometric authorization. Cloud Q&A permits only explicitly attested non-person
inputs after a fail-closed local face preflight and exact fresh review. Detection
can miss faces: uncertain or identifying material belongs on the local path.
Use trusted model weights and OS controls for device access, temporary files,
process egress and storage; neither permission policy nor VLM instructions form
an OS sandbox. No secure RAM/swap/backups erasure guarantee is made.


### Environment, desktop and device controls (Task 12)

- Every new surface is OFF in operator-owned `permissions.yaml`; shared fresh
  approval, role ceilings, optional 2FA, limits, emergency stop and audit remain
  authoritative. Legacy desktop/backup/home classes and Computer Studio mutations
  cannot bypass them via an environment flag or reusable confirmation phrase.
- Native input is scoped to a verified allowlisted executable/window and bounded
  foreground coordinates or a unique supported accessibility label. All input is
  dangerous: keystrokes/clicks may send, spend, deploy or delete inside another app.
  This is not app/network confinement or protection from same-privilege OS races.
- Browser pages are scriptless/ephemeral, with no credential autofill, persisted
  profile, file uploads, sockets or service workers. Raw clicks cannot submit a
  form. A separate hash-bound request and fresh review are required for the exact
  GET/POST form target/body. Complex/login/hidden-token forms are unsupported.
  Origin routing is not proof that a GET endpoint has no side effects.
- Downloaded filenames never choose paths. Bounded unauthenticated downloads stay
  in an operator-chosen workspace folder and are never opened/executed. File
  management refuses private/operator paths, links, device names and alternate
  streams; deletes go to bounded recoverable trash with no automatic purge.
- Macros are pinned, protected operator files. Learned skills, runtime drafts,
  webpages and MCP outputs cannot become replay authority. Parameters are typed
  literal JSON data, not shell templates. Replay checks each step and stops on
  denial/revocation/uncertain outcome; there is no external rollback guarantee.
- HA REST and TLS MQTT use exact device/service/topic/action allowlists. Locks,
  alarms and garage doors cannot be downgraded below DANGEROUS. API acknowledgements
  do not prove physical state; QoS 1 can deliver duplicate messages. Only normal
  companion notifications are supported for phones, never command hooks/intents.
- Screenshots reuse Task 11 context denylist/OCR/lease/hard-off and remain in local
  private storage, never an automatic cloud/model upload. Privacy filters are
  best effort; plaintext local artifacts and OS ACLs need operator review.
- Computer Studio HTTP is authenticated and same-origin. Synchronous legacy
  effects need one-use expiring reviews; new actions use existing live approval
  sessions with required current IDs. No automatic replay after server restart.

Offline fake-backend tests are not native OS/hardware/account acceptance. See
[environment setup, examples and residual limits](computer-control.md).


### Defensive monitoring and scoped tests (Task 14)

The [defensive security assistant](defensive-security.md) remains OFF in
operator-owned `security.yaml`. Inputs are local read-only observations, not
instructions, target authorization or proof of compromise. Source coverage gaps
are explicit. Alerts retain evidence and propose review; they never dispatch an
action. Public-certificate/SSH key parsing is an optional lazy dependency.

Active tests require an explicit IP/port allowlist and fresh DANGEROUS review.
Raw scanners/exploit runners are refused; enabled defensive mode also rejects
opaque execution relays at the shared gate before and after approval. This is not
a universal OS sandbox. Firewall/account/public-key responses have exact commands
and rollback previews but **no executor**. Existing gates, role ceilings, optional
second factor, rate limits and emergency stop cannot be expanded by a log or model.

Normalized, redacted evidence is privately stored with read-only artifacts and
hash-linked timelines, never silently purged. These are not original forensic
images, WORM storage, secure erasure or root-compromise protection. Dedicated
analysis uses existing defensive roles without tools, retry or fallback and needs
explicit remote disclosure authority. Ordinary parent-agent model conversations
retain their own context/fallback behavior; use the local CLI for private
investigations. Local alerts honor proactive/Inbox DND and speaker handoff, never
inherit automatic Telegram/push/desktop sends. Live-host acceptance remains an
operator responsibility; deterministic fixtures are not a completed security audit.
