# Module 4 — guarded Jarvis daemon

The Module 4 implementation reuses Nexus agents, roles, routing, model transport,
learning, Module 2 project actions, Module 3 permissions/snapshots and existing
workspace-scoped Studio SQLite records. It does not introduce another learning KB.
**Default L1, runtime disabled, read-only, offline, no triggers, context OFF.**
Module 5 delegation is now started; see [its explicit scope](delegation.md). Native Windows/service/GitHub/Ollama acceptance requires
an operator's actual host/accounts; scripted transport tests are not that certification.

## Start and control (PowerShell)

Install Nexus in the Python environment first (`python -m pip install -e .`). The
worker uses `python -I -m nexus`, never cwd/PYTHONPATH executable imports.

```powershell
nexus jarvis -C . status
nexus jarvis -C . start
nexus jarvis -C . daemon-status
nexus jarvis -C . pause
nexus jarvis -C . resume
nexus jarvis -C . stop
nexus jarvis -C . kill
```

Start returns durable intent, **not fake readiness**. A matching-instance heartbeat
and OS lock confirm liveness. Only one daemon/worker per workspace is allowed.
Stop/kill/pause are cooperative controls. Owned child processes are terminated;
persisted PIDs are informational, never blindly signalled. An unresponsive daemon
is reported honestly. System emergency-stop and the existing Windows hotkey remain
authoritative. Invalid policy cannot prevent stop/kill. No task automatically replays
on crash: the old lease is interrupted; explicit retry and fresh approval are required.

```powershell
nexus jarvis -C . enqueue "Inspect failing tests and propose a fix" --mission dev --priority 1
nexus jarvis -C . approve JOB_ID
nexus jarvis -C . reject JOB_ID
nexus jarvis -C . cancel JOB_ID
nexus jarvis -C . retry JOB_ID
nexus jarvis -C . explain JOB_ID
```

The actual task is shown before exact `APPROVE QUEUED TASK JOB_ID` consent.
Approval of a task is **never approval of its individual mutations**. Jobs have
priority, expiry, attempts (max 3), depth, duplicate/cooldown and admission-estimate
limits. Job/notice/action history is bounded at 1,000; archive trusted state before
continuing once full. Multi-record Studio updates are not an atomic cross-record
transaction. Reservations are conservative and never silently refunded after a crash.

## Actual execution policy

Edit the operator-owned root `autonomy.yaml`; model file tools cannot edit it,
`permissions.yaml`, private paths or trusted Nexus state.

```yaml
level: L1
runtime:
  enabled: true
  local_model: qwen2.5-coder:7b # install this local model yourself; example, not a probe result
  mode: read_only
  connectivity: offline
  write_roots: []
  projects: []
  auto_sources: []
  allow_safe_writes: false
triggers: []
```

Keep the bounded runtime defaults in the shipped file unless deliberately changing
them. Enabling runtime is explicit operator authority, not inferred from an event.
Only loopback HTTP Ollama inference is permitted, with the model pinned, proxies
and redirects disabled, no cloud adapter/fallback and no known `:cloud` aliases.
The operator must ensure the local server/model is not itself a remote proxy.
Existing roles and learning are reused, with request/output limits also applied to
learning calls. Input bytes, requests, output tokens, wall time, parent-observed RSS,
CPU pressure and UTC-hour/day run/request/token/action reservations plus daily
byte/time reservations are bounded. Per-job action counts are enforced on the actual
tool registry, including denied attempts. Token reservations conservatively allocate
maximum input bytes plus maximum output tokens; they are not invoiced usage. Daily keys are
captured once; active deadlines use monotonic time. Tokens are actual reported
usage; local inference's USD/electricity cost is **unknown**, not fake zero billing.

### L0–L3 semantics & `risk_classes` / `status_narration` / `notifications`

| Level | Admission and writes |
|---|---|
| L0 | No new admission/dispatch; daemon pauses. |
| L1 (default) | Exact human task approval. Mutations separately reviewed. |
| L2 | Only explicitly listed `runtime.auto_sources` can auto-admit, with runtime enabled. Scoped mutations still separately reviewed. |
| L3 | Same scoped admission; optional non-destructive new-file/append auto-writes only with `allow_safe_writes: true`, `mode: reviewed_write`, explicit `write_roots`, and explicit per-tool `AUTO_ALLOW` in `permissions.yaml`. |

In addition to L0–L3 mission admission, `autonomy.yaml` declares operator-owned tool risk classes, live narration verbosity, and notification routing (`nexus/autonomy.py`, `nexus/notifications.py`):

```yaml
risk_classes:
  SAFE: automatic          # read/search/draft/summarise
  MEDIUM: automatic_notify # bounded edits, local tests, git branches, draft PRs
  DANGEROUS: fresh_approval # push main, deploy, send, delete, spend, change permissions, unknown tools

status_narration:
  enabled: false           # opt-in step-boundary narration
  verbosity: milestones    # silent | milestones | chatty
  cooldown_seconds: 0.0
  max_chars: 180

notifications:
  enabled: false           # opt-in routing by importance & presence
  presence: auto           # auto | present | away
  present_ttl_seconds: 300
  voice_when_present: true
  away_channel: auto       # auto | telegram | desktop | none
```

Unknown or undeclared tools ALWAYS classify as `DANGEROUS` (`fresh_approval`), and `DANGEROUS` cannot be downgraded by `autonomy.yaml` or bypassed with `--yes` / `AUTO_ALLOW`. Both `status_narration` and `notifications` are **off by default**.

Removing a source or downgrading L2/L3 revokes unclaimed autonomous admission;
the job becomes approval-pending. Already-running jobs are revoked on policy change.
Autonomy never grants unavailable tools or bypasses role whitelists. A root-wide
`AUTO_ALLOW` is **not** enough for L3 file auto-writes. Overwrites, edits, deletions
and dangerous/external actions remain separately reviewed; raw shell/Git/browser/
Office/payment tools are not exposed to background agents.

### Scoped edits and fresh action reviews

Set `mode: reviewed_write`, then list narrow workspace-relative `write_roots`
(e.g. `projects/shop`, never the whole workspace). File writes/edits/deletes reuse
actual Module 3 snapshot-backed tools and existing undo, not a parallel editor.
The running Nexus package, private paths and operator policies remain protected.

```powershell
nexus jarvis -C . approvals
nexus jarvis -C . allow APPROVAL_ID
nexus jarvis -C . deny APPROVAL_ID
```

Review the stored arguments, diff/proposal and expiry. Allow requires the exact
`ALLOW ACTION APPROVAL_ID DIGEST` shown in that row. It is one-use, tied to the
job lease, policy and deadline. Changed file preimages invalidate consent. Secret
arguments/previews are refused before persistence/logging. Cancellation, policy
change or expiry revokes the waiting action; the gate rechecks authority after
approval. These guards/snapshots are not complete OS confinement or a universal
race/power-loss/ACL rollback guarantee.

For existing Module 2 mapped projects, explicitly set `projects: [shop]` and
`connectivity: online`. Dev/default missions may then see `project_action` if their
role allows it. It delegates conventional commit, scanned push, exact-head CI-gated
PR and merge to the **same Module 2 preview/executor**, not raw Git. A tool request
review and a second exact-preview review are required. Credentials remain
`GITHUB_TOKEN` environment/workspace `.env` only; credentials are never printed.
Diff/head/scans/CI/identity are revalidated at execution. No live repository changes
are made by tests. Repository hooks/signing in a local commit are trusted-code
execution explicitly disclosed in the existing preview, not an OS sandbox.

## Automatic sources: explicit opt-in, bounded, untrusted

At most ten root-policy triggers. Their task templates are operator-owned.
Sampled file bodies, inbox text, CI names/logs and system details are **not** copied
into the model task or durable trigger state. A trigger cannot approve an action.
All sources pass the same queue/router, depth/TTL/duplicate/cooldown/budget controls.

```yaml
triggers:
  - {id: tasks, kind: file, path: tasks.md, task: "Review updated tasks.md"}
  - {id: todos, kind: todo, path: projects/shop/tasks.md, task: "Inspect remaining TODOs"}
  - {id: build, kind: build, path: projects/shop/build-result.json, task: "Investigate the failing build"}
  - {id: commits, kind: git, poll_seconds: 60, task: "Review changed Git metadata"}
  - {id: checks, kind: ci, repo: OWNER/REPO, ref: main, poll_seconds: 60, task: "Investigate failed GitHub checks"}
  - {id: resources, kind: system, metric: memory, threshold: 85, poll_seconds: 60, task: "Investigate memory pressure"}
  - {id: results, kind: inbox, poll_seconds: 60, task: "Review new local results"}
  - {id: idle, kind: idle, after_seconds: 300, poll_seconds: 60, task: "Review outstanding project documentation"}
  - {id: daily, kind: schedule, every_seconds: 86400, task: "Prepare today's project review"}
```

- **File:** baseline then content-hash changes; bounded 64 KB regular files.
- **TODO:** literal TODO/FIXME in the explicitly named file; emits on new/changed
  relevant artifact, including a first existing TODO. No recursive disk crawling.
- **Build:** consumes a bounded local JSON result artifact, `{"exit_code": 1}`
  indicates failure. The watcher does not execute build commands or certify tests;
  the actual build producer must write this artifact. It cannot substitute for
  Module 2's real remote exact-head CI gate. Malformed artifacts fail closed.
- **Git:** reads bounded local `.git/HEAD`, symbolic ref/packed refs and index hashes.
  Never runs `git status`, filters or hooks. It requires an ordinary local `.git`
  directory, not an external worktree/gitdir pointer; use explicit file/CI events
  for such checkouts. Unstaged content needs file watchers, not index metadata.
- **CI:** online-only fixed GitHub GET endpoints for an explicit repository/ref;
  validates immutable head and complete bounded check-run response (max 100).
  Failed/cancelled/timed-out/action-required completed runs emit. No log ingestion,
  arbitrary URLs, redirects, CLI-auth fallback or publishing. Minimum 60-second poll.
- **System:** actual psutil CPU/memory or workspace `disk_free` percentage crossing
  a configured threshold. State transitions, not every raw fluctuation, emit.
- **Inbox:** notices from real stored job results, not invented messages; child
  jobs carry the result's parent ID so feedback stops at the message-depth limit.
- **Idle:** **Nexus queue inactivity**, not keyboard/mouse surveillance. No screen,
  microphone, global-input or active-window collection. A pending/active job blocks
  idle work. The trigger starts only after the configured quiet queue interval.
- **Schedule:** persisted intervals, 60 seconds–7 days.

Pending events survive a full queue/cooldown and retry when capacity returns.
Probe errors retain only error class, no raw remote/credential details. One daemon
probe thread prevents slow network/native delivery from blocking lifecycle/resource
supervision. Network revocation/removal is rechecked before admitting probe results.
Manual integration remains available: `nexus jarvis -C . event SOURCE "TASK" --parent JOB_ID`.

## Offline → online reconciliation

`runtime.connectivity` is an **operator-declared network authority**, not an active
Internet ping. Both modes keep inference local. Offline CI probes persist a deferred
state without network access; online retries reconcile actual latest check state,
not fabricated success. Authentication/network failures back off and remain deferred.

```powershell
nexus jarvis -C . enqueue "Review a network project" --online
```

An approved `--online` job stays queued offline without spending its model reservation.
It becomes eligible after explicit `connectivity: online`, within its original TTL
and approval scope. Policy changes during active work revoke that lease; retry is
explicit. **Partly completed mutations are never automatically replayed**. After
uncertain external outcomes inspect the remote before making a new approved request.

## Inbox, notifications and missions

```powershell
nexus jarvis -C . inbox
nexus jarvis -C . digest
nexus jarvis -C . acknowledge NOTICE_ID
nexus jarvis -C . notify
nexus jarvis -C . quiet on
nexus jarvis -C . dnd 60
nexus jarvis -C . delivery native
nexus jarvis -C . delivery local
```

Inbox/digests reflect actual persisted results plus pending task/action reviews.
Native review counts include approval-pending event tasks, not just completed jobs. Local pull is default. Native push
is explicit opt-in: notify-send on Linux, PowerShell/.NET balloon on Windows,
osascript on macOS. Requires a working user desktop/notifier. **Counts only**, no
task text/key export; honors quiet/DND, deduplicates, limits attempts to once/minute,
and retains failed notices. Successful OS submission is not proof of display/read.
`delivery off` disables background push; explicit inbox/notify still works. No native
notification or OS service was installed/launched as an acceptance claim in this sandbox.

Missions reuse existing routing/learning: Dev scoped analysis/edits and reviewed
mapped project actions; Trading local supplied-data/risk analysis only (no orders);
Pentest authorized local artifact review only (no scanners/exploitation); Study
explanations/exercises/plans. No trading credentials or unrestricted shell surface. Explicit action decisions
for an actual selected role feed the existing SessionStore routing-feedback API;
consent is labelled as consent, not proof that tests passed. No second learning KB.

## OS user services: reviewed manifests and explicit installer

```powershell
nexus jarvis -C . service --platform windows --user "$env:USERDOMAIN\$env:USERNAME"
nexus jarvis -C . service --platform systemd
```

Returns exact escaped XML/unit plus installation/removal commands. Add `--install`
on the actual target host to review the full manifest and enter the exact
`INSTALL SERVICE FILENAME DIGEST` confirmation. The installer writes a new private
user configuration without overwriting an existing file, then invokes the actual
user manager (`systemctl --user` or `schtasks /Create`, never elevation). A partial
manager failure is explicitly reported with the retained configuration path; inspect
before retrying. Manager acceptance is not a fake running/readiness confirmation.
Without `--install` the command only exports for review/manual installation. Save the
`content` to the displayed filename, review, then execute those commands on the
actual OS. No implicit installation, elevation, stored password or execution-policy
change. Windows uses Task Scheduler InteractiveToken/LeastPrivilege at logon;
systemd uses `--user`, control-group termination and bounded restart backoff.
Set the same trusted NEXUS_HOME/service environment as your interactive installation.
Native manager installation/startup must be verified on the target host; XML/unit
fixture tests are not live Windows/systemd acceptance.

## Verification and remaining host acceptance

Dedicated tests cover real persistence/leases/budgets/actual file snapshots,
L1–L3 admission/revocation, per-action reviews, changed-preimage/cancel/secret denial,
real subprocess daemon lifecycle, source artifacts, deferred CI, native-delivery
contracts and slow-probe stop responsiveness. Protocol fixtures are explicitly
**not live Ollama inference**, and mocked GitHub/native OS transports do not claim
live account/platform acceptance. Final current Python regression: **1,034 passed in 78.22 seconds**, including the
actual headless browser/Office file tests; **96 Jarvis tests**. Dashboard regression:
**29 passed**, using scripted APIs, not live model/account actions. Ruff/diff and
wheel verification are recorded in `docs/agent-extension-plan.md`.

Host checks: installed local model inference; native Windows Task Scheduler and
notification display; systemd user startup; authorized live GitHub CI/PR flow.
Module 3's pre-existing native GUI/Docker/Office acceptance limitations still apply.
Module 5 HUD/MCP/Telegram/context and Module 6 evaluation remain separate;
context collection stays OFF and is implemented last.

## Long-running missions, timezone scheduling, watchdogs & self-monitoring

`autonomy.yaml` keeps `level: L1`, `runtime.enabled: false`, `scheduler.enabled: false`,
and `watchdog.enabled: false` by default.

- **Timezone-aware scheduler (`nexus/scheduler.py`)**: supports standard 5-field cron (`0 7 * * *`, `@daily`, `@hourly`) plus English and Bangla natural-language schedules (e.g., `"every day at 7 run the market report"`, `"next Friday remind me"`, `"প্রতিদিন সকাল ৭টায় রিপোর্ট চালাও"`, `"আগামী শুক্রবার বিকাল ৩টায় মনে করিয়ে দিও"`). Resolves user timezone via `zoneinfo.ZoneInfo` from `autonomy.yaml` (`scheduler.timezone` / `timezone`), `agents.yaml` (`settings.timezone`), or `NEXUS_TIMEZONE`. Persists schedules under `$NEXUS_HOME/scheduler/` and integrates with `Daemon.run()` while still obeying `L1` approval gates.
- **Watchdogs (`nexus/watchdog.py`)**: when enabled (`watchdog.enabled: true` in `autonomy.yaml`), detects stuck steps (`step_timeout_seconds` with no progress), repeated identical `(tool, args)` infinite loops (`max_identical_tool_calls`), and runaway cost (`cost_cap_usd`), stops the mission immediately, logs `watchdog.triggered`, and alerts via `NotificationRouter`.
- **Self-monitoring (`nexus/monitoring.py`)**: computes real `queue_depth`, `running_missions`, `cost_today`, and 24h `error_rate` exposed at `/api/monitoring`, `HUD.snapshot()["monitoring"]`, `GET /api/mission/summary`, `nexus jarvis monitoring`, and `nexus chief monitoring`.

## Opt-in Proactive Engine (`proactive.yaml`)

`proactive.yaml` ships **disabled by default** (`enabled: false`, every subsection and monitor off) and is operator-owned (`Permissions.operator_control()` and `autonomy._PROTECTED_POLICY_NAMES` refuse agent file edits).

When explicitly enabled by the operator in `proactive.yaml`:

- **Morning brief (`morning_brief.enabled: true`)**: aggregates up to 6 real local/configured sources (weather via `nexus.hud.world`, Google Calendar, Gmail unread count, pending tasks/schedules/reminders, system/CI/server health, and curated RSS news topics). Generates a short spoken digest plus full HUD text, delivering once per local day (or on voice greeting) via voice when the user is present or Telegram/desktop when away.
- **Voice reminders & recurring tasks (`voice_reminders.enabled: true`)**: parses English and Bangla spoken utterances (`"remind me at 5 to call Mom"`, `"remind me in 10 minutes to stretch"`, `"schedule a recurring task every day at 9am to run tests"`, `"আমাকে বিকাল ৫টায় মনে করিয়ে দিও"`) into `ReminderStore` reminders or `Scheduler` tasks (recurring schedules still obey `L1` approval before executing tools).
- **Opt-in monitors (`monitors.enabled: true`)**: evaluates explicit rules (`website`, `server`, `github_ci`, `disk`/`cpu`/`memory`, `price`/`market`, `log_pattern` guarded by `Permissions.path()`, and `rss_keyword`) with per-monitor `threshold`, `cooldown_seconds`, and `severity` (`info`, `warning`, `urgent`).
- **Rate-limited suggestions (`suggestions.enabled: true`)**: proposes one bounded action at a time (failing build, meeting starting soon, deadline tomorrow), caps proactive speech at `max_speech_per_hour`, and remembers `"stop suggesting this"` across restarts in `$NEXUS_HOME/proactive/<workspace-hash>/state.json`. Accepted suggestions enter the `Queue` as `awaiting_approval` under `L1`.
- **DND, focus mode & channel routing**: enforces scheduled DND windows (including overnight windows like `22:00`–`07:00`) and spoken `"focus mode"` (`nexus jarvis proactive focus --minutes 60`), blocking `info`/`warning` interruptions unless `allow_urgent_override: true` and `severity == "urgent"`. Routes `info` → HUD toast (`#toasts` via `alert.new` SSE), `warning` → HUD + desktop/voice (if present) or Telegram (if away), and `urgent` → voice + HUD + Telegram.

CLI & HTTP routes:

```powershell
nexus jarvis -C . proactive status
nexus jarvis -C . proactive tick
nexus jarvis -C . proactive brief --present --greeting
nexus jarvis -C . proactive remind "remind me at 5 to call Mom"
nexus jarvis -C . proactive focus --minutes 60
nexus jarvis -C . proactive suggest
nexus jarvis -C . proactive stop-suggesting
nexus jarvis -C . proactive accept [SUGGESTION_ID]
```

Authenticated endpoints: `GET /api/proactive/status` and `POST /api/proactive/action` (plus `HUD.snapshot()["proactive"]`).

