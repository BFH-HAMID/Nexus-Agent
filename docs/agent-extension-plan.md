# Six-module extension: audited rollout and acceptance ledger

This is an implementation/automated-acceptance ledger, not native/live certification.
The rollout is deliberately incremental. Existing behavior remains the default;
new orchestration must be explicitly requested until its integration is verified.

## Implementation status — 2026-10-29

- **Module 1: supported-flow implementation and automated acceptance complete.** Local
  FAISS index (Ollama semantic encoder or explicit lexical-hash), bounded metadata,
  cache/new-role refresh/watch, two-stage top-15 -> 3–7 routing, confidence question,
  fixed reviewed project DAG, existing Studio validation/execution/approval,
  max-three repairs + inbox escalation, shared artifacts, per-node adapter/tool
  ceilings and explicit feedback in existing sessions.db. Local-only is enforced
  before constructing CLI providers; task secrets are masked before embeddings.
- **Module 2: supported-flow implementation and offline acceptance complete.**
  Token-only GitHub API/HTTPS, public/private/org provisioning, mandatory scans,
  controlled conventional commits, no hooks/force/admin/auto merge, initialized
  Python/Node scaffold with actual tests/README/license/gitignore/push CI, separate
  project Git folders and persisted repo/branch/PR lifecycle mapping are consumed.
  Exact remote-head successful fresh test runs are required at PR/merge preview and
  execution. Legacy reviewed helpers share these gates; generic API writes/raw PR
  mutations/alternate typed commit producers cannot bypass them. See
  [Module 2 acceptance and limits](github-extension.md). No claim of live/native
  Windows certification, arbitrary binary transport or OS/shell isolation.
- **Module 3: supported computer-control core implemented and tested.** Existing
  permission gate now consumes opt-in Playwright/VS Code/Office adapters, typed
  text/OOXML snapshots with exact safe undo, fresh dangerous/external consent,
  CLI dry-run, emergency command/Windows hotkey listener and scoped read-only-copy
  Docker execution. Literal credential arguments/output are protected, including
  split live chunks. Browser/password/payment and untrusted-host boundaries are
  deliberately constrained. No claim of native Windows, live GUI/Docker acceptance,
  universally undoable host commands or complete OS isolation. See
  [computer controls and operational acceptance](computer-control.md).
- **Module 4: implementation delivered; native/live host acceptance separate.**
  Reuses Studio, agents/router/learning, Module 2 previews/CI/scans and Module 3
  permissions/snapshots. Durable lifecycle/priority/TTL/depth/cooldown/leases/budgets;
  default-L1/off/read-only/offline policy; explicit L2/L3 source admission and
  narrow new-file/append L3 rules; fresh one-use reviewed edits/project actions;
  file/TODO/build/Git/CI/system/inbox/queue-idle/schedule probes; deferred CI and
  online-required job reconciliation; inbox/digests/quiet/DND, opt-in native counts
  notification transports, actual OS-manager exports/exact-confirmed user installers and bounded missions.
  No raw host shell in background jobs or automatic mutation replay. Native service
  installation, notification display, actual Ollama and authorized live GitHub remain
  host acceptance, not claimed from mocked transports. Module 5 consumers reuse this runtime.
  See [Jarvis operation and acceptance boundaries](jarvis.md).
- **Module 5: implementation and automated acceptance complete.** Existing Jarvis jobs now support
  immutable per-task deadlines, actual per-attempt metadata-only checkpoints,
  status/report/tasks/cancel/resume CLI, fresh reapproval on recovery, explicit
  uncertain outcomes and parent-observed 80% time alerts. No second scheduler/KB.
  Authenticated FastAPI/WebSocket HUD and local Textual TUI now consume the same
  real state, with working exact approvals, controls, metrics/DAG/audit and literal
  rendering. `hud.yaml` has a tested consumer. Optional operator-priced model allocation caps/80% alerts, foreground MCP
  stdio/HTTP/SSE manifest plugins, and Telegram private-chat long polling with
  exact whitelist/PIN/replay guards now have real consumers. Context/reference/privacy now has its real default-OFF consumer,
  native probes/VS Code bridge, local-only reference-bearing runs and separately
  gated local/cloud vision. See [context boundaries](context.md). Live vendor/account/native acceptance is separate from fixtures. See [HUD operation/security](hud.md).
  See [delegation boundaries](delegation.md). Context stays OFF and last.
  Prior delegation-only acceptance: **22 dedicated tests; 1,056 Python tests passed in
  79.81 seconds**, including actual headless browser/Office integration. Ruff/diff
  and wheel content verification passed. The previous 29 dashboard regressions
  were the browser baseline at that earlier slice.
  **Earlier HUD-only verification:** 28 HUD/TUI Python tests; **1,084 full Python tests
  passed in 114.36 seconds**, **5 real HUD browser tests** (7.34 seconds) and
  **29 legacy dashboard browser tests** (206.83 seconds) against restarted current
  servers. Ruff/diff and wheel content verification passed. One upstream Starlette
  test-client deprecation warning remains; no tests skipped/failed. Native host
  acceptance is not inferred from those tests.
- **Module 6: implementation and automated acceptance complete.** Measured existing-store learning, evidence-aware ranking/exploration, weekly advisory, redacted inspect/why, default-no-effects replay/freshly authorized limited file re-execution, historical snapshot costs and six scripted integration demos are delivered. See [current six-module audit](six-module-audit.md) and [operation/boundaries](evaluation.md).
- **Module 4 acceptance baseline:** **1,034 Python tests passed** (including real
  local headless browser and DOCX/XLSX/PPTX integration), **29 browser dashboard
  regressions passed** (156.91 seconds) against a restarted current-code server, Ruff/diff checks
  and wheel build passed. New stage: 26 computer tests, 6 stream-redaction tests,
  14 Jarvis admission tests, 12 lifecycle/service tests, 15 runtime tests and
  13 event/inbox/mission tests plus 42 Module 4 completion/security/transport tests;
  prior permission/recovery/GitHub suites retained. Final Python run: 78.22 seconds.
  Desktop/Docker/Win32 transports are scripted where noted, not live acceptance.
  Earlier Module 2 closure was 848 Python / 29 browser; permission/recovery closure
  was 906 Python / 29 browser. Real 1,000-role lexical-hash FAISS indexing/cache was
  exercised earlier. No hosted-model/live GitHub/native Windows certification.
- **Module 1 limits:** fixed project graph; eager existing YAML role loading,
  selected-only prompt injection; shared-checkout writes serialize. No claim of
  isolated concurrent writers or new HUD beyond existing Studio. Provenance checks
  cannot prove an LLM review is factually correct. Broader permissions are pending.

See [Module 1 operation and safety boundaries](orchestration.md). Preserve Module
2/3 strict gates and schedule explicitly authorized platform/live acceptance.
Context was implemented last and remains OFF by default. Module 6 starts only after
the current Module 5 automated gate; native/live host acceptance stays explicit.

## Existing architecture (reuse, do not replace)

- `config.py`: layered `roles.yaml` + `roles.d/*.yaml`, aliases, weighted keywords,
  provider configuration and role tool restrictions. Preserve every existing role.
- `router.py`: keyword routing and bounded classifier prompts (already avoids
  sending all full role personas). Preserve the legacy single-role path.
- `providers.py`: Gemini, DeepSeek, OpenRouter, OpenAI-compatible/Ollama,
  streaming and configured fallback chains. No parallel provider adapter stack.
- `agent.py` + `tools.py`: actual execution, approval gate, path guard, cancellation,
  subprocess timeouts, execution-evidence guards. Extend these boundaries rather
  than adding a privileged second tool runner.
- `learning.py`, `memory.py`, `skills.py`, `sessions.py`: the existing procedural,
  declarative and episodic learning system. New ranking observations belong in
  this system, not a new learning database.
- `studio_workflows.py`: validated immutable DAGs, budgeted nodes, explicit
  execution approval and parallel read-only research/review. Writes serialize.
- `studio_execution.py`, `studio.py`: managed processes, Docker execution,
  checkpoints, human review, context secret scanning and durable SQLite records.
- `github_repositories.py`, `github_tasks.py`: repository inventory, approved
  direct GitHub task branches/commits and task-bound PR/merge review.
- `server.py`, `panel/`: existing HTTP/SSE HUD, approvals, task history, real
  hardware telemetry, live output. Keep it working while adding new surfaces.

## Target architecture

```text
CLI / authenticated HUD / Textual TUI / future authenticated Telegram
                         |
                admission + privacy + budgets
                         |
          events -> existing Router + hybrid role index
                         |         ^
                         |         | existing skills/memory/sessions
                 selected-role DAG planner
                         |
           existing Studio workflow/task scheduler
              |                       |
      bounded worker agents     independent reviewers
              |                       | retry <= 3, then human
       existing ApprovalGate <--------+
              |
     existing tools/providers + permissioned plugins/MCP
              |
   workspace / isolated checkout / GitHub / browser / Office
              |
       redacted audit + existing learning feedback
```

## Rollout / requirement coverage

### 1. Auto Role Orchestrator — first implementation slice

- Add bounded metadata records with role ID/name/category/tags/description/skills/
  tools. Do not copy full personas into the index or routing prompts.
- Local FAISS vector search, local Ollama embeddings and a clearly labelled
  deterministic lexical-vector option for offline testing/bootstrap.
- Refresh the index when layered role metadata changes; provide one-shot and
  watch CLI commands. No automatic model download or background process at import.
- Cheap task/phase classifier -> hybrid top 15 -> 3–7 selected roles with validated
  confidence/reasons. Ask exactly one question at low confidence; do not execute
  an uncertain plan.
- Read preferences and historical outcomes from existing MemoryStore/SessionStore;
  append explicitly approved/rejected routing feedback through existing sessions.
- Generate phase dependencies and independent reviewer nodes in the existing
  workflow schema, retaining inputs, outputs, criteria and allowed-tool metadata.
- Integrate bounded reviewer retry/escalation in the existing DAG executor.
- Reuse provider fallback; configurable local/simple and strong/complex model
  choices must never override a local-only policy.
- Persist plan/status/decisions as redacted JSON/Markdown in the project runtime
  directory; expose the saved DAG in the current Studio UI.
- **Limit:** config currently materializes role objects from YAML. Only selected
  full personas enter worker prompts; true lazy-on-disk persona loading remains a
  separate compatibility change. Parallel mutating workers require isolated
  workspaces; do not introduce unsafe concurrent writes to one checkout.

### 2. GitHub Automation — supported implementation delivered

Audit/extend existing GitHub tools: env/.env-only strict token mode, fine-grained
scope errors, repo creation (public/private), project-per-folder, init/branches,
conventional commits, push/PR/issues, README/gitignore/license/CI scaffolding,
pre-commit secret scan on *every* path (including shell-mediated Git), test
requirements and explicit main-merge approval. Preserve task-bound head matching.
No live external mutation tests; use scripted transports and disposable repos.

### 3. Permissioned computer control — before autonomous work

Consume `permissions.yaml` in the existing ApprovalGate: READ-ONLY / ASK-EVERY-TIME /
AUTO-ALLOW per tool; approval for outside-workspace access; dangerous actions
always confirm regardless of autonomy. Add browser, VS Code, Office and file
operations through that same gate. Cover allow/deny lists, timeouts, kill command
and hotkey, per-call redacted audit, dry-run, untrusted-data boundaries, scoped
Docker/VM execution, and task snapshots + safe conflict-aware undo. Never claim
OS sandboxing from an application path guard alone.

### 4. Jarvis daemon

Persist a priority queue/state in the existing project store; implement
start/stop/status/pause/resume/kill and OS-specific user-service installers.
File/TODO/build/Git/PR/CI/schedule/system/inbox/tasks.md/idle triggers must be
explicitly enabled, bounded and deduplicated. Route them through Module 1.
Message-bus depth, loop cooldown, CPU priority and hourly/daily call/token/action/
parallel budgets fail closed across restart. `autonomy.yaml`: L0 observe, L1
suggest (default), L2 branch/sandbox prepare, L3 whitelisted safe actions only.
External/destructive actions still need approval. Add quiet hours/DND/digests,
notifications, inbox/approve/reject/explain, approved/rejected learning feedback,
Dev/Trading/Pentest/Study missions (no trading/payment execution without approval),
offline Ollama-only operation and explicit online reconciliation.

### 5. Surfaces and integrations (context last)

1. Extend the current HUD without fake telemetry; add authenticated loopback
   FastAPI/WebSocket surface and Textual TUI, live DAG/experts/approvals/logs/
   hardware/usage/audit/context/notifications and pause/resume/kill/autonomy/DND.
2. Delegation with persisted step checkpoints and explicit safe crash recovery,
   queued/running/blocked/waiting-approval/done/failed states, time/cost limits,
   80% alerts, task/cancel/resume commands and evidence-bearing final reports.
3. MCP stdio/HTTP clients and manifest-based Python plugins, discovery/reload,
   per-tool budgets/timeouts/rate limits, permission/audit wrapping, untrusted
   outputs, secret references to env/keychain, template plugin and guide.
4. Telegram long polling only: exact user-ID whitelist; status/tasks/delegate/
   approve/reject/pause/kill/report; replay-resistant second PIN confirmation for
   sensitive approvals and quiet-hour notifications. Never execute arbitrary chat
   text or trust a username as an identity.
5. Finally `context.yaml`, default OFF: opt-in window/app/project/VS Code file/
   terminal exit/error/Git/clipboard signals, local reference resolution,
   password-manager/banking/private-browser/email blocklists, secret/card masking,
   in-memory/short-lived screenshots and local vision. Cloud vision/upload needs
   separate approval; screen/file/log content remains untrusted.

### 6. Evaluation, role quality, replay and costs

Extend existing sessions/learning with role success/review/retry/approval/time/
cost observations and exploration-aware ranking; do not treat a model's success
claim as verified test success. Weekly HUD/CLI reports, unused/problematic roles
and advisory improve/merge/remove suggestions (never automatically delete roles).
Full redacted trigger/context/candidates/reasons/prompts/models/tools/results/
reviews trace; safe inspect/step replay defaults to no side effects, with a fresh
approval for any re-execution. Why reports and model/project/day token/cost views;
unknown prices remain unknown, not invented zero-cost totals.

## Remaining deliverables / acceptance gates

Only ship a configuration file when a real consumer and tests exist. All required Module 5 configuration consumers now exist, including default-OFF
`context.yaml`. MCP/Telegram/context defaults are shipped with real disabled-by-default consumers and tests. Never ship fake daemon/plugin endpoints or
empty integrations as completed modules.

Scripted integration demos must be labelled scripted, not presented as successful
live external actions. Final end-to-end acceptance requires: e-commerce plan and
execution with payment approval boundaries; failing test -> fix branch -> reviewer
-> inbox; context reference resolution; Telegram delegation + HUD PR approval;
plugin added without core edits; safe trace replay. Native Windows Office/COM,
hotkeys, service installation, real model inference and live external services
need explicit platform/live verification in addition to unit tests.

### Module 5 MCP / Telegram / priced-allocation acceptance checkpoint

Module 5 **remains in progress**. Context/reference/privacy collection is still
OFF/unimplemented and is the remaining required integration; Module 6 has not
started. Do not treat this checkpoint as completion of the six-module rollout.

Implemented/reused consumers: foreground MCP stdio + HTTP/response-SSE and plugin
manifests, operator-owned plugin trees/entry-file hashes, fresh approval + persistent
RPC quotas; Telegram exact-private-ID long polling, bounded one-use second PIN,
durable pre-side-effect offsets and quiet count notifications; optional signed
operator-priced local-model allocation cap, unknown-price dispatch block, changed
rate revocation and independent durable 80% warnings. These extend existing gates,
queue/Studio, providers and self-learning rather than a parallel KB or runner.

Verification on this checkout:
- Full Python: **1,128 passed, 1 optional Chromium test skipped**, 88.81 seconds;
  `/tmp/nexus-m5-extensions-final-full.log`. One upstream Starlette deprecation.
- Reran the complete computer adapter suite with the actual installed Chromium:
  **26 passed**, 9.51 seconds; `/tmp/nexus-m5-final-computer.log` (includes the
  optional case skipped by the no-environment full invocation).
- Current-code real HTTP/WebSocket HUD browser: **5 passed**, 7.33 seconds;
  `/tmp/nexus-m5-final-hud-browser.log`.
- Existing dashboard scripted browser regression: **29 passed**, 175.19 seconds;
  `/tmp/nexus-m5-final-dashboard-browser.log`.
- Ruff, `git diff --check`, isolated wheel build and explicit wheel checks for new
  code/config/docs/runnable template resources passed. Wheel SHA256:
  `c505c7a5a1593b06fc0ea51869885c776b12de3061dccf82012d733ea94c8c1c`.
- Both temporary verification servers stopped; no service installed, model invoked,
  live Telegram/GitHub/vendor MCP account action certified, or branch pushed.

Further acceptance: actual Telegram account delivery, arbitrary vendor MCP
compatibility, native Windows/Office/VSCode/background service/inference and the
context/privacy/reference demo remain distinct from automated fixtures. Priced
allocations do not certify provider invoices, external tool fees or electricity.


### Module 5 closing gate — context last, then Module 6

Implementation and automated acceptance complete; **not native/live certification**.
Context has actual opt-in window/app/project/editor/file/terminal/Git/clipboard
consumers, workspace/TTL/privacy/secret/card checks, local reference resolution,
existing-gate screenshot consent and separate exact-image cloud consent. Window
pixels stay out of Nexus logs/history, OCR is mandatory and best effort, screenshots
are bounded/ephemeral. Unsupported or headless desktop/vision fails closed. Default
context remains OFF; no hidden daemon/watcher or second learning KB.

Acceptance before writing Module 6 code: **40 context tests**, **1,169 full Python
tests passed** in 70.53 seconds with actual optional Chromium (no skips); **5 HUD
browser tests** in 5.97 seconds; **29 existing dashboard browser tests** in 113.27
seconds. Ruff/diff, VS Code TypeScript compile, isolated wheel and context resource
checks passed. Wheel SHA256 `eca73fcd82415e7f09b6d4a25f62384662217c0a40fd3fc16ce29a0042bd15fb`.
Logs: `/tmp/nexus-context-final-full.log`, `/tmp/nexus-context-hud-browser.log`,
`/tmp/nexus-context-dashboard-browser.log`, `/tmp/nexus-context-vscode.log`.
Both test servers stopped; no live account actions or service installation.
Native Win32/X11/VS Code UI, OCR accuracy and actual hosted/local model/account
acceptance are still host/operator checks, not inferred from scripted probes.
Earlier in-progress checkpoints above are historical, superseded by this gate.


### Historical Module 6 start — first working consumer

After the closing gate, extended **existing SessionStore** with bounded,
workspace/time-scoped weekly aggregates; `nexus evaluate` and HUD/TUI consume real
rows and separate human routing feedback. Advisory unused/problematic-role
observations never alter roles. Agent `ok` is labelled reported, not verified;
missing legacy token/retry/approval/cost telemetry stays unknown. Existing learning
and exploration ranking remain intact. Full measured instrumentation, why/trace,
replay and model/project/day cost histories remain Module 6 work, not completed
placeholders. See [evaluation progress and limits](evaluation.md).


### Historical verification after the first Module 6 slice

**1,183 Python tests passed**, no skips, 74.92 seconds (including actual headless
Chromium/Office cases); **41 context tests**, **13 evaluation tests**, **28 dedicated
HUD/TUI tests**. One upstream Starlette deprecation remains. Current-code HUD
browser **5 passed** (5.67 s), dashboard scripted regressions **29 passed** (112.40 s).
Ruff/diff, VS Code compile, isolated wheel resource verification and an actual
installed-wheel/default-OFF context/evaluation smoke test passed.
Logs: `/tmp/nexus-evaluation-accepted-full.log`,
`/tmp/nexus-evaluation-accepted-hud-browser.log`,
`/tmp/nexus-evaluation-accepted-dashboard-browser.log`.
Accepted code wheel SHA256:
`2f66c79cfe98fd0b7cd579ad65d8f8960a17a8c1522c1bbf3174310196418788`.
Only this final verification paragraph/doc count was added afterward; no subsequent
implementation edits. Temporary servers stopped; no live account actions, model
inference, service install, push or branch switch. Module 5 implementation/automated
acceptance is closed; Module 6's weekly-report slice is delivered and the remaining
Module 6 trace/replay/instrumentation/cost/demo gates stay explicitly open.


### Module 6 closing gate and all-six recheck

The historical first-slice/open items above are superseded by the current
[Module 6 implementation](evaluation.md) and [all-six audit](six-module-audit.md).
Existing roles, adapters, Studio/Jarvis, permissions and sessions.db are reused.
Actual local effects and scripted model/remote transports remain explicitly
distinguished. No native/live acceptance is inferred; no live push, service
installation, payment, cloud screenshot or deployed-store claim is made.

## Module 7 — World observatory (2026-10-29)

Adds public information to the existing HUD/app factory/WebSocket/theme, not another
app or learning KB. Bundled globe.gl/Three/Natural Earth/texture/Bengali font,
interactive SVG fallback, RSS country matching, Open-Meteo/USGS/public crypto,
TTL/disk/offline handling and strict existing auth are implemented. Operator-owned
`widget.yaml` controls bounded sources/TTLs/layers/language/theme. Feed text never
enters agents. Cointelegraph is gated on appropriate permission. See
[implementation, source review limits and acceptance evidence](world-widget.md).
Live public endpoint availability, complete publisher legal clearance and 60 FPS on
the target laptop are not inferred from mocked feeds or software-rendered tests.
