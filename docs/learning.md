# Learning: skills, memory, recall — the Hermes loop, natively in Nexus

Nexus is not only a router that runs a task and forgets it. It keeps what it
learns in three places, and loads it again on the next run:

| Layer | Where it lives | What goes in it |
| --- | --- | --- |
| **Skills** (procedural) | `~/.config/nexus/skills/` + `<project>/.nexus/skills/` + the bundled `skills/` | *how* to do something: `SKILL.md` with steps, commands, pitfalls, verification |
| **Memory** (declarative) | `~/.config/nexus/memories/MEMORY.md` + `USER.md` | facts, decisions, gotchas, and stable preferences of the human |
| **Sessions** (episodic) | `~/.config/nexus/sessions.db` (SQLite + FTS5) | every run's task, summary and transcript — searchable |

The design follows [Nous Research's `hermes-agent`](https://github.com/NousResearch/hermes-agent):
skills are portable `SKILL.md` files (agentskills.io format), memory is a small
curated pair of markdown files with a hard character cap, and the agent improves
its own library instead of waiting for you to write documentation.

---

## 1. The loop, in one picture

```
       task ──► router ──► role ──► model ──► tools ──► answer
                                     ▲                   │
                                     │                   ▼
                       skills index + memory      learning review (cheap model)
                       injected into the prompt           │
                                     ▲                    ├─ create / patch a skill
                                     └────────────────────┤
                                                          ├─ MEMORY.md / USER.md entries
                                                          └─ session stored for /recall
```

* **Before** a run: matching skills (names + ≤60-char descriptions) and your
  `MEMORY.md` / `USER.md` enter the system prompt. The full skill body is only
  pulled in when it matters — with the `skill_view` tool, or when you pin it.
* **After** a run that used at least `learning_min_tools` tool calls, a cheap
  model reads the task, the tool trail, the changed files and the final answer,
  and may write **one** skill, **one** patch and up to three memory entries.
  It never sees your keys and it never edits code.
* Nothing is ever deleted: the curator *archives* stale skills.

Turn learning off with `NEXUS_LEARNING=0`, or tune `learning`, `learning_agent`,
`learning_min_tools`, `skill_nudge_interval`, `skills_enabled` in the
`settings:` block of `agents.yaml`.

---

## 2. Skills

```bash
nexus skills                       # what the agent has learned
nexus skills show paginate-endpoint
nexus skills import                # clone hermes-agent and import its 200+ skills
nexus skills curate [--apply]      # what is stale; --apply archives it
nexus skills delete <name>
nexus skills path                  # the roots that are searched
```

Inside the REPL:

```
/skills
/skill paginate-endpoint            # print the procedure
/skill paginate-endpoint add cursor pagination to /users   # run a task with it pinned
/paginate-endpoint add cursor pagination to /users         # same, shorter
/learn add a --json flag to every command                  # capture what we just did
/curate
```

`/learn` also accepts a path or URL: `/learn docs/architecture.md` and
`/learn https://example.com/runbook` turn documentation into a skill.

### The file format

```markdown
---
name: paginate-endpoint
description: Add cursor pagination to a list endpoint.
version: 1.0.0
author: Nexus Agent
license: MIT
metadata:
  hermes:
    tags: [api, pagination]
    category: learned
---

# Paginate Endpoint Skill

## When to Use
A list endpoint returns the whole table.

## Procedure
1. add `limit` and `cursor` parameters
2. clamp `limit` to 1..100
3. return `next_cursor`

## Pitfalls
- Offsets drift when rows are inserted mid-pagination.

## Verification
1. `curl 'localhost:8000/users?limit=2'` twice and check the cursor advances.
```

Rules the linter enforces (`skilllib.lint`): lowercase-dashed name ≤64 chars,
**one-sentence description ≤60 chars ending with a period**, a markdown H1,
`## When to Use` / `## Procedure` / `## Verification` sections.

Roots are searched lowest-precedence first, later wins on a name clash:

1. `skills/` shipped with Nexus (4 starter skills: add a role, add an agent,
   verify before commit, release & self-update)
2. `~/.config/nexus/skills/` — what the agent learns, plus imports
3. `<project>/.nexus/skills/` — project-local skills, committed with the repo

Imported skills are **read-only**: the learning loop will not patch a skill that
came from upstream.

---

## 3. Memory

```bash
nexus memory                                  # MEMORY.md + USER.md
nexus memory add "this repo deploys with fly.io"
nexus memory add "answers in Bangla first" --user
nexus memory remove fly.io
nexus memory import --source ~/.hermes/memories
```

The agent writes to the same files through the `memory` tool
(`section=memory` for facts, `section=user` for preferences). Caps: 2200 chars
for `MEMORY.md`, 1375 for `USER.md` — when a file is full the tool refuses and
tells the model to consolidate instead of appending. Files use Hermes'
`§` delimiter, so a Hermes memory directory imports directly.

Role memory (`.nexus/memory/<role>.md`, the `remember` tool) still exists and is
scoped to one repository; `MEMORY.md` / `USER.md` follow you everywhere.

---

## 4. Recall — your own history

Every run is recorded (task, summary, transcript, turns, tool calls, duration)
in `~/.config/nexus/sessions.db`. SQLite FTS5 makes it searchable.

```bash
nexus recall                       # the last runs
nexus recall flaky test            # full-text search
nexus recall --json
```

In the REPL: `/recall flaky test`. In the GUI: the **LEARNING BRAIN** card.
In a run: the `recall` tool — the agent can search its own past sessions before
asking you something it already knows.

---

## 5. The curator

Skills age. `nexus skills curate` reports skills that were never used or unused
for `--stale-days` (default 45), and `--apply` moves the stale *local* ones to
`~/.config/nexus/skills/.archive/`. Bundled, project and imported skills are
never touched. Deletion is always a manual, explicit act.

`/curate` does the same inside the REPL.

---

## 6. The Hermes bridge

```bash
nexus hermes                 # status: upstream checkout, imported skills, CLI
nexus hermes install         # shallow-clone hermes-agent and import every SKILL.md
nexus hermes sync            # re-import skills + MEMORY.md / USER.md
nexus hermes run <args>      # hand a task to an installed `hermes` binary
```

`nexus hermes run` is the escape hatch for what Nexus does not re-implement —
Hermes' gateway (Telegram/Discord/…), TUI and terminal backends. Everything else
is native: skills, memory, nudges, `/learn`, curator, recall.

Set `NEXUS_HERMES_CHECKOUT` to use an existing clone instead of
`~/.config/nexus/hermes-agent`. Nothing is downloaded unless you ask.

---

## 7. Self-update

```bash
nexus update            # git pull --ff-only + pip install -e .
nexus update --no-pull  # just reinstall
```

`nexus update` refuses to run on a dirty tree (the pull would fail) and prints
the reason instead of guessing.

---

## 8. Prompts and nudges

* The index in the prompt is deliberately tiny: name, category, ≤60-char
  description, at most 12 matches (40 when nothing matches). Bodies load on demand,
  which keeps prompt caching effective — the same invariant Hermes documents.
* Every `skill_nudge_interval` turns (default 8) the system prompt gets one extra
  paragraph reminding the agent to save reusable procedures and durable facts.
* The review model is `settings.learning_agent`, falling back to
  `settings.router_agent` — the cheapest model you have configured.

---

## 9. Testing

```bash
python -m pytest -q tests/test_skills.py tests/test_brain.py tests/test_learning.py \
  tests/test_agent_learning.py tests/test_cli_learning.py
```

All of it runs offline: `tests/conftest.py` points `NEXUS_HOME` at a temp
directory and sets `NEXUS_LEARNING=0`, so the suite never touches your real
skills, memory or session history.
