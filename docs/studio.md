# Nexus Studio

Open **Studio** from the dashboard top bar (`nexus gui` or `nexus serve`).
The selected 45 additions now have implementations and controls. The table below
maps **every selected ID** to its implementation and states the real boundaries.
No production screen populates demo conversations, invented telemetry, model
answers, test results, or simulated approval decisions.

## Feature checklist

| ID | Feature / entry point | Behavior and boundary |
|---|---|---|
| 1 | Sessions: tabs | Workspace-persisted conversations; tasks use the serial queue |
| 2 | Sessions: search | Searches saved conversation records, not invented examples |
| 3 | Sessions: edit/branch | Creates a new branch, retaining the original messages |
| 4 | Sessions: attachments | Up to five UTF-8 text/code files, 300 KB total; binary/NUL content refused |
| 5 | Evidence: context | Captures canonical input **and actual OpenAI/Anthropic JSON request bodies**, after tool conversion; redacted; authorization headers excluded |
| 7 | Sessions: templates | Save and reuse task text |
| 8 | Evidence: acceptance checklist | Human decisions are persisted and explicitly distinguished from automated proof |
| 9 | Inbox | Blocking `request_clarification` tool; answer, timeout and cancellation; workflow approvals also appear here |
| 10 | Policy: notifications | Explicit browser permission + opt-in; completion events observed by the open page |
| 11 | Evidence: hunks | Select diff operations; updates arguments/rotates approval ID, never silently approves |
| 12 | Evidence: checkpoints | Pre-write/edit/delete snapshots for bounded supported text-file tools |
| 13 | Evidence: rollback | Review diff, type confirmation; refuses to overwrite manual changes or active runs |
| 14 | Approval conflict guard | File hashes and approval IDs reject stale previews/selections |
| 15 | Runtime: checkout/container | Detached committed worktree + actual constrained Docker execution; worktree alone is not an OS sandbox |
| 16 | Evidence: test explorer | Actual pytest exit/output and parsed JUnit cases/status/duration |
| 17 | Evidence: diagnostics | Actual Ruff/mypy output, with best-effort file/line parsing |
| 18 | Runtime: dev apps | Explicit argv start, process-group stop, bounded logs, actual listener observation, iframe/new-tab preview |
| 19 | Evidence: dependencies | Actual dependency manifest/lockfile content and diffs |
| 20 | Evidence records | Subprocess exit codes, source fingerprints, commands and tool records; model prose is not test proof |
| 21 | Workflows: builder | Editable SVG node/edge graph and forms; click a node to edit/delete; saved versions immutable |
| 22 | Workflow conditions | Dependency success/failure/always branches; validation rejects cycles/unknown references |
| 23 | Workflow handoffs | Inspect recorded run/node inputs, results, task IDs and evidence IDs |
| 24 | Parallel research | Up to four fresh read-only research/reviewer agents; execution/Git/learning tools refused; writes serialize |
| 25 | Independent reviewer | Fresh role agent receives untrusted handoff plus actual verification records; no write tools; review is not guaranteed correct |
| 26 | Policy: role permissions | Read/write/shell/network-tool gates; model transport routing remains a separate local-only policy |
| 27 | Workflow step budgets | Turn/tool/token/time ceilings, conservative input preflight + output caps and observed usage; provider deadlines/cooperative cancellation |
| 28 | Workflow versions | Every save makes an immutable family/version record; load any saved version |
| 29 | Workflow dry-run | Pure graph/role/dependency/condition/budget validation; lists writing nodes; executes nothing |
| 30 | Escalations | Tool names or role/path/command-substring rules force normal approval even with auto-yes; conditional workflow approval nodes |
| 33 | Model profiles | Saved temperature, output cap and client context-compaction budget derived from documented window; select on a conversation |
| 34 | Context warning | Clearly labelled characters/4 warning, not tokenizer-measured precision |
| 35 | Ollama controls | Explicit installed-model load/unload via `keep_alive` |
| 36 | Hardware fit | User model parameter/quantization/context inputs + actual free RAM/optional GPU memory; numeric heuristic range, not a guarantee |
| 37 | Circuit breaker | Three recorded provider failures open a 60-second circuit; retry only after cooldown on a new explicit request |
| 38 | Fallback policy | Validated explicit permitted fallback map; cycles refused; local-only prevents cloud fallbacks |
| 41 | Secret review | Heuristic scan/redaction + pre-send blocking of detected secrets, including final JSON body; not guaranteed complete detection |
| 42 | Domain enforcement | Docker network disabled; separate exact-host DNS-pinned HTTPS egress; private/mixed DNS/IP literals/implicit redirects refused; no host fallback |
| 43 | Untrusted content | Prior messages/attachments/handoffs explicitly separated as untrusted data; tool/wire roles retained; rendered as text, not HTML |
| 44 | Audit exports | Redacted JSON/CSV; spreadsheet-formula cells escaped; recorded audit is not tamper-evident |
| 45 | Data lifecycle | Run + Studio history retention, explicit per-record deletion, exports; live memory provenance protected until revoked |
| 46 | Optional encryption | Whole dashboard SQLite file uses AES-GCM with OS-keystore key; locked/missing store fails closed; new secure-dashboard transcripts/notes stay out of legacy plaintext stores |
| 47 | Memory inbox | Review/edit/approve/reject; automatic dashboard learning staged by default, forced when encryption is on |
| 48 | Memory lifecycle | Source/timestamps, pending expiry and 30-day active expiry for future runs, exact-entry revocation that preserves manual modifications |
| 49 | Skill review | Inactive create/patch/delete drafts, preview, structural lint, actual linked runtime checks, stale hash protection and explicit promotion/rejection |
| 50 | Backup/migration | Credential-free schema-2 data backup; deterministic schema 0/1/2 import migration; validates everything before atomic inactive-data restore |

IDs 6, 31–32 and 39–40 were not selected and are not part of this checklist.

## Workflows

1. Name a workflow and add nodes in dependency order. Choose `agent`, `research`,
   `reviewer`, `verification`, or `approval`. Set dependencies and a condition.
2. Set each node's `turns`, `tools`, `tokens`, `seconds` budgets. A `verification`
   node selects tests/lint/types; an `approval` node asks a human question.
3. Dry-run, save an immutable version, then type **RUN REVIEWED WORKFLOW**.
4. Refresh **live node approvals** and open a child run to use ordinary approval
   controls. Clarifications/explicit approval nodes are answered in **Inbox**.
   Workbench/Studio dialogs close when an observed normal approval arrives.
5. Inspect run handoffs and verification evidence. Cancel requests stop subsequent
   work, wake pending approvals/questions and cancel current agent sessions.

Ordinary queued tasks wait while a workflow owns the executor. Managed dev apps
must be stopped first; no managed host writer can start during the workflow.
Only independent research/reviewer nodes can execute together. Their registries
and policy refuse shell/Git and automatic learning; this is a tool-level read-only
boundary, not an assertion that another external process cannot edit the project.

Conditions use recorded upstream `ok` values. Skipped/non-success dependencies
are not silently counted as successful verification. A recovered branch does not
hide an earlier failed node in the overall run status. Node results/handoffs are
bounded, and individual agent sessions retain their own normal event history.

## Enforced execution and domain allowlist

Requires a working **Docker daemon** and an **already installed trusted image**.
There is no automatic pull/install and no unsafe host-execution fallback.

- In Data, create a detached committed checkout (uncommitted files are not copied).
- In Policy JSON set `isolation` to e.g.:

```json
{
  "enabled": true,
  "image": "python:3.12-slim",
  "domains": ["api.github.com", "github.com"],
  "checkout_id": "the-real-id-returned-by-checkout-creation"
}
```

Replace the ID with the actual returned ID. Activation checks Docker/image
availability. The image must contain the tools your command/check needs. A Docker
image lacking pytest/Ruff/mypy produces a real failed check, not a fallback.

Containers run with `--network=none`, all capabilities dropped, no-new-privileges,
read-only container root, bounded tmpfs, PID/CPU/memory limits, non-root host UID,
no daemon socket/credentials/environment mounts, and only the registered worktree
mounted at `/workspace`. A trusted image and host kernel/container runtime remain
part of the security boundary. Source workspaces are not mounted implicitly.

Under enforced mode host shell/Git/GitHub execution and host dev apps are refused.
Verification also runs in the configured checkout/container, **not** the dirty
source workspace. `domain_fetch` is the deliberate HTTPS egress path: exact hosts,
public DNS answers only, connection pinned to the inspected IP, TLS hostname/SNI
verification, no environment proxies, no credentials in URLs, bounded response,
and no implicit redirects. A redirect target must be explicitly requested and
independently validated. An empty host list denies all such requests.

The allowlist covers execution/tool egress, not the separately configured model
provider transport. Use existing local-only routing to prohibit hosted models.
It is not a promise of perfect isolation against host-kernel/Docker exploits.

## Dev app previews

Start an actual trusted argv array and its real port in Runtime. Apps must bind
`0.0.0.0` and accept the preview host/origin; environment `HOST`/`PORT` are provided,
but not every framework uses them. The UI reports actual known listener addresses
and logs rather than declaring an unreachable app healthy. Preview URLs use the
current browser hostname (including Arena's port-prefixed preview hostname), not
hard-coded browser-side localhost. Use the separate-tab link if the app forbids
iframe embedding. Stop terminates the process group; server shutdown also cleans
managed processes. These are explicitly trusted **host** apps, not containers.

## Encryption

Install the optional extra:

```sh
python -m pip install 'nexus-agent[secure-state]'
```

Python 3.11+ with SQLite serialization support is required.
A working OS **Secret Service**, **macOS Keychain**, or **Windows Credential Vault**
backend must be configured on the server. Plaintext/file-key/fail backends are
refused. In Data, confirm other Nexus servers using the shared state are stopped,
then type **ENCRYPT DASHBOARD STATE**. No password or raw key is accepted through
this API. Keep access to the OS credential store; losing that key loses access to
the encrypted database. Portable backups intentionally do not include it.

Encryption covers the entire dashboard SQLite file at rest, including checkpoints,
workflow state and history. New secure-dashboard transcripts and approved notes
are stored in this encrypted DB rather than copied into the legacy plaintext CLI
history/memory files. Authentication failure or a locked key store never falls
back to plaintext. Connections are serialized across threads/processes with file
locks; writes seal to a 0600 temporary file and atomically replace the database.

It does **not** encrypt project/code files, legacy CLI files that predate activation,
explicitly promoted skills, user-run command output files, old filesystem backups,
or RAM while the app is running. Old unlinked disk blocks are not guaranteed
secure-erased. Back up/review/delete legacy files separately if they contain secrets.

## Memory and skill review

Drafts are inactive; approval is not inferred from a model's success text. Pending
memory drafts expire after 30 days; approved entries get active expiry. Cleanup
runs before new agent construction, so expiry affects **future runs**, not text
already sent to an in-flight model. Global-memory revocation removes only the exact
owned entry. Manual modifications are retained rather than overwritten. Encrypted
notes are available only to that dashboard workspace and remain encrypted at rest.

Skill create/patch/delete proposals must be previewed against the current installed
skill. Imported/bundled skills are not modified in place. Create/patch promotion
requires a passing actual repository runtime check linked to that exact proposal
hash and unchanged source fingerprint, plus explicit human confirmation. Humans
must assess whether those tests really cover the proposed procedure—passing an
unrelated test is **not** proof a skill is semantically correct. Deletion removes
only the reviewed `SKILL.md`, not unrelated scripts/references recursively.

## Verification, retention and backup boundaries

Verification executes repository code only after explicit confirmation or a
reviewed workflow invocation. In normal mode this is trusted host execution;
enforced mode requires the registered checkout and never falls back to the host.
Actual command/exit/output/JUnit data and source fingerprints are recorded. Known
runtime/cache/build outputs are excluded from the bounded source fingerprint.
Diagnostic parsing is best-effort. Command output capture is not itself a host
OS-memory sandbox; untrusted code belongs in constrained container execution.

Step token preflight uses a deliberately conservative UTF-8-byte/framing bound,
then limits output and accounts reported usage. Context-window warnings and the selected profile’s client compaction budget
use an explicitly approximate characters/4 estimate; they do not reconfigure the
model server’s native context allocation. Provider-reported counts and
providers that violate output limits cannot be independently guaranteed. Turn/tool
checks and shell/container deadlines are enforced; wall-time cancellation of
already in-flight HTTP requests is cooperative and bounded by provider timeouts.

Rollback is per supported text file, not a transaction for shell/Git side effects.
It refuses stale hashes and active runs; it cannot lock out an unrelated editor
between filesystem checks and writes. Heuristic secret redaction is not complete
DLP; inspect exported content before sharing.

Retention deletes old run and Studio history. Saved settings/templates/profiles,
workflow definitions and checkouts are intentionally protected; delete reviewed
records explicitly. Active memory provenance must be revoked before deletion.
Deletion is logical, not a claim of forensic erasure of old filesystem backups.

Schema-2 backups export templates, conversations/attachments, profiles, inactive
skill/memory data, workflow versions, and redacted history/preferences. Import
creates **new** normalized inactive records/families and read-only archived history,
restores validated UI language/layout, never resumes processes or runs, never
imports execution/model permissions or credentials, and never activates memory/skills.
Archived history is labelled imported, not presented as live/current verification.
Legacy schema 0 uses `conversations`, `templates`, `profiles`, `memory_drafts` lists;
schemas 1/2 use the `studio` collection object. Unknown schemas are refused. Every
record is validated before one atomic restore transaction.

## Validation environment

The automated suite exercises actual local subprocess/JUnit execution, managed
process lifecycle, real AES-GCM encrypted file roundtrips/tamper rejection, graph
validation/parallel scheduling, normal HTTP approvals, and browser controls.
External provider requests, Docker CLI behavior and OS key retrieval have scripted
test transports; live Docker and OS-vault integration additionally require those
services on the deployment host. The dashboard reports their actual availability
and refuses activation when unavailable; tests do not make unavailable services
appear enabled.
