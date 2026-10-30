# Dashboard inventory — baseline before the 13-prompt build

Read-only survey of `nexus-agent` v0.3.0 at commit `2f5c9d5` (branch
`arena/011df0b6-nexus-agent`). Nothing here is aspirational: every row was
checked against the source, the running server (`nexus gui --port 8787`) or the
test suite. This file is the map the following build prompts extend.

## 1. Stack

| Layer | What it actually is | Notes |
|---|---|---|
| Frontend | Hand-written vanilla JS (classic `<script src>`, no modules, no framework, no bundler, no npm package for the panel) | 12 scripts in `nexus/panel/`, one document `dashboard.html`, one optional ES-module bundle for World (`frontend/world/build.mjs` → `nexus/hud/world/assets/world.js`) |
| Styling | 8 hand-written stylesheets using CSS custom properties | `dashboard.css` (1149 lines), `desktop.css`, `modules.css`, `integrations.css`, `repository.css`, `workbench.css`, `nexus/hud/assets/hud.css`, world bundle CSS |
| Backend | Python 3.10+ **stdlib** `ThreadingHTTPServer` + `BaseHTTPRequestHandler` (`nexus/server.py`, 1023 lines) | No FastAPI/uvicorn/Express on the dashboard path. FastAPI+uvicorn exist only in the optional `hud` extra for a legacy app that is not served by `nexus gui` |
| Façades | `nexus/workbench.py` (~60 subroutes), `nexus/studio.py` (workflow/evidence store), `nexus/github_repositories.py`, `nexus/gui_modules.py` (operations + world proxy) | `gui_modules` applies the only rate limit in the product: 120 req/min per group, semaphores 2 (operations) / 3 (world) |
| Database | **SQLite**, three tables-sets in two files under `nexus_home()` = `~/.config/nexus/` | `dashboard.db`: `preferences`, `runs`, `studio`. `sessions.db`: `sessions`, `routing_feedback`, `session_observations`. No ORM, no migrations table |
| Config | YAML at the workspace root | `agents.yaml` (13 agents), `roles.yaml` + `roles.d/` (1000 roles, 41 categories), `pipelines.yaml` (5), `permissions.yaml`, `autonomy.yaml`, `context.yaml`, `mcp.yaml`, `telegram.yaml`, `hud.yaml`, `widget.yaml`, `role-index.yaml` |
| Auth | One shared token (`NEXUS_SERVE_TOKEN` / `--token`): `X-Nexus-Token` header or `?token=`, HMAC-compared; browsers exchange it for a signed HttpOnly cookie (`nexus/gui_session.py`: issue/renew/forget/fingerprint) | Host allowlist `NEXUS_ALLOWED_HOSTS`; UI assets are deliberately public (they carry no data). No user accounts, no 2FA, no session list |
| Process manager | **None.** The GUI is one foreground process (`nexus gui` → `server.serve()`). Background work = threads inside that process | Durable work lives in the jarvis daemon (`nexus/jarvis/daemon.py`, lock file) and an optional OS user service (systemd `--user` / Windows logon task) registered from the Desktop setup pane. There is no scheduler, no cron, no restart survival for the web server |
| Streaming | SSE per task: `GET /api/events/<task_id>` | Details in §4 |
| Tests | pytest (1368 collected, 62 files) + 9 Playwright/node:test browser suites in `tests/browser/` driving the real server | `ruff check nexus tests`; `node --check` on panel scripts; no type checker for JS or Python |

## 2. Pages and views

One document, `/dashboard` (aliases `/gui`; `/panel`, `/console`, `/hud`, `/` all 302 here). Nine client entry points own the screen.

| View | Owner | What is on it |
|---|---|---|
| `run` (default) | `dashboard.js` | `.deck` three columns: left = live run data, AI agents, agent topology (`#map`), tool activity; centre = approval card, live activity stream, composer; right = run telemetry, throughput canvases, pipelines, role memory, learning brain, integrations, API keys |
| `control` | `desktop.js` (DOM injected at load) | Hero, 4 KPIs, setup readiness, search, **exactly 18 `.dc-card`** controls with favourites + category chips, `#dcEmpty` |
| `operations` | `hud.js` served as `/dashboard/operations.js` | Status strip, section nav, 4 metrics, execution controls, new task, task list, pending reviews, trace/why, evaluation, permission audit, inbox, diagnostics |
| `world` | world bundle (`import('/world/world.js')` on first open) | Bundled Natural Earth map, USGS quakes, RSS news desk (Bangladesh/World/Crypto/Security), crypto quotes, WeatherAPI forecast, saved cities |
| Workbench dialog | `workbench.js` | 12 panes: overview, models, policy, runs, queue, files, github, errors, layout, + injected `manage` (roles/agents/teams), `desktopSetup` (context/MCP/Telegram/OS service), `worldSettings` |
| Studio dialog | `studio.js` | 8 panes: Sessions, Review, Evidence, Policy, Data, Workflows, Inbox, Runtime |
| Repository review dialog | `repository.js` | PR preview/create, project & branch attach |
| GUI login dialog | `gui.js` | Token entry + "remember this browser" |
| World settings pane | `world-settings.js` | RSS publisher checklist, WeatherAPI key form (secret-safe) |

Navigation is four `[data-gui-tab]` buttons in the sidebar; at ≤1080 px they become a fixed bottom tab bar. The same document is also iframed by the VS Code extension, so any nav redesign must survive a narrow webview.

### Shell and ID contract (must not break)

`#sidebar`, `#navToggle`, `#clock`, `#metaUplink #metaWs #metaMode #metaRoles #metaAgents #metaRouting #metaUptime`, `#navScrim`, `#pageTitle`, `footer.statusbar` (`#sbState #sbTask #sbRole #sbAgent #sbTicker #sbVersion`), `#liveNotice`, `#approvalCard` (`#apYes #apAlways #apNo #apRisk #apTool #apPreview`), `#phaseText #phaseBanner #actionError #stream #filters`, composer `#taskInput #runBtn #cancelBtn #clearBtn #roleSelect #pipelineSelect #readOnly #autoYes`, `#map #resetMap #routeRole #routeMethod #routeHits #ndName #ndProvider #ndBody`, `#tel*`, `#sparkEvents #sparkTokens #sparkLatency`, `#pipeList #memBody #memPath #brainQuery #brainGo #brainList`, `#keyList #integrations`, `#dcGrid #dcSearch #dcRefresh #dcEmpty #dcStatus`, operations IDs (`#controls #reviews #review-count #daemon #hardware #counts #usage #observed #connection #error #diagnostics`), `#guiToken #guiLogin #guiRemember #guiLoginError #guiSettings #guiLogout #guiManagement`, `#workbench #wbClose` (+ `.wb-nav` / `.wb-main`), `#wbPalette #wbSearch`, `#studioOpen #stNav #stPane`, `#repoBar #repoSelect #repoSearch #repoBranch #repoCounts #repoMore #repoActions #repoDialog`, plus the world widget's `#world-*` IDs.

## 3. HTTP surface

### Server core (`nexus/server.py`)

| Method | Path | Notes |
|---|---|---|
| GET | `/` | 302 → `/dashboard` |
| GET | `/dashboard`, `/gui` | `dashboard.html` |
| GET | `/dashboard/<asset>`, `/gui/<asset>`, `/panel/<asset>`, `/console/<asset>` | static from `nexus/panel/`; `operations.js` is rewritten to `hud/assets/hud.js` |
| GET | `/world/<asset>` | 11-file allowlist (geojson, earth-night.jpg, Bengali font, `world.js`, licences) |
| GET | `/health`, `/api/health` | version, workspace, role/agent/pipeline counts, demo flag, auth flags |
| GET | `/api/session` | auth probe + cookie renewal |
| POST | `/api/session` | `{remember:true}` issue cookie, `{forget:true}` clear it |
| GET | `/api/roles` | 1000 roles (id/name/title/category/persona/keywords/agent) |
| GET | `/api/agents` | 13 model profiles incl. `key_present` (never the key) |
| GET | `/api/pipelines` | 5 pipelines with steps |
| GET | `/api/tasks` | in-memory session snapshots |
| GET | `/api/task/<id>` | snapshot + `pending` + last 200 log entries |
| GET | `/api/events/<id>` | SSE (§4) |
| GET | `/api/stats` | first-paint snapshot: roles by category, agents, key presence, run counters, token totals, brain counts, local-only policy |
| GET | `/api/skills`, `/api/skills/<name>`; POST `/api/skills` | learned skills library (SKILL.md) |
| GET | `/api/brain` | skills + long-term memory + past sessions |
| GET/POST | `/api/recall` | FTS5 search over past sessions |
| GET | `/api/memory/<role>` | per-role memory notes |
| GET | `/api/ollama`, `/api/github` | local model inventory, gh CLI status |
| GET/POST | `/api/workbench/<name>` | 13 GET + 14 POST names, see below |
| POST | `/api/task` | `{task, role?, pipeline?, read_only?, auto_yes?, local_model?, …}` → 202 `{task_id,status}` |
| POST | `/api/approve/<id>` | `{approved:bool, scope:'once'|'session', approval_id?, reason?}`; revalidates file preview hash |
| POST | `/api/cancel/<id>` | sets cancel event, resolves pending approval as denied |
| ANY | fallback | `404 {"error": "no route for …"}` |

Workbench subroutes — GET: `configuration, desktop, errors, file, files, git, github, history, jobs, metrics, queue, settings, world-settings`, `repository/<14>`, `studio/<24>`. POST: `capability, configuration, create-pr, delete-model, desktop, execute-plan, health, ollama, pr-preview, pull, queue, settings, tests, world-settings`, `repository/<14>`, `studio/<24>`. Several POSTs require an exact typed confirmation (`RUN TESTS`, `PULL <model>`, `DELETE <model>`, `RUN SMALL MODEL TEST`).

### Operations and public information (`nexus/gui_modules.py`)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/operations/state` | daemon, hardware, policy, 50 jobs, pending actions, usage budgets, inbox, workflows, audit, traces, evaluation — everything the operations page draws |
| POST | `/api/operations/action/<operation>` | start/pause/resume/stop/kill worker, DND, autonomy, queue actions … (each re-validated in `hud/core.py`) |
| GET | `/api/operations/trace/<id>?operation=inspect|why|replay` | evaluation replay |
| GET | `/api/operations/job/<id>`, `/api/operations/approval/<id>` | one record |
| GET | `/api/weather?latitude&longitude`, `/api/cities?q`, `/api/news`, `/api/quakes`, `/api/crypto`, `/api/countries` | read-only; disabled when the World widget is off |

### Consumer contracts

The VS Code extension uses `/api/roles`, `/api/agents`, `/api/approve/<id>` and iframes `/dashboard`. The browser tests assert `[data-gui-tab]`, `#view-control` visibility, `.dc-card` count `18`, and the SSE framing. `tests/test_server.py` asserts legacy CSS tokens and the dashboard title.

## 4. The SSE stream

`GET /api/events/<task_id>` — **one stream per task**, never global.

- Frames: `id: <event_id>\n data: {json}\n\n`; `event_id` is a per-session monotonic counter.
- Resume: `Last-Event-ID` header replays from that cursor (each subscriber keeps its own cursor — a shared queue used to make the browser and the sidebar steal each other's events).
- Idle: a `{"type":"ping","status":…}` frame every 15 s.
- Lifecycle: the stream closes itself when the terminal `done` event is delivered; 404 for an unknown task.
- Auth: same token/cookie rules; `?token=` accepted for EventSource compatibility.
- Event types (`nexus/types.py`): `status, role, agent, think, usage, text, text_delta, tool_call, tool_output, process, file_change, tool_result, approval, error, memory, skill, done`.

**Gap that affects Prompt 2:** there is no application-wide stream, so nothing pushes daemon heartbeat, new approvals from the Jarvis queue, resource ticks or alerts. Those must be produced inside the existing server (a second provider connection would break the single-server rule).

## 5. State, secrets and safety invariants

- Live run state is in memory (`ServerState.sessions`); durability is `sessions.db` + `dashboard.db.runs` + `studio` items.
- Secrets: provider keys come from environment/keyring (`nexus/keys.py`), are masked before reaching the browser (`key_source`, `masked` only), and `nexus/redaction.py` scrubs event text. `studio.secret-scan` exists.
- Review-first: `ApprovalGate` in `nexus/tools.py`; `permissions.yaml` has three modes (`READ_ONLY`, `ASK_EVERY_TIME`, `AUTO_ALLOW`) plus per-tool entries; `fresh_confirmation_required()` forces a human decision for shell/git/external mutations even under `AUTO_ALLOW`. Orchestration-level approval in `nexus/jarvis/approvals.py` (pending → leased → decided, with expiry).
- Computer adapters are **off by default**: `permissions.yaml → computer: {browser:false, editor:false, office:false}`. `nexus/computer.py` (bounded adapters), `nexus/browser_worker.py` (Playwright, origin allowlist, GET/HEAD only), `nexus/computer_control.py` (EmergencyStop latch file). Docs state plainly that this is *not* an OS sandbox and that VS Code hotkey/Office interop are not implemented.
- Error envelope is `{"error": "<human string>"}` everywhere; there is no `{ok, error:{code,message}}` shape today.

## 6. Coverage against the 13 build prompts

| # | Prompt | Exists today | Real gap |
|---|---|---|---|
| 1 | Shell, nav, palette | 4-tab sidebar → bottom bar, top bar with tool group, status bar, theme toggle, Workbench Ctrl+K focus | 6+5 section nav, breadcrumb, agent/status chips, Emergency Kill, global palette with fuzzy search + prefixes, notifications bell, toasts |
| 2 | Mission Control | Control-view KPIs + readiness; operations metrics; topology map; live stream (inside Run) | `/api/mission/summary`, cross-run feed, 24 h timeline, health strip, alerts, composer drawer, deep links |
| 3 | Role Hub | 1000 roles readable; edit/save/remove through `workbench/configuration` with revision + confirmation; teams = pipelines | Everything else: version history/diff/rollback, stats, bulk actions, virtualised table, import/export, quality checker, Monaco prompt editor, role test runner |
| 4 | Router Studio | `nexus/router.py` (keyword scoring, LLM tie-break, sticky/manual/default, candidate list), `routing_feedback` table, `agents.yaml` fallbacks, `local_only` policy, model registry = `/api/agents` | Rule builder, dry-run, fallback chain UI, cost/speed/quality weights, test lab, accuracy panel, per-decision persistence, override in composer |
| 5 | Computer Control | Bounded adapters (browser via Playwright worker, file read/write, Office read), EmergencyStop latch, domain allowlist | Per-capability Off/Read/Ask/Auto, scopes/trust profiles, browser tab UI + screenshots, file tree/trash/undo, VS Code bridge, real terminal, screen view, app launcher, panic hotkey |
| 6 | Approval Center | Live approval card, operations review inbox, Jarvis approvals with lease + expiry, audit trail | Risk scoring with reasons, detail/diff view, bulk + standing rules, edit-then-approve, timeout policy UI, Telegram buttons, history search/export |
| 7 | Workflow builder | Studio `workflow`/`workflow-live` items, `pipelines.yaml`, retries/timeouts in `studio_execution` | Canvas editor, triggers, cron scheduler that survives restarts, node-level run view, run diffing, versioning UI |
| 8 | Security & audit | Studio audit list, permission audit panel, `redaction.py`, `studio.secret-scan`, backup/restore routes, session cookie | Hash-chained log, tamper check UI, anomaly rules, vault UI, sandbox toggle, prompt-injection banner, TOTP/2FA, session list |
| 9 | Memory & knowledge | Per-role markdown memory, skills, FTS5 recall, `brain`, faiss + Ollama `nomic-embed-text` index for **roles** (`nexus/orchestration/index.py`) | Memory policy UI, document RAG (chunks/scores/sources), collections, project context, cleanup tools |
| 10 | Cost & performance | Budget reservations + actual tokens; user-entered price per model; provider reachability probe; Ollama inventory/pull/delete/smoke; server CPU/RAM/GPU probe | Latency percentiles, per-provider/model/role/day breakdown, budgets with hard stop, comparison runner, provider dropdown population, resource history |
| 11 | Notifications, Telegram, voice | Telegram policy, supervised polling, approval PIN, quiet hours/DND, Telegram extra package | Notification centre, channels + test buttons, full command set, voice I/O, mobile-only simplified mode |
| 12 | World page | Bundled map + geojson, quakes, RSS desk with per-feed failure, WeatherAPI with saved cities and "local threshold, not official warning", secret-safe key form | Auto-fetch on open + 10-min cache, explicit loading/error + Retry, custom feed list, BD security feed, market snapshot gating |
| 13 | Settings, polish, QA | Appearance (theme), language (en/bn) persisted per workspace, layout pane (hide/move/resize/focus), reduced-motion CSS, docs | Shortcuts editor, first-run wizard, a11y audit, lazy-load/virtualisation budget, new e2e coverage, Bangla user guide |

## 7. Non-negotiables audit

| Rule | Status today | What the build must change |
|---|---|---|
| 1 Review-first | Enforced in three places (tool gate, orchestration approvals, `fresh_confirmation_required`) | Keep; every new endpoint that can act must route through the same gate |
| 2 No invented data | Enforced by convention and tested in places (`actual_cost_usd: null` unless priced) | Keep; new panels need explicit "Unavailable / Not checked" states |
| 3 Last-updated on live panels | Present in a few (`observed_at`, `#observed`) | Must be added to every new live panel |
| 4 Offline-first with Ollama | Yes — `local_only`, Ollama inventory, local default model | Keep; cloud stays optional |
| 5 Bangla/English, dark/light, keyboard, 360 px | en/bn + themes exist; keyboard coverage partial; **tested at 390 px, not 360** | Extend strings to all new UI; verify 360 px |
| 6 Endpoint hardening: validation, auth, rate limit, `{ok,error:{code,message}}`, audit | Validation and auth yes; rate limit only in `gui_modules`; error envelope is `{"error": "…"}`; audit exists for orchestration actions only | Decide the envelope (see §8), add a shared limiter + audit decorator for new routes |
| 7 Secrets never reach the browser | Yes (masked keys, redaction) | Keep; vault UI shows last 4 only |
| 8 Visual style: dark navy + teal, mono technical values, rounded cards | Exactly the current design system (`--bg`, `--accent #2dd4a4`) | Keep |
| 9 Lint + tests after each step | `ruff` + pytest + `node --check`; browser suites need a live server | Automation stays per-step |
| 10 End-of-prompt report | — | Files, endpoints, tables, manual test, limits |

## 8. Decisions to settle before Prompt 1

1. **Error envelope.** Rule 6 wants `{ok:false, error:{code,message}}`; every existing route returns `{"error":"…"}` and both the panel and `tests/test_server.py` read that key. Proposal: new routes return the structured envelope *in addition to* `error` (a superset), and a shared helper is introduced; existing routes are migrated only where tests allow.
2. **Emergency Kill.** The repo deliberately refuses arbitrary PID killing (`nexus/computer_control.py` docstring). Proposal: the red button performs the reviewed lifecycle stop (daemon stop + queue pause + `EmergencyStop` latch + cancel live sessions) with a typed double confirmation, and never sends signals to unrelated processes. No new "kill the OS" privilege.
3. **Navigation.** Six sections + a secondary group replaces four tabs. Proposal: hash router (`#/mission`, `#/roles`, …) with the four legacy hashes (`#run`, `#control`, `#operations`, `#world`) kept as aliases, all existing views still reachable, `[data-gui-tab]` retained for the mobile bar and the VS Code webview.
4. **Monaco.** The panel has no bundler and must work offline. Proposal: vendor a pinned editor bundle only if it is small enough to ship in-wheel (otherwise a light code editor with line numbers, tab handling and token count, plus copy/download).
5. **Sandbox for terminal/browser/screen (Prompt 5).** Host execution is explicitly not sandboxed today. Proposal: build on the existing `shell_backend: docker` path and trust profiles, and keep host automation behind `ASK_EVERY_TIME` unless the operator opts in.
6. **360 px.** Tests currently assert 390 px. Proposal: extend to 360 px and keep 390 px passing.

Defaults above are what the build will use unless told otherwise.
