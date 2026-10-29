# Module 6 — measured learning, redacted traces and safe inspection/re-execution

Implementation and automated acceptance are complete. Authorized native/live
acceptance is a separate boundary, not inferred from scripted providers.

## Existing learning store, not a second KB

`nexus.agent.NexusAgent`, orchestration and existing model/tool adapters now record
bounded observations in **the existing `sessions.db`**. Legacy sessions and routing
feedback remain intact. No new learning database or automatically generated roles
are created. Prior policy, budget, response and error observers are composed and
restored; trace collection cannot disable permission or context privacy guards.

Captured observations include selected roles/routing candidates, redacted model
requests/final responses, actual wire attempts, fallbacks/errors, exact failed-tool
retries, tool specifications/arguments/results, command exits, approval decisions,
wall time, usage and historical rate snapshots. Streaming deltas are intentionally
not duplicated. `why` explains recorded decisions/reasons, **not hidden model
reasoning**. Model `ok`, actual command exit, peer review and explicit operator
assessment are separately labelled. Exit zero is not independent test certification.

Valid evidence-bound workflow reviewer verdicts link distinct completed target and
reviewer traces. Store-level structural checks reject malformed evidence lists and
blank/overlong review reasons without changing learning observations; actual
evidence membership is checked by the workflow, not certified by a local caller
supplying arbitrary IDs. Operator assessments require exact local confirmation:

```powershell
nexus evaluate -C WORKSPACE --days 7
nexus trace -C WORKSPACE inspect TRACE_ID
nexus trace -C WORKSPACE why TRACE_ID
nexus trace -C WORKSPACE feedback TRACE_ID --verdict accept --reason "Reviewed actual artifacts and verification evidence"
```

The role ranker retains its bounded semantic/lexical/history/exploration blend:
`0.55 semantic + 0.40 lexical + 0.05 observed quality + bounded exploration`.
Quality is smoothed from existing outcomes/routing feedback, command-success
fractions, latest reviews by source (operator weight 2, peer weight 1), and a small
exact-retry penalty capped at 0.02. This is an advisory prior, not an autonomous
correctness verdict. No automatic deletion/merge/edit of low-use roles occurs.
Time/cost are reported, not blindly used to penalize human approval wait or
incomparable tasks. CLI/HUD/TUI weekly reports show evidence, quality and unused
role advice; empty workspaces have no seeded achievements.

## Safe replay: effects OFF by default

```powershell
nexus trace -C WORKSPACE replay TRACE_ID --step 4
# Only when a NEW operation is actually wanted; review both fresh confirmations:
nexus trace -C WORKSPACE replay TRACE_ID --step 4 --execute
```

A step is its recorded `events[].step`, not a tool-call ordinal. Default replay
only returns stored, untrusted evidence: no model, tool, provider, approval or
external call runs. HUD/TUI offer authenticated **inspection only**; the HUD has
no execution endpoint.

Explicit CLI re-execution requires a selected unique successful file-tool step in
a complete finished trace, unchanged tool fingerprint, matching workspace,
nonredacted arguments, current path/permissions and **fresh dangerous outer
consent bound to trace/step/arguments/current target hash**. State and target are
rechecked after consent, then the normal inner tool gate still applies. The new
operation has a separate observed journal, `replay_trace`, linked to its source;
permission audit records fresh decisions. Source history is not overwritten.

Only `read_file`, `list_files`, `grep`, `write_file`, `edit_file` are eligible.
Terminal, GitHub, payment, plugin and uncertain/failed/running/truncated outcomes
are refused. Prior approval/scope is never reused. This is not crash recovery,
idempotency or a guarantee that an operator-approved overwrite is desirable.
Concurrent in-workspace writers remain a normal OS race; symlink/path/policy
boundaries and review-time target changes fail closed.

## Honest historical costs

Measured reports group model/agent usage and calculated USD by **UTC day and exact
workspace/project**, using per-response copied operator rates from the existing
pricing settings. Changing today's price cannot retroactively reprice old traces.
Missing explicit token usage, rate/source, failed/unmatched wires, running or
incomplete journals and bounded-query truncation remain unknown. An SDK usage
object without input/output counts is not evidence of zero tokens. Known subtotals
are distinguished from unknown totals; `actual_cost_usd` is always null (no invoice
source). No calls in a completed scope is a genuine zero model-inference subtotal,
not zero hosting/billing cost.

Legacy detailed counters stay unknown, never invented. Reports cover instrumented
scopes **started in the selected window**, not a universal invoice ledger. Routing
and explicit re-execution have their own scope kinds. Electricity, external tool
fees and embedding transports outside the provider registry are excluded.
Existing delegation's operator-priced allocations/caps remain a separate consumer.

## Privacy, retention and bounds

Traces remain local in sessions.db; no telemetry service/upload is introduced.
Known credentials are masked, including raw, JSON-escaped, URL/base64 variants;
credential keys/assignments and card-like values are masked. Image contents are
omitted. This is not a universal guarantee for arbitrary unknown secret encodings.
Context remains OFF by default and cloud vision still requires separate consent.

Per journal: 1,000 events / approximately 1 MB / 500 model responses; strings,
collections and nesting are bounded. Truncation/persistence loss marks the journal
incomplete and disallows re-execution. Indexed window reads are bounded to
1,000 observations / 4 MB, 1–365 days, exact workspace; group/role output truncation
is explicit. Existing explicit session purge also removes associated and aged
orphan observations. No automatic role deletion or hidden retention daemon starts.

## Verification

```powershell
python -m pytest tests/test_trace.py tests/test_evaluation.py tests/test_extension_demos.py -q
python -m pytest -q
```

See the [six-module audit and demo boundaries](six-module-audit.md). Actual local
file/process/SQLite/permission paths are exercised; remote/model/native fixtures
are labelled. No live vendor/account, Windows service, payment or deployed-store
acceptance is asserted.
