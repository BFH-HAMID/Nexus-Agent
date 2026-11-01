# Approval Center — one inbox for every decision, answered through the real gates

Prompt 6 of the dashboard build. The workspace already asked for consent in three
real places:

* a live task pauses inside the agent's approval gate (`POST /api/approve/<task>`);
* a background Jarvis job waits for a one-use action approval
  (`nexus/jarvis/approvals.py`);
* a queued task waits for admission (`nexus/jarvis/queue.py`).

Each had its own screen and its own phrasing, none of them presented the three
together, and the dashboard badge only counted background actions. The Approval
Center is a **projection and a decision surface** over those stores — it starts
nothing, grants nothing by itself and never invents a pending item.

Code: `nexus/approvalcenter.py` (service), `nexus/server.py` (`/api/approvals/*`,
`ServerState.decide_approval`), `nexus/permissions.py` (`FORCED_REVIEW`),
`nexus/panel/approvals.js` + `shell.css`, `tests/test_approvalcenter.py`,
`tests/browser/approvals.test.mjs`.

## Rules that shape the page

1. **No second control plane.** A decision calls the same gate, the same
   one-use approval store and the same queue review that raised it. The legacy
   `POST /api/approve/<task>` and this page now share one implementation
   (`ServerState.decide_approval`), so there is exactly one set of checks.
2. **Nothing is invented.** An item is listed only while its own record still
   exists, is inside its own deadline, and still matches its owning job/run
   (status, lease, approval id). Expired, consumed and withdrawn requests are
   reported as stale, never revived.
3. **Deny is free, grant is typed.** Refusing needs one click. A grant needs the
   exact token the request itself published. A latched emergency stop refuses
   every grant while leaving denial available.

## The three sources

| source | store | real token | decisions |
| --- | --- | --- | --- |
| `live` | in-process run, `session.pending` | `APPROVE ACTION <approval_id>` | approve, deny |
| `action` | Studio `jarvis-approval` row | the row's own `confirmation` | allow, deny |
| `queue` | Studio `jarvis-job`, status `awaiting_approval` | `APPROVE QUEUED TASK <id>` | approve, reject, cancel |

A live request now also records `requested` and `timeout`, so the page can show
a real countdown instead of guessing one. Sessions that have been cancelled,
approvals that were consumed, expired or whose job stopped running, and queued
tasks past their TTL are all excluded from the inbox.

Approving a **queued task admits the task** — it never approves that task's
tools, GitHub mutations or external actions; each of those asks again.

## Endpoints

Reads (GET, `approvals:` bucket, sampled audit once per five minutes, redacted):

| endpoint | returns |
| --- | --- |
| `/api/approvals/inbox` | items (100 max, oldest deadline first), per-source counts, limits, source errors |
| `/api/approvals/policy` | `permissions.yaml` mode and per-tool rows, autonomy level/runtime/queue limits, latch, queue pause, DND, forced-review families, and what is **not** checked |
| `/api/approvals/history?limit=` | recorded decisions from the API audit plus the approval store, newest first |

Writes (POST, `approvals-write:` bucket, audited on every decision):

| endpoint | body | consent |
| --- | --- | --- |
| `/api/approvals/decide` | `{source, id, decision, approval_id?, confirmation?, scope?, reason?}` | grant needs the published token; deny does not |

Errors: 409 `stale_request` when the request is no longer answerable, 400
`invalid_request` for anything malformed, 429 `rate_limited` over budget, 401
without the GUI session, 404 for unknown routes and GET on the POST route. All
as `{ok:false, error:{code,message}}`.

Demo mode refuses every decision (`Live mode is required`), because a demo run
has no real consent to record.

## The page

Four tabs (`inbox`, `policy`, `history`, `diagnostics`) as an ARIA tablist with
Arrow/Home/End support:

* **Inbox** — three counters (live task / background action / queued task), a
  source filter, then one card per request: title, source, risk, time left,
  the exact arguments, and the preview the gate produced. Grant opens an inline
  form that shows the token, enables its button only when the typed text matches
  exactly, and offers the real scopes (`once` / `session`) for a live run.
  A failure from the server is shown on the card, and a stale request disappears
  on the next paint instead of pretending it succeeded.
* **Policy** — the default tool mode, per-tool overrides, adapter state, shell
  backend, autonomy level and runtime, queue limits and TTL, latch/queue/DND
  state, the families that are **always** reviewed whatever the mode says, and
  a list of things this page deliberately does not verify. Those server-written
  explanations carry a stable id and are translated on the page, so Bangla mode
  does not fall back to English.
* **History** — only decisions that were actually written down: the API audit
  trail plus approval-store rows, each naming its source, record id and where it
  was recorded. History re-reads when the tab opens; an empty state says so.
* **Diagnostics** — the read timestamp, item/text budgets, per-source errors
  ("every source answered" when clean), where history comes from, and the
  inbox's three guarantees.

The sidebar **Approvals** row and the mobile tabbar both open this page; the
legacy Operations page (start/pause/resume/stop/kill, autonomy, DND, new task,
tasks list, diagnostics) is unchanged and still opens from `#operations`, the
All controls card, and the **Operations & controls** button on this page. The
sidebar/bell badge now counts the whole inbox, falling back to the old
operations count if the inbox cannot be read.

Desktop is single-column cards at ≥1080 px; 360 px keeps every control reachable
and the document never widens (`scrollWidth − innerWidth = 0`). All labels exist
in English and Bangla (105 `approvals.*` keys per language) and the page re-paints
on a language switch without refetching.

## Manual test

```bash
.venv/bin/python -m nexus gui --port 8787 --no-open --host 0.0.0.0
# open http://127.0.0.1:8787/dashboard, log in with the serve token
```

1. Sidebar → **Approvals**. The inbox counts what is really waiting; with
   nothing pending it says so and the badge is empty.
2. Operations & controls → create a task with **Create task**. The Inbox card
   appears within one poll (or immediately on **Refresh**), naming the task,
   `priority 5`, the time left and the exact token.
3. Press **Approve**: the button stays disabled until
   `APPROVE QUEUED TASK <id>` is typed exactly. Type one wrong character and it
   stays disabled. **Keep waiting** closes the form without deciding.
4. Press **Reject** instead: one click, no typing. The card goes, the job shows
   `rejected` in Operations, and the History tab gains the decision.
5. Answer a request somewhere else first (e.g. Operations), then press its
   button in this page: the card reports the refusal and disappears on refresh
   instead of claiming success.
6. Policy tab: the default mode, adapter state, autonomy level and the
   always-reviewed families; Diagnostics tab: the read time and "Every source
   answered".
7. Switch to Bangla, shrink to 360 px, and move between tabs with the arrow keys.

## Tests

* `tests/test_approvalcenter.py` — 25 tests: empty inbox honesty, live/action/
  queue projection with real tokens and deadlines, cancelled and expired
  exclusion, typed grant tokens, free denial, stale refusals, the one-use store,
  queue admission, latched emergency stop, policy facts (including an unreadable
  `permissions.yaml`), history from the audit, secret redaction, input
  validation, demo refusal, and the HTTP surface (401, 404, 409, 429, POST-only,
  audit).
* `tests/browser/approvals.test.mjs` — 7 tests: paint equals the API item for
  item, a real queued task created through Operations (wrong token keeps the
  grant disabled, reject is one click, the job really changes), approve lands in
  history, an answer made elsewhere is reported stale, the policy tab reads the
  real gate and names blind spots, keyboard tabs plus the legacy Operations page
  still opening, and Bangla at 360 px.

## Known limits

* Live-run approvals exist only while this process owns the run; the inbox can
  show them but a restart during an approval ends that request.
* History is built from the audit trail and the approval store — it is not a
  separate ledger, and it keeps what those two keep.
* A session-scoped grant covers that run only; it is not saved as policy.
* The page cannot prove an action was safe; it shows what was asked, what the
  gate intends to do, and who answered.
* Queue approval does not approve the queued task's own tools.
