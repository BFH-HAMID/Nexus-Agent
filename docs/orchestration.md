# Opt-in role orchestration: first Module 1 slice

This extends the existing role registry, model adapters, Studio DAG executor and
learning storage. It does **not** enable an autonomous daemon or implement the
six-module roadmap in full. The normal `nexus` single-role behavior is unchanged.

## Install and index

PowerShell, from your existing checkout (no reset/clean or policy changes):

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[orchestrator]"
nexus orchestrate --encoder lexical-hash index
nexus orchestrate --encoder lexical-hash index --watch
```

The default in `role-index.yaml` is **semantic Ollama embeddings** using an
already-installed `nomic-embed-text` model and loopback `/api/embed`. Provision that
model yourself before using `nexus orchestrate index`; Nexus does not download a
model automatically. `lexical-hash` is deterministic and entirely offline, but it
is **not semantic inference**. Both encoders use real FAISS cosine search.

The index stores bounded ID/name/category/tags/description/skills/tools metadata
and normalized vectors under ignored `.nexus/orchestration/`. It never copies a
full persona. Metadata and encoder settings are fingerprinted: a subsequent
invocation rebuilds when they change; `--watch` reloads layered roles and settings
every five seconds. There is no permanent watcher unless you run that command.
Interrupted/corrupt caches fail closed; rebuild explicitly with `index --force`.

## Route and plan (model calls, no project execution)

```powershell
nexus orchestrate --encoder lexical-hash route "Build an e-commerce website with login, admin panel and payment"
nexus orchestrate --encoder lexical-hash plan "Build an e-commerce website with login, admin panel and payment" --save-workflow
```

Routing performs two bounded JSON model calls through the **existing** provider
registry: cheap task/phase/complexity classification, then selection of 3–7 roles
from the hybrid top 15. Reasons, IDs and finite confidence are validated. Low
confidence returns one clarifying question and exits with status 2; answer it in
the next task description and reroute. Invalid output fails rather than inventing
a successful plan. Known secret formats are redacted before embeddings, model
payloads and artifacts; regex masking is not a guarantee of detecting every secret.

Hosted routing may send the redacted task and 15 metadata records to your existing
configured router. Persisted Workbench **local-only** policy is applied before
constructing providers and strips hosted fallback routes. Indexing itself never
uses a cloud embedding service. Simple phases choose an existing configured local
OpenAI-compatible adapter (or explicit `simple_agent`); complex phases retain the
role's existing adapter (or explicit `complex_agent`). Existing provider fallback
chains are reused. Configured local models must actually exist; there is no hidden
model download or automatic live availability probe.

A project plan uses the existing validated Studio schema:

```text
requirements -> review -> architecture -> review
  -> design / database -> reviews -> backend -> review -> frontend -> review
  -> security -> review -> qa -> review -> devops -> review -> documentation -> review
```

Each worker has role, model, inputs, expected outputs, criteria and a tool ceiling.
Reviewers are separately selected, read-only and restricted to correlated phase
handoff evidence IDs. A producer failure cannot be approved. Rejections repair the
responsible role at most three times, then open a human Studio inbox question and
block the phase. A human answer does **not** bypass the failed review. These checks
validate provenance, not the truth of an LLM's interpretation of evidence.

`--save-workflow` creates a version, not a run. Use the existing Studio workflow
review/run confirmation and existing tool approval prompts to execute it. No shell,
GitHub mutation, deployment or payment is executed by `route`/`plan`. Tool ceilings
only narrow a role's existing registry; they cannot grant new tools. There is no
new policy engine yet: the broader permissions/autonomy requirements remain in
Modules 2–5 and prompt warnings alone are not an authorization boundary.

Shared artifacts are `.nexus/orchestration/plans/<32hex>/plan.json`, `PLAN.md`, and
`status.json` once execution publishes state. The existing Studio graph, inbox and
live workflow/session evidence remain the UI; no synthetic HUD data was added.

## Existing learning, not a second knowledge base

```powershell
nexus orchestrate feedback approve "Build an API" --roles backend security --reason "Human-approved role selection"
nexus orchestrate feedback reject "Build an API" --roles frontend --reason "Wrong specialization"
```

Explicit redacted feedback is recorded in a table in the **existing `sessions.db`**,
scoped by workspace and covered by its retention purge. Prior session outcomes
plus feedback receive a small smoothed ranking weight and exploration bonus.
An historical `ok` is only an outcome proxy, **not proof that tests passed**. The
existing memory preferences are bounded and treated as untrusted router context;
existing agent execution continues to own procedural learning and memory writes.

## Honest limits and verification

- Module 1 is an opt-in first slice, not six finished modules. No new GitHub,
  permissions, daemon, context, MCP, Telegram or evaluation service is claimed.
- Only selected personas are sent to workers; the existing YAML registry still
  eagerly loads role objects from disk. There is no new disk-lazy role loader.
- The generated ten-phase project template is fixed, not an arbitrary LLM-produced
  graph. Writers and repairing reviewers serialize in a shared checkout. Existing
  independent read-only research/review can parallelize; isolated parallel writes
  and project-branch automation are not implemented by this slice.
- Unit/integration tests use scripted model replies and real local FAISS. They
  cover cache drift/corruption, redaction, local-only filtering, selection,
  confidence, schema/tool boundaries, repair/escalation/cancellation and artifacts.
- An actual 1,000-role `lexical-hash` index and cache hit were exercised locally.
  Live Ollama semantics, real hosted model routing and generated-project execution
  have **not** been verified. No live GitHub changes or payment operations ran.

See [the implementation ledger](agent-extension-plan.md) for the pending rollout.
