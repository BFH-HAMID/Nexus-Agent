# Nexus Agent

*This is my personal work station.*

**One CLI / VS Code agent, a whole team of specialists behind it.**

You describe a task. Nexus routes it to the right *employee* — `frontend`, `backend`,
`pentesting`, `docs`, `trading`, `osint`, 107 curated of them (959 with the imported
packs in `roles.d/`) — and that role runs on the model that
fits it best: Claude for real code edits, Hermes for cheap bulk work, an uncensored local
Ollama model for authorized offensive-security research. Then it uses tools (files, shell,
git, GitHub), asks you before anything destructive, and can open the PR for you.

```
                         ┌─────────────────────────┐
                         │        User (you)        │
                         │  VS Code / Terminal CLI   │
                         └────────────┬──────────────┘
                                      │ task text
                                      ▼
                         ┌─────────────────────────┐
                         │         ROUTER           │
                         │ 1) keyword fast-match     │
                         │ 2) LLM classify fallback   │
                         └────────────┬──────────────┘
                                      │ role name
                                      ▼
                         ┌─────────────────────────┐
                         │      ROLE REGISTRY       │
                         │ frontend / backend / ... │
                         │ role → persona + agent    │
                         └────────────┬──────────────┘
                                      │ picks provider
              ┌───────────────┬───────┴────────┬────────────────┐
              ▼               ▼                ▼                ▼
         ┌─────────┐    ┌───────────┐    ┌───────────┐   ┌───────────────────┐
         │ Claude  │    │ Hermes 3  │    │  Ollama   │   │ Ollama-uncensored │
         │  (API)  │    │  (local)  │   │  (local)  │   │  dolphin-mixtral  │
         └────┬────┘    └─────┬─────┘    └─────┬─────┘   └─────────┬─────────┘
              └───────────────┴────────────────┴───────────────────┘
                                      │
                                      ▼
                         ┌─────────────────────────┐
                         │        TOOL LAYER        │
                         │ files · shell · git ·    │
                         │ GitHub · memory · peers  │
                         └─────────────────────────┘
```

The router is a **classifier**, not a model picker: it chooses a *role*, and the role is mapped
to an *agent* in `agents.yaml`. That indirection is the whole idea — pentesting/OSINT need an
uncensored local model, frontend/backend need Claude's reasoning and tool calling, and bulk work
(docs, QA, marketing, trading scripts) can go to a free local model to keep the cost down.

---

## Try it in 60 seconds (no API key needed)

```bash
git clone https://github.com/BFH-HAMID/Nexus-Agent && cd Nexus-Agent
python -m venv .venv && . .venv/bin/activate
pip install -e .

nexus doctor                                   # what's reachable, what's missing
nexus roles                                    # meet the team (959 roles)
nexus roles --show pentesting                  # persona + keywords + agent
nexus addagent --list                          # the imported packs in roles.d/
nexus task 'add pagination to the users api' --demo --yes
```

`--demo` runs a **scripted, offline** version of the full loop — same routing, same tools, same
approval gate, same event stream, zero tokens. It writes `demo/nexus-demo-notes.md` so you can
see the diff and the PR-shaped output. Perfect for the VS Code sidebar on a fresh checkout.

Then make it real:

```bash
cp .env.example .env          # put ANTHROPIC_API_KEY in it
nexus doctor                  # ✓ agent:claude reachable
nexus chat                    # REPL with /commands
nexus task 'add cursor pagination to /api/users and cover it with tests'
```

Local models (optional, for the cheap and uncensored lanes):

```bash
ollama pull hermes3:8b        # tool-capable local worker  -> agent: hermes
ollama pull dolphin-mixtral   # uncensored, NO tool template -> agent: ollama-uncensored (tool_mode: text)
ollama pull llama3.1:8b       # generic local fallback     -> agent: ollama
```

---

## Commands

| Command | What it does |
| --- | --- |
| `nexus` / `nexus chat` | interactive REPL with `/commands` |
| `nexus task "<task>"` | one-shot autonomous run (route → tools → summary) |
| `nexus ask "<question>"` | read-only run — plan mode, nothing is written |
| `nexus task "<task>" --plan` | same as `ask`: produces a plan, refuses mutations |
| `nexus task "<task>" --role backend` | skip routing, force a specialist |
| `nexus task "<task>" --yes` | auto-approve non-dangerous tool calls |
| `nexus task "<task>" --demo` | scripted offline run (no keys, no models) |
| `nexus pipeline ship-feature "<task>"` | multi-role handoff run |
| `nexus pipeline --list` | show available pipelines |
| `nexus roles [--search x \| --show x \| --category x \| --json]` | the team |
| `nexus addagent <github-url>` | import a repo's agents/skills as new roles |
| `nexus addagent --list` | show installed import packs + role counts |
| `nexus agents` | configured model endpoints and which roles use them |
| `nexus prompt <role>` | print the exact system prompt a role gets |
| `nexus doctor [--json]` | keys, endpoints, models, `gh`, repo state |
| `nexus serve [--demo] [--yes]` | local HTTP+SSE server for the sidebar/panel |
| `nexus panel` | `serve` + open the built-in browser panel |
| `nexus init` | drop `.env`, `.nexus/roles.yaml`, `.vscode/` into any project |
| `nexus task ... --json` | machine-readable result (CI friendly) |

### REPL commands

```
/help  /role <name>  /auto [on|off]  /agent <name>  /model <id>
/ask <q>  /plan <task>  /task <task>  /pipeline <name> <task>
/roles [needle]  /addagent <github-url>  /agents  /tools  /system [role]
/memory [role]  /remember <text>
/doctor  /stats  /compact  /clear  /new  /save [file]  /exit
```

Everything that does not start with `/` is a chat turn.

---

## The team (Phase 3 — 107 curated roles, 959 with imports)

The curated core is 107 hand-written specialists:

| Category | Count | Examples | Default agent |
| --- | --- | --- | --- |
| development | 39 | frontend, backend, database, devops, mobile, blockchain, embedded, debugging, refactoring, architecture | `claude` (bulk: `hermes`) |
| security | 14 | pentesting, osint, security-audit, malware-analysis, reverse-engineering, forensics, cryptography, devsecops | `claude` / `ollama-uncensored` |
| business | 17 | marketing, seo, copywriting, sales, finance, legal-draft, hr, product-management | `hermes` |
| creative | 12 | design, branding, content-writing, video-script, translation, presentation | `hermes` |
| data-ai | 10 | data-science, ai-engineering, data-engineering, ml-ops, nlp, computer-vision | `claude` |
| qa | 5 | qa, test-automation, performance-testing, chaos-engineering, release-engineering | `hermes` |
| docs | 4 | docs, developer-experience, tutorial-writing, ux-writing | `hermes` |
| trading | 4 | trading, quant-research, defi-analyst, risk-management | `hermes` |
| general | 2 | general (fallback), assistant | *default agent* |

On top of that, `roles.d/` ships **852 agents imported from five public repos**
(see [`/addagent`](#grow-the-team-addagent)), for **959 specialists** total:

| Pack | Roles | From |
| --- | --- | --- |
| `agency-agents` | 283 | [msitarzewski/agency-agents](https://github.com/msitarzewski/agency-agents) — a full AI agency |
| `ecc` | 426 | [affaan-m/ecc](https://github.com/affaan-m/ecc) — agent-harness skills + subagents |
| `awesome-llm-apps` | 123 | [shubhamsaboo/awesome-llm-apps](https://github.com/shubhamsaboo/awesome-llm-apps) — reference agent/RAG apps |
| `superpowers` | 15 | [obra/superpowers](https://github.com/obra/superpowers) — dev-methodology skills |
| `rea` | 5 | [morluto/rea](https://github.com/morluto/rea) — reverse-engineering skills |

`nexus roles` prints the live table; `nexus roles -s <needle>` searches all 959.
Full list of the curated core with personas: [`docs/roles.md`](docs/roles.md).

### Grow the team: `/addagent`

Adding a specialist is a YAML block — and importing a whole repo of them is one
command. Point `/addagent` (or `nexus addagent`) at any GitHub repo of
agent/skill definitions and it fetches, discovers, converts and hot-loads them:

```bash
nexus addagent https://github.com/obra/superpowers          # -> .nexus/roles.d/superpowers.yaml
nexus addagent https://github.com/you/repo --dest repo       # commit it into roles.d/
nexus addagent https://github.com/affaan-m/ecc/tree/main/skills   # just a subdir
```

It recognises `SKILL.md` files, frontmatter personas (`<category>/<agent>.md`)
and example-app dirs; auto-derives keywords so routing needs no LLM; maps
security roles to the uncensored local model; de-duplicates mirrors; and never
clobbers a curated role (collisions are suffixed). In the REPL, `/addagent <url>`
imports **and reloads**, so the next task can route to the new roles instantly.
Details: [`docs/addagent.md`](docs/addagent.md).

### Model mapping (why)

| Role category | Agent | Why |
| --- | --- | --- |
| Frontend, Backend, Mobile, Database, Security review, Data science | `claude` | best reasoning + reliable native tool calling for real code edits |
| DevOps, Marketing, Trading, QA, Docs | `hermes` | good enough, free, fast, runs locally |
| Pentesting, OSINT, malware/RE analysis | `ollama-uncensored` (`dolphin-mixtral`) | mainstream models over-refuse legitimate authorized security research |
| Long-horizon architecture, legacy modernization | `claude-big` (`claude-opus-5-5`) | deep multi-file reasoning |
| Routing classifier | `claude-cheap` (`claude-haiku-5-5`) | high-volume, low-stakes, cents |
| General / unclassified | `claude` | safe default |

### Adding a role = adding a YAML block (no code)

```yaml
# roles.yaml (or .nexus/roles.yaml for a project-local specialist)
roles:
  rust-wasm:
    title: Rust + WASM Engineer
    category: development
    agent: claude                 # any key from agents.yaml
    persona: >-
      You are a Rust + WebAssembly engineer. Focus on wasm-bindgen boundaries,
      bundle size, and keeping unsafe code out of the crate root.
    keywords:
      - wasm
      - wasm-bindgen
      - text: rust frontend      # weighted keyword
        weight: 2
    aliases: [rust-wasm-engineer]
    tools: [read_file, edit_file, write_file, run_command, git, finish]   # optional whitelist
```

`roles.yaml` is generated: edit [`scripts/build_roles.py`](scripts/build_roles.py) and re-run
`python scripts/build_roles.py` (CI fails if the two drift apart). Project-local
`.nexus/roles.yaml` merges **on top** of the bundled file, so a repo can add its own specialists.
Imported packs live in `roles.d/*.yaml` (committed) or `.nexus/roles.d/` (project-local) and merge
the same way — see [`/addagent`](#grow-the-team-addagent).

---

## Router logic (Phase 2)

**Step 1 — keyword fast-match (instant, no LLM).** Each role has weighted keywords; the router
scores every role against the task text with word-boundary matching (`api` does not fire on
`rapid`, but `next.js` and `c++` still work as substrings) and takes the highest score above
`settings.router_min_score`. Ties are not guessed — they are escalated to step 2.

```
$ nexus task 'run nmap against my lab box and try privilege escalation'
⇄ role: pentesting -> agent: ollama-uncensored [keyword] hits: nmap, privilege escalation, pentest
→ agent: ollama-uncensored  model: dolphin-mixtral:latest  provider: openai-compatible  tools: text
```

**Step 2 — LLM classifier fallback (only if nothing matched).** The roadmap prompt, verbatim:

```
You are a routing classifier.
Pick exactly ONE role name from this list that best fits the task.
Reply with ONLY the role name, nothing else.

Roles: frontend, backend, database, devops, ...
Task: <user's task text>
```

The candidate list is **narrowed to the ≤40 most relevant roles** (by keyword/name/title overlap)
before the call, so even a 959-role roster costs a single cheap classifier call rather than one
per 40-role chunk; a tie among ≤8 roles is classified among exactly those. Output is
sanitized (lowercase, `a-z0-9-` only, code fences and prose stripped) and validated against the
full registry; anything unknown falls back to `general`. The classifier runs on `claude-cheap`
(configurable via `settings.router_agent`) at `temperature: 0`, `max_tokens: 32`.

---

## Tools & the approval gate

`list_files` · `read_file` · `grep` · `write_file` · `edit_file` · `delete_file` ·
`run_command` · `git` · `git_prepare` · `gh_create_pr` · `gh` · `gh_merge_pr` ·
`remember` · `ask_agent` · `finish`

| Risk | Examples | Behaviour |
| --- | --- | --- |
| `read` | `read_file`, `grep`, `list_files`, `git status/diff/log` | never prompts |
| `write` | `write_file`, `edit_file`, `run_command`, `git commit`, `gh pr create` | prompts unless `--yes` |
| `danger` | `delete_file`, `gh pr merge/close` | always prompts; `--yes` will **not** skip it |

Three independent layers keep it safe:

1. **Path guard** — every path resolves inside the workspace; escapes are refused unless you
   pass `--allow-outside`.
2. **Deny list** — `rm -rf /`, `sudo rm`, `mkfs`, `dd of=/dev/…`, fork bombs, `git push --force`,
   `curl … | sh`, `npm publish`, `drop database`, … are refused outright. With
   `--allow-dangerous` they become "ask, loudly" instead of "refuse". Your own extra patterns go
   in `settings.blocked_command_patterns`.
3. **Approval modes** — `ask` (default), `auto` (`--yes`), `plan` (`--plan` / `nexus ask`:
   mutating tools are removed from the registry *and* the gate refuses them).

Details, schemas and examples: [`docs/tools.md`](docs/tools.md).

---

## GitHub workflow (Phase 4)

`git_prepare` does the whole delivery in one call:

* branch: `feat/<role>-<short-task>` (`slugify`d, collision-suffixed, prefix configurable)
* staging: given paths or `-A`
* commit message: generated conventional commit — `feat(backend): add cursor pagination`
  plus a body listing the changed files, the role, and a "generated with Nexus" trailer
* optional `push: true`

`gh_create_pr` then opens the PR: it fills `.github/PULL_REQUEST_TEMPLATE.md` (the `<!-- summary -->`
and `<!-- motivation -->` placeholders), and appends a **Nexus run** footer with the role → agent
mapping, branch, commit list and diffstat. Uses the `gh` CLI when installed, otherwise the REST
API with `GITHUB_TOKEN`. Labels and reviewers are optional arguments.

`gh_merge_pr` is `danger` risk: `merge`/`squash`/`rebase`/`close` always ask, even with `--yes`.

---

## VS Code (Phase 5)

**Works today, zero setup** — `.vscode/tasks.json` and `.vscode/launch.json` are in the repo:

* `Ctrl+Shift+P` → *Tasks: Run Task* → **Nexus: chat**, **Nexus: ask (read-only)**,
  **Nexus: task…**, **Nexus: plan**, **Nexus: pick a role…**, **Nexus: pipeline…**,
  **Nexus: roles**, **Nexus: doctor**, **Nexus: serve sidebar**, **Nexus: open panel**
* `F5` → debug configurations for chat / task / ask / pipeline / serve / pytest

**Sidebar extension** ([`vscode-extension/`](vscode-extension/)) — a real activity-bar view with
a live role/agent indicator and native **Approve / Approve & remember / Deny** buttons instead of
a terminal `y/N`:

```bash
cd vscode-extension && npm install && npm run compile   # then F5 in VS Code
```

It is a thin shell: it starts `nexus serve` and loads the same panel UI the browser uses, so CLI,
browser and sidebar behave identically. Settings: `nexus.pythonPath`, `nexus.serverPort`,
`nexus.autoStartServer`, `nexus.autoApprove`. Details: [`docs/vscode.md`](docs/vscode.md).

---

## Pipelines (Phase 6) and memory (Phase 7)

```bash
nexus pipeline ship-feature 'add a rate limiter to the public API'
# backend → qa → security-audit → docs → devops, one run, each step gets the
# previous specialist's `finish` summary as a handoff note.
```

`bug-hunt`, `security-review`, `content-drop` and `release` ship in
[`pipelines.yaml`](pipelines.yaml); add your own the same way you add a role. Full guide:
[`docs/pipelines.md`](docs/pipelines.md).
Inside a run, any role can consult another with the `ask_agent` tool (one question, one answer,
no tools, no recursion).

Every role keeps its own memory file — `.nexus/memory/<role>.md` — injected into that role's
system prompt on the next run in the same repo. The agent writes to it with the `remember` tool,
you can write to it with `/remember` in the REPL.

---

## Project layout

```
Nexus-Agent/
├── agents.yaml              # which model lives where (claude, hermes, ollama, uncensored, openrouter)
├── roles.yaml               # 107 curated roles → persona + keywords + agent  (generated)
├── roles.d/                 # 852 imported roles (5 packs) - `nexus addagent` writes here
├── pipelines.yaml           # multi-role handoff pipelines
├── pyproject.toml           # `pip install -e .` → `nexus`
├── .env.example
├── .vscode/                 # tasks.json, launch.json, settings.json, extensions.json
├── .github/
│   ├── PULL_REQUEST_TEMPLATE.md   # auto-filled by gh_create_pr
│   └── workflows/ci.yml           # tests + roles.yaml sync check
├── scripts/build_roles.py   # the role data + generator + validator
├── nexus/
│   ├── cli.py               # entry point, REPL, /commands (incl. /addagent)
│   ├── agent.py             # main loop: route → model → tools → repeat, pipelines
│   ├── router.py            # keyword match + narrowed LLM classifier fallback
│   ├── importers.py         # `addagent`: fetch a GitHub repo → discover agents → roles.d pack
│   ├── providers.py         # Anthropic adapter + OpenAI-compatible adapter + fallback chain
│   ├── tools.py             # files, shell, git, GitHub, memory, ask_agent, approval gate
│   ├── config.py            # agents/roles/roles.d/pipelines/settings loading + layering
│   ├── server.py            # HTTP + SSE server for the sidebar/panel
│   ├── demo.py              # scripted offline run
│   ├── env.py               # .env loading
│   ├── types.py             # roles, messages, completions, events
│   └── panel/               # the web UI the sidebar embeds
├── vscode-extension/        # Phase 5 sidebar (TypeScript, no runtime deps)
├── tests/                   # 264 offline tests
└── docs/                    # architecture, configuration, tools, pipelines, vscode, security, roles, roadmap, addagent
```

---

## Configuration

`agents.yaml` + `roles.yaml` + `roles.d/*.yaml` + `pipelines.yaml` are loaded in layers, later wins:

1. bundled defaults (inside the wheel)
2. the repo checkout (incl. the committed `roles.d/` import packs)
3. `$NEXUS_HOME` (default `~/.config/nexus`) — your machine-wide overrides
4. `./.nexus/*.yaml` + `./.nexus/roles.d/*.yaml`, then `./agents.yaml` etc. in the project you are working on
5. environment / `.env` (`${VAR:-default}` expansion works inside the YAML)

Behaviour knobs (`settings:` block) and every `NEXUS_*` override are documented in
[`docs/configuration.md`](docs/configuration.md).

---

## Roadmap status

| Phase | Status | Where |
| --- | --- | --- |
| 1 — Core engine (providers, tools, loop, approval gate) | ✅ | `nexus/providers.py`, `nexus/tools.py`, `nexus/agent.py` |
| 2 — Auto role-routing (keywords + LLM fallback) | ✅ | `nexus/router.py`, `roles.yaml` |
| 3 — 100+ roles | ✅ **107 curated + 852 imported = 959** | `roles.yaml`, `roles.d/`, `scripts/build_roles.py` |
| 3b — Import agents from any GitHub repo | ✅ **`/addagent`** | `nexus/importers.py`, `roles.d/*.yaml`, [`docs/addagent.md`](docs/addagent.md) |
| 4 — GitHub workflow polish (branch, commit, PR template) | ✅ | `git_prepare`, `gh_create_pr`, `.github/PULL_REQUEST_TEMPLATE.md` |
| 5 — VS Code integration | ✅ tasks/launch **and** sidebar extension + web panel | `.vscode/`, `vscode-extension/`, `nexus/server.py`, `nexus/panel/` |
| 6 — Multi-agent collaboration | ✅ `ask_agent` + **pipeline mode** | `pipelines.yaml`, `NexusAgent.run_pipeline` |
| 7 — Per-role memory | ✅ | `.nexus/memory/<role>.md`, `remember` tool |
| 8 — Model health & fallback | ✅ `fallback:` chain + `nexus doctor` | `agents.yaml`, `ProviderRegistry.call_with_fallback`, `cmd_doctor` |
| Next — streaming tokens, MCP tools, sessions/resume, per-repo role learning | ⏳ | [`docs/roadmap.md`](docs/roadmap.md), [`docs/architecture.md`](docs/architecture.md#whats-next) |

---

## Testing & development

```bash
pip install -e ".[dev]"
python -m pytest -q                     # 264 tests, all offline (scripted providers)
python scripts/build_roles.py --check   # validate the role data
python scripts/build_roles.py --stats   # counts by category / agent
ruff check nexus tests
```

Nothing in the test suite touches the network: providers are scripted, the server tests run
against a real socket on an ephemeral port, and `--demo` covers the loop end to end.

---

## Safety & ethics note

The `pentesting` / `osint` / `malware-analysis` / `reverse-engineering` / `ctf-player` /
`bug-bounty` roles are routed to an uncensored local model. That is for **your own systems, your
own repos, your CTF lab, and engagements you have written permission for**. A persona prompt does
not authorize anything: if you point it at a real third-party target you do not own, no system
prompt makes that legal or acceptable, and the personas say so explicitly. Nexus adds its own
guardrails on top (deny-listed commands, `danger` risk class, path guard, plan mode) — see
[`docs/security.md`](docs/security.md).

## License

MIT — see [LICENSE](LICENSE). © NXR HAMID
