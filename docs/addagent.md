# `/addagent` — import agents from any GitHub repo

Nexus ships **107 curated roles**. `/addagent` lets you grow that team from any
public GitHub repo of agent/skill definitions — no code changes, ever. Point it
at a URL and it fetches the repo, discovers the agents inside, converts each one
into a Nexus role (persona + keywords + model mapping), and writes a drop-in
YAML pack that the config loader merges automatically.

```bash
nexus addagent https://github.com/obra/superpowers
nexus addagent https://github.com/msitarzewski/agency-agents --dest repo
nexus addagent https://github.com/affaan-m/ecc/tree/main/skills      # just a subdir
nexus addagent https://github.com/you/one-agent/blob/main/AGENT.md    # a single file
```

Inside the REPL it is `/addagent <url>`; it imports **and hot-reloads**, so the
new roles are routable on the very next task without restarting:

```
you> /addagent https://github.com/obra/superpowers
  cloning obra/superpowers ...
  discovered 15 agent(s) in obra/superpowers
✓ imported 15 agent(s) from superpowers -> .nexus/roles.d/superpowers.yaml
  974 roles now available - just describe a task, Nexus routes to the right one
```

## What it discovers

Three strategies run together and are de-duplicated:

| Strategy | Recognises | Example repos |
| --- | --- | --- |
| **skills** | `**/SKILL.md` / `**/AGENT.md` with `name` + `description` frontmatter and a markdown body | obra/superpowers, affaan-m/ecc, morluto/rea |
| **personas** | any `.md` with a `name`/`title` in frontmatter whose body reads like a persona | msitarzewski/agency-agents (`<category>/<agent>.md`) |
| **apps** | a directory under an `*agent*` / `*skill*` / `rag` path holding a `README.md` + code | shubhamsaboo/awesome-llm-apps |

Quality filters keep the roster clean:

* `docs/`, `tests/`, `examples/`, `references/`, `templates/`, `node_modules/`,
  `legacy/`, `deprecated/` and other non-agent trees are skipped;
* localized mirrors (`docs/es/skills/…`) and duplicate copies (`pi/core/skills/…`
  mirroring `skills/…`) collapse to one role — the richest persona wins;
* description-only slash-command stubs (no `name:`) are not treated as agents;
* numbered tutorial lessons (`1_starter_agent`) and structural dirs
  (`backend/agents/README.md` → a bogus role named `agents`) are dropped.

## How each agent becomes a role

| Nexus field | Source |
| --- | --- |
| `name` | the skill dir / file stem / app dir, slugified to `a-z0-9-` |
| `title` | frontmatter `name`, else the humanised slug |
| `category` | the top grouping dir (`skills`, `academic`, `engineering`, `rag-tutorials`, …) |
| `persona` | frontmatter `description` + the markdown body (capped at 6000 chars) |
| `keywords` | auto-derived from name + title + description (weighted, de-stopworded) so **keyword routing works without an LLM call** |
| `aliases` | the original slug / title when the name was disambiguated |
| `agent` | `""` (inherits `default_agent` → `gemini`), **except** security/pentest/OSINT/reverse-engineering categories which map to `openrouter-uncensored`, matching the core roster |

Names never clobber a curated core role: on collision the import is suffixed
(`api-design` → `api-design-ecc`), then `-2`, `-3`, … Cross-repo collisions are
handled the same way, so importing five repos can't overwrite one another.

## Where packs are written (`--dest`)

| `--dest` | Path | Use |
| --- | --- | --- |
| `project` (default) | `<workspace>/.nexus/roles.d/<repo>.yaml` | per-project, gitignored |
| `global` | `~/.config/nexus/roles.d/<repo>.yaml` | every project on this machine |
| `repo` | `<nexus-checkout>/roles.d/<repo>.yaml` | **committed**, ships with Nexus |

Every `roles.d/*.yaml` is just a `roles.yaml` layer — the config loader merges
them in precedence order (bundled → repo → `$NEXUS_HOME` → project `.nexus/` →
project root), later wins. You can hand-edit or delete a pack at any time; it is
plain YAML.

## Flags

```
nexus addagent <url>
  --dest project|global|repo   where to write the pack (default: project)
  --agent NAME                 force every imported role onto one agent
  --category SLUG              prefix imported categories with this slug
  --limit N                    import at most N agents
  --dry-run                    discover + convert, write nothing (preview)
  --force                      overwrite an existing pack for this repo
  --list                       show installed packs and their role counts
  --json                       machine-readable result
  -C, --workspace DIR          project to import into
```

`--dry-run` is the safe way to see what a repo would contribute before writing
anything. `--list` audits what is installed:

```bash
$ nexus addagent --list
/home/you/Nexus-Agent/roles.d
  agency-agents.yaml                 283 roles
  awesome-llm-apps.yaml              123 roles
  ecc.yaml                           426 roles
  rea.yaml                             5 roles
  superpowers.yaml                    15 roles
```

## Removing / refreshing an import

A pack is one file. To drop a repo's agents, delete its pack:

```bash
rm .nexus/roles.d/ecc.yaml          # or roles.d/ecc.yaml if --dest repo
```

To refresh after the upstream repo changes, re-run with `--force`:

```bash
nexus addagent https://github.com/affaan-m/ecc --dest repo --force
```

## Routing with a big roster

Imported roles get auto-generated keywords, so stage-1 keyword routing handles
most tasks with **no LLM call** even at ~1000 roles. When keywords tie or miss,
the LLM classifier is asked about a *narrowed* set of the ≤40 most relevant
candidates (not the whole roster in chunks), so a huge team stays a single cheap
classifier call. See [`docs/architecture.md`](architecture.md#routing-internals).

## The five packs that ship with Nexus

`roles.d/` is committed with these already imported (regenerate any of them with
the command in its file header):

| Pack | Roles | What it adds |
| --- | --- | --- |
| `agency-agents` | 283 | a full "AI agency": academic, design, engineering, finance, game-dev, healthcare, marketing, product, sales, security, strategy… |
| `ecc` | 426 | affaan-m's agent-harness skills + subagents (framework patterns, testing, security, per-language specialists) |
| `awesome-llm-apps` | 123 | runnable agent/RAG reference apps as specialists (chess, finance, legal, travel, RAG variants…) |
| `superpowers` | 15 | obra's development-methodology skills (brainstorming, TDD, systematic debugging, writing plans…) |
| `rea` | 5 | reverse-engineering skills (native binaries, apps, CI audits) → uncensored local model |

Together with the 107 core roles that is **959 specialists**. `nexus roles`
lists them by category; `nexus roles -s <needle>` searches.

## Limits & notes

* Fetching uses `git clone --depth 1` (falling back to a codeload tarball), so
  the sandbox/host needs `git` and network to github.com.
* Personas are capped at 6000 chars; the source path is kept in the pack header
  so you can always go back to the original.
* Importing is a one-time snapshot. Upstream edits are not tracked — re-run with
  `--force` to refresh.
* These are third-party prompts. Skim a pack before you let its roles run with
  `--yes`; the usual approval gate and deny-list still apply to everything they do.
