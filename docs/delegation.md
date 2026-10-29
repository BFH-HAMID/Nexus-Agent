# Module 5, first slice: durable delegation

This slice implements **delegation on the existing Jarvis queue/worker**. It is
not the finished Module 5 HUD/MCP/Telegram/context release. No second scheduler,
learning KB or global context collector is introduced. Context stays OFF.

```powershell
nexus delegate -C . create "Review failing unit tests and propose a scoped fix" --max-seconds 60 --mission dev
nexus delegate -C . tasks
nexus delegate -C . status JOB_ID
nexus jarvis -C . approve JOB_ID
nexus jarvis -C . start
nexus delegate -C . report JOB_ID
nexus delegate -C . cancel JOB_ID
nexus delegate -C . resume JOB_ID
```

Creation stores a normal `jarvis-job` with an immutable delegation specification.
It starts **approval-pending** even at L2/L3; it never starts a daemon/model implicitly.
Configure the existing `autonomy.yaml` runtime/local model deliberately. Individual
mutations still use existing per-action consent, roles, permissions and snapshots.
All Jarvis admission, duplicate, TTL, CPU/RSS, request/token/action/hour/day controls
remain authoritative. The per-task wall deadline is the smaller of the approved
5–600 second delegation limit and operator runtime limit; the actual parent
supervisor enforces it. Changing the approved spec invalidates task approval.

## Reports and actual checkpoints

The same job record durably stores bounded, ordered per-attempt checkpoints:
claim; actual selected-role events; tool-call start **after gate approval and before
the handler**; tool result/error; final outcome. Checkpoints contain call IDs,
argument hashes and observed metadata, not file bodies, credentials, tool output,
clipboard or screenshots. A tool call without a result, or with a reported error,
is highlighted as **uncertain**: effects may have applied. The final result is a
redacted existing-agent report/session reference, **not independent test proof**.
Reports identify the result's attempt so an old report cannot masquerade as a new
attempt's completed result. Queue state is not worker liveness; inspect the actual
Jarvis daemon heartbeat separately.

States are queued/running/blocked/waiting_approval/done/failed plus terminal
cancelled/rejected. Waiting action reviews are actual one-use Jarvis approval IDs,
not fake UI requests. Offline/disabled/L0 policy blockers are surfaced. An 80%
wall-time alert persists once per attempt and submits an ordinary inbox notice;
existing quiet/DND/native-notification rules apply. No timer callback or model
response is needed to fabricate the alert. It is measured by the parent worker.

## Crash recovery is explicit, never replay

Cancel revokes the existing lease. Resume is allowed only for error/interrupted
jobs within the existing three-attempt cap. It returns to **waiting_approval**;
run `jarvis approve` again. A still-running/orphaned lease is not guessed dead from
a PID: use existing daemon restart/reconciliation to revoke it first. Old worker
checkpoints/outcomes cannot authorize new changes under a new lease.

Resume starts a **new bounded attempt**, not mid-tool continuation or replay.
It preserves the prior checkpoint history and tells the agent to inspect existing
effects with read-only tools before making any new changes. Inspect uncertain
external outcomes yourself before granting new consent. Individual external/
destructive actions still need fresh reviews; a checkpoint is not an execution
command. Application guards do not provide a universal race/power-loss rollback.

## Explicitly pending in Module 5

- HUD/FastAPI/WebSocket and Textual consumers are now implemented; see [HUD](hud.md).
- Optional operator-priced local model allocation caps and 80% alerts are now
  implemented (below); actual invoice/electricity USD remains unknown.
- MCP/plugins and Telegram whitelist/PIN now have tested consumers; see their
  dedicated guides.
- Context/reference collection **last**, default OFF.

Dedicated tests use real SQLite jobs, actual tool/gate execution, lease revocation,
real approval waiting, deadline reservations, durable alerts and CLI parsing;
model responses used in these tests are scripted, not live Ollama acceptance.


Prior delegation-only verification: **22 dedicated delegation tests**, **1,056 full Python tests passed**
in 79.81 seconds with actual optional headless browser/Office integrations enabled.
Ruff/diff and wheel-content verification passed. Dashboard browser baseline remains
29 from Module 4 at that earlier slice; current HUD/TUI verification is in
[HUD documentation](hud.md). Existing
non-delegated job approval signatures remain backward-compatible.


## Operator-priced model allocation caps

`nexus delegate -C . create "TASK" --max-cost 0.05` signs an optional model-cost
cap into the exact task approval. Configure the pinned local model's existing
Workbench `pricing` entry with finite input/output USD-per-million rates and an
explicit source. Missing pricing **blocks dispatch without a reservation**; it
never silently assumes zero. Unpriced approved jobs are not starved behind it.

Each model wire attempt reserves its serialized input-byte upper allocation plus
its maximum output-token allocation at the snapshotted operator rates, before IO.
The next over-cap attempt is refused, including retries/learning calls. Rate changes
revoke remaining work. At 80% allocation an existing durable inbox alert is emitted
once; 80% time alerts remain independent. Reported usage exceeding the cap stops
further work. The final report separates calculated token cost from actual USD
billing, which remains unknown. Interrupted calls without reported usage never
become invented zero-dollar outcomes.

These are conservative **operator-priced local-model allocations**, not electricity
meters or provider-invoice guarantees; a server can report unexpected usage and
one in-flight request cannot be unsent. External MCP/broker/service fees are not
included or silently authorized by this cap. Existing local-only transport remains
mandatory. Cost ceilings are not permission to select a hosted model or spend money.
