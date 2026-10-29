# Module 5: authenticated HUD and local Textual controls

This is a consumer of **existing Studio/Jarvis/delegation/workflow/permission state**,
not a replacement scheduler, learning database or demo dashboard. Opening either
surface never starts a model/daemon implicitly. Context collection remains **OFF**.

## Install and run (PowerShell)

```powershell
python -m pip install -e ".[hud]"
# Generate/set a private HUD-only token locally; never use a GitHub/provider key.
$env:NEXUS_HUD_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
nexus hud -C .
```

Open `http://127.0.0.1:8790`, enter the private token, then Connect. `hud.yaml`
controls loopback host/port, refresh (1–30 seconds) and sockets (1–8). It is an
operator-owned, bounded, validated policy; model file tools and snapshot restore
cannot change it. No inline credentials or auto-start configuration is accepted.
The token comes only from the process environment, not command-line arguments,
URL query, local/session storage, cookies, HTML or logs. Do not print/share it.
The root `.env.example` documents the variable, but HUD does not implicitly import
that file. HTTP API requests require bearer auth; WebSockets require same-origin
handshake and first-message authentication within five seconds. Uvicorn disables
access logging and forwarded-header trust. No CORS access is granted.

Default binding is loopback. For a deliberately trusted reverse proxy/preview,
`--expose --allow-host EXACT.HOSTNAME` opts into 0.0.0.0, still requiring the private
token and exact Host/Origin. Never use an untrusted/public plaintext network; use
TLS and an authenticated/private proxy. No wildcard hosts, permissive CORS or
URL-token shortcut exists. This is a local operator surface, **not multi-user RBAC**.

```powershell
nexus hud -C . --tui
```

Textual uses the current OS user's local Studio access, like the existing CLI. It
opens **no HTTP listener**, needs no bearer token, and uses the same control adapter.
The exact prompt is `[NEXUS_CORE@SYSTEM:~]$`. Type `help`; input is parsed as literal
HUD commands, never executed by a shell. Prepare `approve ID` / `allow ID` / `start`
/ `autonomy L3`, inspect the real record/warning, then separately type the exact
`confirm PHRASE`. Wrong confirmation leaves the request retryable. Local OS-user
access is already trusted to edit the application's state; no new privilege boundary
or OS sandbox is claimed.

## Actual data and working controls

- WebSocket snapshots of current queue, actual daemon heartbeat/lock state,
  active-role metadata, CPU/memory/disk (psutil), reported token usage and conservative
  day/hour reservations. USD remains **unknown**, not zero. Refresh is sampled,
  not a transactional cross-record snapshot; the first nonblocking CPU sample can
  be zero. Queued/approved is not a claim that a worker is running.
- Create bounded delegated jobs; inspect checkpoint/effect uncertainty and prior
  result-attempt provenance; approve/reject/cancel; resume interrupted/error jobs
  **for fresh review**, never replay. No implicit daemon start on submission.
- Inspect real pending per-action arguments/proposal/diff, then type the exact
  one-use confirmation. Task approval never grants action consent. Expired/stale/
  changed-file/changed-policy approvals fail through existing Jarvis enforcement.
- Start/resume explicitly warns about previously approved work and requires typed
  confirmation. Pause/stop/kill are durable cooperative requests; UI displays the
  actual heartbeat, never a fabricated successful kill from an intent receipt.
- Review L0–L3 policy changes using exact `SET AUTONOMY LEVEL`; validated existing
  settings are retained. Raising a level does not enable runtime or grant roots,
  new tools, external action approvals or cloud inference. Quiet/DND use the real inbox.
- Actual stored workflow-run DAG dependencies and observed node statuses;
  unobserved nodes are labelled `not_observed`, never painted complete. Planning-only
  files are not executed or crawled by the HUD.
- Existing permission audit tail and inbox counts. No shell/log-file arbitrary
  reads, screenshot capture, global-input signals or cloud upload endpoints.

Views are bounded: latest 50 jobs/actions (actual total counts retained), latest
five workflow runs with up to 40 displayed nodes each, last 20 audit entries from
a bounded regular trusted audit file. Detail endpoints inspect a selected record.
All outputs are masked against known secrets, then rendered as literal text, not
HTML/Markdown commands. The local TUI also disables rich markup for untrusted data.
These literal masks are not a guarantee against unknown/encoded credentials.

The web transport limits authenticated requests to 120/minute, concurrent snapshots/
controls to two, configured WebSocket connections, first-frame size/time, and action
bodies to 16 KB. Wrong auth/origin/host, unsupported fields and invalid confirmations
fail closed. These are application limits, not an Internet-facing denial-of-service
protection service. A disconnected UI retains its last observed snapshot clearly
labelled disconnected; no silent reconnect or fake progress is generated.

## Tests and remaining Module 5 work

ASGI tests exercise real SQLite queue/delegation/review/control state, a real waiting
approval callback, request/connection/auth/Origin limits and actual audit metadata.
Headless Textual tests drive actual commands and refresh. Browser tests use the
real temporary-workspace FastAPI/WebSocket server, not mocked API responses: live
auth, create/approval retry/cancel, literal XSS rendering and mobile/disconnect.
No model, native service or external GitHub action runs in those tests.

Install dependencies via `.[dev]` to run the optional HUD/TUI Python tests. For the
browser tests, launch an isolated runtime-disabled HUD and use:

```powershell
$env:NEXUS_HUD_TEST_URL = 'http://127.0.0.1:8790'
$env:NEXUS_HUD_TEST_TOKEN = $env:NEXUS_HUD_TOKEN
cd tests/browser
node --test hud.test.mjs
```

MCP/plugins and Telegram now have bounded disabled-by-default consumers and
functional tests; see [MCP](mcp.md) and [Telegram](telegram.md). Optional priced
model-allocation caps and independent 80% cost/time alerts are documented in
[delegation](delegation.md). Actual invoice/electricity cost stays unknown.
**Module 5 implementation/automated acceptance is complete.** Context was added
last and remains OFF by default; its real indicator reports policy, never claims a
hidden collector is active. See [context privacy/native limits](context.md). Existing legacy dashboard remains available on its separate
stdlib server; these optional control surfaces do not weaken its approval paths.



Earlier HUD-only verification (before MCP/Telegram/cost edits): **28 dedicated HUD/TUI Python tests**, **1,084 full Python tests
passed in 114.36 seconds**, **5 real HUD browser tests passed** (7.34 seconds), and
**29 existing dashboard browser tests passed** (206.83 seconds) on restarted
current-code servers. Ruff/diff checks and wheel-content verification passed.
There is one upstream Starlette TestClient/httpx deprecation warning, not a failed
or skipped test. The fixture servers were stopped; no daemon/model was started.


Module 6 adds authenticated trace inspection, why and selected-step no-effects
views; **no trace execution endpoint**. The optional sixth browser case consumes
a real observed trace identified by `NEXUS_HUD_TEST_TRACE_ID` in an isolated test
workspace; it is skipped when no such trace is supplied. The shipped HUD never
seeds demo observations. Snapshot sockets release slots immediately on client
disconnect and reject post-auth command frames. See [evaluation](evaluation.md)
and the [current six-module audit](six-module-audit.md).
