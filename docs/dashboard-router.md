# Router Studio — rules, dry runs, fallback ladders, an honest accuracy panel

Prompt 4 of the dashboard build. Routing used to be invisible: `roles.yaml`
weighted keywords decided a role unless the score was too low, in which case an
optional classifier call picked one. There was no way to see why a task went to
a role, no way to add a workspace-specific phrase without editing the shipped
role pack, and no measurement of whether the choice was good.

The studio adds a reviewed rule layer, an offline dry run (zero provider calls),
a read-only ladder view for every `agents.yaml` fallback, a batch lab, and an
accuracy panel that separates what is measured from what is not.

Code: `nexus/routerstudio.py` (service and store), `nexus/router.py`
(`_apply_rules`), `nexus/sessions.py` (`routing_stats`), `nexus/server.py`
(routes), `nexus/panel/routerstudio.js` + `shell.css` (page, composer preview),
`tests/test_routerstudio.py`, `tests/browser/router.test.mjs`.

## The policy store

Everything lives under `<workspace>/.nexus/router/` — workspace runtime state,
git-ignored, never part of a role pack:

| file | contents |
| --- | --- |
| `policy.json` | `{schema, enabled, weights, rules[], updated_at}`, mode `0600` |
| `history/<UTC>-<sha8>.json` | pre-change snapshot with the reason, newest 20 kept |
| `lab.json` | the last 10 lab runs, each with its cases and policy revision |

Writes are atomic (temp file + `os.replace`), the store is read through an
mtime-cached loader, and a damaged or missing file **never** stops a run: the
router treats it as "no rules". The revision is `sha256(enabled, weights,
rules)[:16]`; every write must quote the current one, so a stale tab cannot
overwrite a newer decision. Demo mode refuses every write.

The master switch is **off** by default. With it off, saved rules are stored and
editable but do not affect routing.

## Rules

A rule is `{id, phrase, mode, role, weight, priority, enabled, note}`:

* `phrase` 1–160 characters, `mode` `contains` (substring, case-folded) or
  `words` (every whitespace-separated token must appear as a whole word);
* `role` must exist in the current roster (aliases are resolved once, at save);
* `weight` 0.5–10 is added to that role's keyword score when the phrase matches;
* `priority` 1–9 breaks an **exact** score tie only — it never adds score;
* `id` is `r-` + `sha256(role, phrase, mode)[:8]`; the same role+phrase is an
  edit, a different mode is a refusal, and a duplicate phrase for the same role
  is rejected with the existing id;
* at most 60 rules.

`Router.keyword_route()` calls `routerstudio.active_rules(workspace)` inside a
`try/except` and adds matching weights to the scored roles, tagging the hits
`rule:<id>` so the decision can be explained. A rule that names a role which has
been removed from the roster is skipped. The router's own threshold, tie-break
and classifier behaviour are unchanged.

## Endpoints

Reads (GET, rate-limited per client, audited once per five minutes):

| endpoint | returns |
| --- | --- |
| `/api/router/overview` | policy summary, live settings, roster counts, chain summary, last lab |
| `/api/router/rules` | full policy, role-name vocabulary, limits, store error |
| `/api/router/chains` | every agent's ladder, nodes and issues, summary |
| `/api/router/models` | cost/speed/quality table with coverage and nulls |
| `/api/router/accuracy` | measured counts, per-role rows, last lab, checked / not-checked |
| `/api/router/history` | recorded policy versions, newest first |
| `/api/router/lab` | the stored lab runs |

Writes (POST, `router-write` bucket, typed confirmation, revision required):

| endpoint | body | consent |
| --- | --- | --- |
| `/api/router/rule` | `action: prepare` | none — validates and returns `SAVE RULE <id>` without writing |
| `/api/router/rule` | `action: save\|remove\|enable\|disable` | `SAVE RULE <id>`, `REMOVE RULE <id>`, `ENABLE RULE <id>`, `DISABLE RULE <id>` |
| `/api/router/weights` | `{weights, revision, confirmation}` | `SAVE WEIGHTS` |
| `/api/router/enable` | `{enabled, revision, confirmation}` | `ENABLE RULES` / `DISABLE RULES` |
| `/api/router/lab` | `{cases: [{task, expected?}]}` | none needed — writes only the lab history |
| `/api/router/restore` | `{version, revision, confirmation}` | `RESTORE ROUTER <version>` |
| `/api/router/simulate` | `{task, draft?}` | none needed — read-only, zero provider calls |

Anything else under `/api/router/` is a 404, an over-budget client gets 429
`rate_limited`, bad input 400 `invalid_request`, and an unavailable store 503
`store_unavailable` — all as `{ok:false, error:{code,message}}`. Responses are
redacted against the workspace secret values; an API key is only ever reported
as `key_present: true|false`.

## Dry run and lab

`simulate()` scores every role with its shipped keyword table plus the enabled
rules, then reports either a `decision` (role, method, score, hits) or a
`deferred` stage (classifier or default) with the reason — no match, below
`router_min_score`, or an exact tie. It returns candidates with `base`, `bonus`,
`priority`, `hits`, the threshold it used, the elapsed milliseconds, and
`provider_calls: 0`. No provider is constructed, so no model can be called and
no task is created. A `draft` rule can be included to preview it before saving.

The lab takes up to 25 lines of `task => expected role` (500 characters per
task), runs `simulate()` for each, and reports per-case accuracy over the cases
that named an expected role, plus which stage would have handled the rest. Each
run records its policy revision and the last 10 runs are kept. Accuracy here
measures the **keyword and rule stage only** — the UI says so.

## Weights and the model table

`/api/router/models` joins three real sources:

* **cost** — operator-entered prices from the Workbench settings
  (`input_per_million + output_per_million`) for the agent's model;
* **speed** — average whole-session duration from `sessions.db` for that agent;
* **quality** — that agent's reported-ok rate from `sessions.db`.

Each dimension is normalised 0–1 across the agents that have it, and a row's
score is the weighted mean over the dimensions it actually has. Rows expose
`coverage` (0–3) and `null` where a measurement is missing; a recommendation is
only offered at coverage ≥ 2. The weights order this table — they do not change
a live request, and the page says so.

## Fallback ladders

`chains()` walks each agent's `fallback` pointer read-only, up to eight hops, and
flags `unknown` targets, `loop`s (an agent already in this ladder), `long`
chains and `missing_key` agents (no resolved credential and not a local
endpoint). Each node carries provider, model, `local` and `key_present`. Editing
a ladder still happens in the Workbench — nothing here writes `agents.yaml`.

## Accuracy

`routing_stats()` (90-day window) groups `sessions.db` by role and agent, and
reads explicit `routing_feedback` rows. The panel shows:

* **measured** — recorded runs, reported-ok counts, known durations, explicit
  feedback observations and approval rate, per-role rows;
* **not checked** — classifier-stage decisions (not recorded per run), whether a
  successful run used the *best* role (no grader), model latency (only overall
  session duration exists), and provider invoices (prices are operator-entered).

Every list has an explicit empty state; nothing is shown as `0` when it is
unknown, and every value carries the time it was read.

## Composer override

The Run composer keeps its `#roleSelect`: choosing a role forces it (the task
body carries `role`, and the preview says the route is manual). A new
`#routePreviewBtn` asks `/api/router/simulate` for the offline route of the
typed task and paints the role, score and any matched rule ids next to it.
Clicking that chip opens the Lab with the task pre-filled. Neither control
starts a run or a model call.

## UI

Five tabs (`policy`, `rules`, `chains`, `lab`, `accuracy`) as an ARIA tablist
with Arrow/Home/End support. Desktop at ≥1080 px and a single column at 360 px;
all labels exist in English and Bangla (172 `router.*` keys per language), and
the page re-paints on a language switch without refetching. Long values are
wrapped, tables scroll inside their card, and the page never widens the
document (`scrollWidth − innerWidth = 0`).

## Manual test

```bash
.venv/bin/python -m nexus gui --port 8787 --no-open --host 0.0.0.0
# open http://127.0.0.1:8787/dashboard, log in with the serve token
```

1. Sidebar → **Router Studio**. The policy tab shows the live `auto_route`,
   threshold, classifier agent and default agent from `agents.yaml`, plus the
   role/agent counts and the read time.
2. Rules tab → type `kubernetes rollout` and role `devops`, press **Prepare**.
   Nothing is written until the exact `SAVE RULE r-…` token is typed.
3. Dry-run the same phrase in the preview box: `provider_calls: 0`, the rule id
   in the candidate row, and a decision or a deferred reason with the threshold.
4. Enable rules (typed `ENABLE RULES`), then check the Run composer: the preview
   button names the role for the task; selecting a role says it is forced.
5. Chains tab: every agent's ladder with its real provider/model/key presence;
   an agent with no credential is flagged, not hidden.
6. Lab tab: paste `add a python api endpoint => backend` and run it. Accuracy is
   1/1 for the keyword stage, no task appears in Operations.
7. Accuracy tab: measured runs and feedback, and the four "not checked" items.
8. Switch to Bangla; the page and the composer chip translate. Shrink to 360 px.

## Tests

* `tests/test_routerstudio.py` — 36 tests: store/revision/atomic write, rule
  validation and duplicates, `prepare` never writing, typed confirmations,
  weights, enable/disable, restore and history ordering, simulate accuracy against
  the real role pack, chains issues, lab persistence and caps, redaction, demo
  refusal, HTTP auth/rate-limit/404/503.
* `tests/browser/router.test.mjs` — 8 tests: live policy stamp, rule lifecycle
  through the UI, draft dry run, ladders, lab, accuracy blind spots, composer
  preview without a run, Bangla at 360 px.

## Known limits

* Rules are additive only: there are no negative rules, no regex, and no
  per-role priority above an exact tie.
* The lab measures the keyword/rule stage; the classifier is never invoked, so
  its accuracy is reported as not checked rather than estimated.
* Speed is whole-session duration, not model latency; quality is the
  agent-reported outcome, not an independent grade; cost uses operator prices,
  not provider invoices.
* Policy history keeps 20 versions; the lab keeps 10 runs.
* The store is per workspace and not synced between machines.
