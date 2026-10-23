# Pipelines (Phase 6 — multi-agent collaboration)

A pipeline hands the same goal to several specialists in order. Each step receives:

1. the overall goal,
2. its own `instruction`,
3. the previous specialist's **handoff note** — their `finish` summary, the files they touched and
   their follow-ups (max 4000 chars).

```bash
nexus pipeline --list
nexus pipeline ship-feature 'add a rate limiter to the public API'
nexus pipeline bug-hunt 'the /users endpoint returns 500 on empty query' --yes
/pipeline ship-feature add a rate limiter        # inside the REPL
```

## Shipped pipelines

| Pipeline | Steps | Intent |
| --- | --- | --- |
| `ship-feature` | backend → qa → security-audit → docs → devops | build it, test it, review it, document it, open the PR |
| `security-review` | security-audit → pentesting → docs *(all read-only)* | map the surface, list verification steps, write the findings doc |
| `bug-hunt` | debugging *(read-only)* → backend → qa | reproduce, fix, regression-test |
| `content-drop` | research *(read-only)* → content-writing → seo → editor | research, write, optimize, line-edit |
| `release` | devops *(read-only)* → docs → release-engineering | decide the version, write the notes, tag and publish |

## Writing your own

```yaml
# pipelines.yaml (or .nexus/pipelines.yaml, which merges on top)
pipelines:
  mobile-release:
    description: Ship a mobile build through QA and the store checklist.
    steps:
      - role: mobile
        instruction: Bump the version, update the changelog and build the release artifact.
      - role: qa
        instruction: Run the device matrix from your QA checklist and report every failure.
        max_turns: 12          # optional per-step cap
      - role: release-engineering
        instruction: Upload the build and fill in the store metadata.
        read_only: false
      - role: docs
        instruction: Write the release notes from the QA report.
        read_only: true        # this step may not write anything
```

Per-step keys: `role` (required), `instruction`, `read_only` (default `false`), `max_turns`.

Behaviour notes:

* a step that fails (provider error, too many tool errors) **stops the pipeline** — you get the
  results collected so far and a `pipeline stopped: step N (role)` line;
* `read_only: true` removes the mutating tools from that step's registry *and* puts the gate in
  plan mode, so a step cannot "accidentally" write;
* approvals are per tool call, exactly like a single run — `--yes` applies to the whole pipeline;
* history is reset between steps (each specialist starts clean and only sees the handoff note),
  which keeps the context small and the roles honest;
* the run ends with the last step's summary, and `--json` prints that step's result.

## `ask_agent` — collaboration inside a single run

Pipelines are for *sequence*; `ask_agent` is for *consultation*. Any role can ask another one
question mid-run:

```json
{"name": "ask_agent",
 "arguments": {"role": "security-audit",
               "question": "Is this JWT refresh flow safe?",
               "context": "we store the refresh token in httpOnly cookie, 7d expiry"}}
```

Rules: one question, one answer, **no tools** in the consulted turn, no recursion
(`ask_agent_depth >= 1` disables the tool), answer clipped to the tool-output limit, and the
consulted role runs on its own agent — so `backend` (Claude) asking `pentesting` really does hit
the uncensored local model, and you see `⇄ asked 'pentesting' via ollama-uncensored` in the log.

## Patterns that work well

* **Read-only first.** A `debugging` or `security-audit` step with `read_only: true` produces
  evidence without risk, and the following step gets a much better brief.
* **End with delivery.** A final `devops`/`release-engineering` step that runs `git_prepare` +
  `gh_create_pr` means one command takes you from task to PR.
* **Cap the expensive steps.** `max_turns` per step prevents a chatty role from eating the budget.
* **Keep handoffs concrete.** The instruction "call `finish` with what the next specialist must
  check" is already injected for you — write instructions that build on it.
