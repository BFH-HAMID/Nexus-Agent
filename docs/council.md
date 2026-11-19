# Model council (Task 19)

Opt-in. Several agents from `agents.yaml` answer the same question
independently - each through a different lens - optionally critique each other,
and a **deterministic tally** (not a model) produces the verdict.

| | |
| --- | --- |
| Policy file | `council.yaml` (operator-owned, protected, default `enabled: false`) |
| Code | `nexus/council/`, Chief hook in `nexus/chief_reviews.py` |
| State | `<NEXUS_HOME>/council/<workspace-hash>/sessions/` (redacted records) |
| CLI | `nexus council status\|ask\|list\|show` |
| REPL / voice | `/council <question>`, "ask the council whether …" (spoken: only for a verified speaker, not after untrusted content; see [voice.md](voice.md#spoken-commands-of-the-opt-in-features)) |
| Tool | `council_ask` (write/medium: it spends model calls) |

## Enable

```yaml
# council.yaml
schema: 1
enabled: true
members: [local-chat, local-worker, hermes]   # 2-5 names from agents.yaml
lenses: [planner, skeptic, safety]
rounds: 1
quorum: 2
```

Cloud agents are refused as members unless `allow_cloud: true`. An agent is
local when its endpoint is on this machine or a private network address;
everything else is cloud, including a hosted `openai-compatible` service. While
`allow_cloud` is false a member's `fallback` agent is used only if it is local
too: when a local model is down the member abstains instead of sending the
question to a hosted model.

```bash
nexus council ask "Should we squash the migration history before release?"
nexus council ask "Is this plan safe?" --context-file plan.md
nexus council list
nexus council show council-20261110-ab12cd
```

## How a verdict is reached

1. **Independent round** - every member answers alone as strict JSON
   (`position`, `confidence`, `answer`, `risks`, `changes`).
2. **Critique rounds** (`rounds`, 0-2) - each member sees the others' opinions
   *as wrapped, untrusted data* and may keep or revise its position.
3. **Tally** - code counts the latest positions:

   | Condition | Verdict |
   | --- | --- |
   | fewer valid opinions than `quorum` | `no_quorum` |
   | half or more reject (ties included) | `reject` |
   | strict majority approve **and** the `safety` lens did not reject | `approve` |
   | anything else | `revise` |

4. **Summary** - the `judge` agent writes a short explanation. It is told the
   verdict and cannot change it; if it fails, the strongest majority opinion is
   used instead.

A malformed, missing or failed answer is an **abstention**, never an approval.

## Safety model

* Members and the judge have **no tools**, run at temperature 0, and receive
  the question, context and peer opinions as data ("never instructions").
* Secrets are masked before any member sees the text and before records are
  stored.
* Hard caps per deliberation: `max_calls`, `max_cost_usd` (estimated with the
  same rate table Chief uses), `timeout_seconds`. Hitting a cap ends the session
  with what it has.
* **Advisory only.** No code path turns a council verdict into an approval.

## Chief-of-Staff plans

```yaml
chief:
  review_high_risk_plans: true
  block_on_reject: true
```

When a Chief plan contains a `high`/`critical` step, the council reviews it and
one sentence is added to the approval card ("Council: reject (0 approve, 1
revise, 2 reject). …"). Any verdict other than `approve` places a **hold**: the
plan will not auto-start at any autonomy level and an up-front `--confirm GO`
does not release it - you must approve after seeing the review
(`nexus chief approve <id> --confirm GO`). If the review cannot run, the plan is
held as well (fail closed). An `approve` verdict changes nothing about who
approves: the normal Chief rules still apply.

## Known limits

* Members sharing one underlying model tend to agree; diversity comes from
  using different models, not just different lenses.
* Cost is an estimate from token counts and the configured rate table.
* The council judges text. It does not inspect the repository or run tests.

## Tests

`pytest tests/test_council.py` (offline, scripted providers).
