# Chief-of-Staff Multi-Agent Orchestrator

`nexus chief` is Nexus Agent's **opt-in, multi-agent, goal-driven Chief-of-Staff orchestrator**. Given a single natural-language goal (or an optional `pipelines.yaml` workflow template), it decomposes the work into a validated dependency DAG across existing roles (`roles.yaml` / `roles.d/` / `.nexus/roles.yaml`), executes independent branches concurrently using isolated per-step `NexusAgent` workers, reviews completed phases with a read-only critic gate, enforces model cost routing and hard USD cost caps, and coordinates bounded recovery when a step fails.

## Safety & Default-OFF Policy

`chief.yaml` at the workspace root (or package default) controls Chief-of-Staff behavior and is **OFF by default**:

```yaml
schema: 1
enabled: false
```

`chief.yaml` is protected by `Permissions.operator_control()` alongside `permissions.yaml`, `autonomy.yaml`, and `voice.yaml`: agents cannot write, edit, or delete `chief.yaml` via workspace tools. Setting `enabled: true` is an explicit operator edit.

Key guardrails:
- **Plan-first `"Go?"` gate**: Under default `autonomy.yaml` (`level: L1`), planned missions pause in `awaiting_approval` and present `"I will use ... Estimated cost $... Go?"` before starting any worker. Even under `L2`/`L3`, any plan with high-risk steps requires explicit approval when `require_approval_for_high_risk: true`.
- **Fresh per-tool approvals preserved**: Every worker runs through `NexusAgent` and the existing `ApprovalGate` + `Permissions` stack (`permissions.yaml`, `FORCED_REVIEW`, `PathGuard`, `Role.denied_tools`, second-factor challenges). Approving a mission start (`"Go?"`) or passing `--yes` never bypasses `permissions.yaml` or dangerous/external gates.
- **Bounded recovery**: Retries (`max_retries`, capped `<= 5`) and escalation/critic repair loops (`max_depth` from `autonomy.yaml`, capped `<= 5`) are strictly bounded and audited (`AuditLog` under `$NEXUS_HOME/chief/<workspace-hash>/missions/`).
- **Hard USD cost cap**: Each mission tracks actual and estimated costs against `min(requested_cap, chief.max_cost_usd)` and aborts cleanly (`aborted_cost_cap`) before launching steps that would breach the budget.
- **Secret redaction**: Mission snapshots and event logs redact secrets via `studio.redact` + `redaction.mask`.

## How It Works

1. **Goal → DAG planning**: `ChiefOfStaff.plan(goal)` asks the planner model (or heuristic fallback / `--pipeline`) for a 2–7 step DAG (`steps` with `id`, `phase`, `role`, `goal`, `depends_on`, `read_only`, `risk`, `complexity`, `budget_weight`) and validates acyclicity, unique IDs, valid role lookup via `RoleIndex` / `TeamBook` / `Router`, and non-empty goals.
2. **Role selection & model tiering**: Each step picks an appropriate role and routes its model tier according to `chief.yaml` (`cheap` → DeepSeek / local Ollama for research/docs/boilerplate; `capable` → Gemini / local Ollama for code/architecture/reviews; `escalation` → fallback tier on retry). Respects `local_only` Workbench policies.
3. **Parallel execution & handoffs**: Independent DAG branches run in parallel up to `min(mission.concurrency, max_concurrency)` via isolated `NexusAgent` workers using `_IsolatedProviderProxy` and thread-safe cloned configs. Each worker injects upstream `finish` handoff summaries (`handoffs`) into its prompt without sharing raw message histories across unrelated branches.
4. **Failure recovery**: When a step fails:
   1. Exponential backoff retry on the current agent (`max_retries` from `chief.yaml`, default 2).
   2. Reassign to a secondary role/escalation tier (`escalation` model tier) if the role has an alternative or escalation agent.
   3. Escalate clearly to the user (`status: escalated`) with `mission.error` and step audit trail if recovery fails or `depth >= max_depth`.
5. **Post-phase critic loop**: When `critic_enabled: true`, once all steps in a phase complete, a read-only critic (`code-review` / `qa` / `security-audit`) inspects the phase deliverables and outputs a JSON verdict (`{"passed": true|false, "notes": "...", "fix_goal": "..."}`). Rejections enqueue a bounded `fix` step and rewire downstream dependencies up to `max_depth`.

## CLI Commands

Enable Chief-of-Staff by editing `chief.yaml`:

```yaml
enabled: true
```

Then run:

```bash
# Plan and execute interactively (prompts "I will use ... Go?")
nexus chief "Build a user auth API with tests"

# Plan only (prints DAG & summary without starting execution)
nexus chief "Audit auth module for SQL injection" --plan-only

# Base DAG on a configured pipeline with explicit budget & parallelism
nexus chief "Ship rate limiting" --pipeline ship-feature --cost-cap 1.50 --concurrency 3

# Non-interactive confirmation ("Go?")
nexus chief "Write module docs" --confirm GO --json

# Live status narration verbosity override (silent | milestones | chatty)
nexus chief "Build auth API" --confirm GO --verbosity chatty

# State-grounded live status Q&A (reads live mission state & logs; never guesses)
nexus chief ask "what is the status?"
nexus chief ask "why did that fail?" --id <mission_id>

# Mission lifecycle controls
nexus chief status
nexus chief status <mission_id>
nexus chief pause <mission_id>
nexus chief resume <mission_id>
nexus chief approve <mission_id> --confirm GO
nexus chief cancel <mission_id>

# Crash recovery, history, replay, scheduling, and self-monitoring
nexus chief recover [--resume]
nexus chief history [--status ...]
nexus chief replay <mission_id> [--step STEP_ID] [--trace-step N] [--why] [--execute]
nexus chief schedule list
nexus chief schedule add "every day at 7 run the market report"
nexus chief monitoring
```

## Crash Recovery, Idempotency, Watchdogs & Replay

- **Checkpointing & crash recovery (`nexus/idempotency.py`, `nexus/chief.py`)**: every completed step persists its `handoff_summary`, `artifacts`, `trace_id`/`trace_ids`, `session_id`, and `checkpoint_at` atomically. On restart or `nexus chief recover` / `nexus chief resume`, already-completed steps (`status == "done"`) are never re-executed, and external-effect tools (`git`, `gh`, `git_prepare`, `gh_create_pr`, `gh_merge_pr`, `browser_action`, `project_action`, `mcp_*`) check a durable SHA-256 idempotency ledger scoped to `(mission_id, step_id)` so external side effects do not repeat.
- **Watchdogs (`nexus/watchdog.py`)**: opt-in via `autonomy.yaml` (`watchdog.enabled: true`). Halts stuck steps (`step_timeout_seconds`), infinite identical tool-call loops (`max_identical_tool_calls`), and runaway cost (`max_cost_usd` / `cost_cap_usd`) and alerts via `NotificationRouter`.
- **Mission history & replay**: `ChiefOfStaff.history()` and `ChiefOfStaff.replay()` reuse `nexus/evaluation/replay.py` (`inspect`, `why`, and fresh-approval `reexecute` restricted to `SAFE_TOOLS`).

## HUD / Server Endpoints & Voice Integration

- **HUD Snapshot**: `HUD.snapshot()` exposes `snapshot()["chief"]` and `snapshot()["monitoring"]` (`queue_depth`, `running_missions`, `cost_today`, `error_rate`).
- **HTTP Endpoints** (available on both `nexus gui`/`serve` and the FastAPI HUD app, protected by existing token/session/origin auth):
  - `GET /api/monitoring`
  - `GET /api/schedules` and `POST /api/schedules`
  - `GET /api/chief/history`
  - `GET /api/chief/missions`
  - `GET /api/chief/missions/<mission_id>`
  - `GET /api/chief/missions/<mission_id>/replay` (read-only `inspect`/`why`)
  - `POST /api/chief/missions`
  - `POST /api/chief/missions/<mission_id>/approve`
  - `POST /api/chief/missions/<mission_id>/pause`
  - `POST /api/chief/missions/<mission_id>/resume`
  - `POST /api/chief/missions/<mission_id>/cancel`
- **Voice Integration, Live Narration & Status Q&A** (`nexus/voice/pipeline.py`, `nexus/notifications.py`):
  - Spoken triggers such as `"chief build a REST API"` or `"start a mission to refactor auth"` trigger `ChiefOfStaff.plan`, speak `"I will use ... Estimated cost ... Go?"`, and wait for spoken `"yes"`/`"go"` or `"no"`/`"cancel"`.
  - Natural status questions (`"what is the status?"`, `"what are you doing?"`, `"why did that fail?"`) are answered strictly from live `_ACTIVE_RUNTIMES` + `Mission` state and logs via `answer_status_question()` — never guessed.
  - When `autonomy.yaml` enables `status_narration` (or `--verbosity milestones|chatty` is passed), `ChiefOfStaff` emits concise redacted step-boundary lines and routes notifications by importance via `NotificationRouter` (spoken when present, Telegram or desktop notification when away; both disabled by default).

## Known Limits

- Planner LLM quality depends on the configured provider; if the planner returns non-JSON output, `ChiefOfStaff` falls back to a validated 3-step heuristic DAG (`research` → `backend`/`general` → `qa`).
- Pause/resume operates at step boundaries: in-flight steps finish cleanly before a mission enters `paused`, and resuming restarts from remaining `pending` (plus interrupted `running`) steps rather than replaying partial mid-turn LLM tool loops.
- Cost tracking uses `chief.yaml` `estimated_rates` or `provider.last_usage` when available; if a custom provider omits token counts, step costs fall back to conservative estimates.
- Live multi-provider execution requires valid API keys or a local Ollama endpoint; offline tests exercise all flows via deterministic `FakeProvider` doubles.
