# Configuration

Three YAML files, one `.env`, and a handful of environment variables. No code changes, ever.

* [`agents.yaml`](../agents.yaml) — **which model lives where**
* [`roles.yaml`](../roles.yaml) — **who does what** (generated, see below)
* [`pipelines.yaml`](../pipelines.yaml) — **who hands off to whom**

## Load order

Every file is loaded in layers and deep-merged, **later wins**:

| # | Location | Use |
| --- | --- | --- |
| 1 | bundled defaults inside the installed wheel | ships with `pip install nexus-agent` |
| 2 | the Nexus checkout (`<repo>/agents.yaml`, `<repo>/roles.d/*.yaml`, …) | the team defaults you see in this repo |
| 3 | `$NEXUS_HOME` (default `~/.config/nexus`) | your machine-wide overrides |
| 4 | `<project>/.nexus/*.yaml` + `<project>/.nexus/roles.d/*.yaml` | project-local specialists + imports |
| 5 | `<project>/agents.yaml`, `roles.yaml`, … | project root overrides |

So a repo can add a `repo-historian` role in `.nexus/roles.yaml` without touching the 107
bundled ones, and your `~/.config/nexus/agents.yaml` can repoint `hermes` at a beefier GPU box
for every project at once.

**`roles.d/` drop-in packs.** Any `roles.d/*.yaml` at each layer is merged on top of the main
`roles.yaml`, lowest precedence first. This is where [`nexus addagent`](addagent.md) writes
imported agents — the repo ships five committed packs under `<repo>/roles.d/` (852 roles), and
`/addagent` writes project-local packs to `<project>/.nexus/roles.d/`. A pack is just a
`roles.yaml` layer, so you can hand-edit or delete one at any time.

`.env` files are loaded from `<workspace>/.env`, `./.env` and `$NEXUS_HOME/.env` (first wins,
already-set environment variables are never overwritten).

## `agents.yaml`

```yaml
default_agent: claude          # used by roles with agent: "" and as the last resort

agents:
  claude:
    provider: anthropic        # anthropic | openai-compatible
    model: claude-sonnet-5-5
    api_key_env: ANTHROPIC_API_KEY
    base_url: ${ANTHROPIC_BASE_URL:-https://api.anthropic.com}
    max_tokens: 8192           # per-call output cap
    temperature: null          # null = provider default
    timeout: 180               # seconds
    supports_tools: true
    tool_mode: native          # native | text | none
    fallback: null             # agent name to try when this one fails
    description: …
    # any other key is passed through to the API request as-is (e.g. top_p, thinking)

  ollama-uncensored:
    provider: openai-compatible
    model: dolphin-mixtral:latest
    base_url: ${OLLAMA_HOST:-http://localhost:11434}/v1
    api_key: ollama            # the OpenAI shim requires a key; Ollama ignores it
    tool_mode: text            # dolphin has NO tool template -> JSON contract in the prompt
    fallback: ollama           # Phase 8: keep working when the local box is off
```

### `tool_mode`

| Mode | What happens | Use for |
| --- | --- | --- |
| `native` | tools are sent as `tools` (Anthropic) / `tools` (OpenAI function calling) | Claude, `hermes3`, `llama3.1`, `qwen`, most hosted models |
| `text` | the tool schemas are rendered into the system prompt as a JSON contract; the reply is parsed for a fenced `{"name": …, "arguments": …}` block | `dolphin-mixtral`, `dolphin-mistral`, any model whose chat template has no tool parser |
| `none` (`supports_tools: false`) | no tools at all — pure conversation | chat-only endpoints, embeddings-ish models |

Text mode also rescues native models that *leak* tool calls into prose (a common local-model
bug): if the response contains a JSON tool object and no structured `tool_calls`, Nexus parses it
and keeps the loop going.

### `fallback`

`ProviderRegistry.call_with_fallback` walks the chain on any `ProviderError`, emits an
`agent` event for each hop (`⚠ agent hermes failed: …` / `⇢ falling back to agent 'claude'`),
and refuses to loop (`a → b → a` stops after both were tried). Cycles and dead ends are reported,
not hung on.

### Model IDs used by default

| Agent | Model | Notes |
| --- | --- | --- |
| `claude` | `claude-sonnet-5-5` | best speed/intelligence balance for code |
| `claude-big` | `claude-opus-5-5` | long-horizon architecture work |
| `claude-cheap` | `claude-haiku-5-5` | the routing classifier |
| `hermes` | `hermes3:8b` | local, tool-capable |
| `hermes-big` | `${HERMES_BIG_MODEL:-hermes3:70b}` | local, if you have the VRAM |
| `ollama` | `${OLLAMA_MODEL:-llama3.1:8b}` | generic local |
| `ollama-uncensored` | `dolphin-mixtral:latest` | uncensored, text-mode tools |
| `openrouter` | `${OPENROUTER_MODEL:-nousresearch/hermes-3-llama-3.1-405b}` | hosted open models |

`claude-sonnet-4-5` retires 2026-11-30 — that is why the default is `claude-sonnet-5-5`.

## `roles.yaml`

Generated from [`scripts/build_roles.py`](../scripts/build_roles.py):

```bash
python scripts/build_roles.py --stats   # counts by category / agent
python scripts/build_roles.py --check   # validate, do not write
python scripts/build_roles.py --docs    # also regenerate docs/roles.md
python scripts/build_roles.py           # write roles.yaml (+ --docs for the docs page)
```

CI fails if `roles.yaml` or `docs/roles.md` drift from the generator.

The generated `roles.yaml` holds the **107 curated** roles. Imported agents are kept separate, in
`roles.d/*.yaml` packs written by [`nexus addagent`](addagent.md), so regenerating the core never
touches them. Both merge into one registry at load time (959 roles in this repo).

### Role schema

| Key | Required | Meaning |
| --- | --- | --- |
| `title` | ✓ | human-readable name for the CLI |
| `category` | ✓ | grouping for `nexus roles` |
| `agent` | ✓ (may be `""`) | key in `agents.yaml`; `""` inherits `default_agent` |
| `persona` | ✓ | the system prompt for this role |
| `keywords` | recommended | routing signals; `- text: kw` + `weight: 3` for strong ones |
| `aliases` | – | other names accepted by `/role` and the router |
| `model` | – | per-role model override (e.g. one role on opus) |
| `temperature` | – | per-role sampling temperature |
| `focus` | – | bullet list appended to the persona |
| `instructions` | – | extra rules appended after the persona |
| `tools` | – | whitelist; the role only sees these tools |

Validator rules: names and aliases are `^[a-z0-9][a-z0-9-]{0,63}$`, unique, and aliases may not
shadow a real role name; keywords must be lowercase and non-empty; every `agent` must exist in
`agents.yaml`; every role except `general` needs at least 3 keywords; the file must contain 100+
roles.

## `settings:` (in `agents.yaml`)

| Key | Default | Env override | Meaning |
| --- | --- | --- | --- |
| `auto_route` | `true` | `NEXUS_AUTO_ROUTE` | route every task; off = keep the pinned role |
| `router_min_score` | `1.0` | `NEXUS_ROUTER_MIN_SCORE` | keyword score needed to skip the LLM |
| `router_llm_fallback` | `true` | `NEXUS_ROUTER_LLM` | classify with a model when no keyword hits |
| `router_agent` | `claude-cheap` | `NEXUS_ROUTER_AGENT` | which agent runs the classifier |
| `max_turns` | `24` | `NEXUS_MAX_TURNS` | hard stop for the tool loop |
| `auto_yes` | `false` | `NEXUS_YES` | never prompt (danger calls still blocked) |
| `allow_dangerous` | `false` | `NEXUS_ALLOW_DANGEROUS` | deny-listed commands become "ask" |
| `command_timeout` | `120` | `NEXUS_COMMAND_TIMEOUT` | seconds per shell command |
| `max_output_chars` | `24000` | – | tool output clipping |
| `max_read_chars` | `120000` | – | `read_file` clipping |
| `context_char_budget` | `240000` | – | compact older tool results above this |
| `memory_enabled` | `true` | `NEXUS_MEMORY` | per-role memory injection |
| `memory_dir` | `.nexus/memory` | – | where role notes live |
| `memory_max_chars` | `4000` | – | how much memory goes into the prompt |
| `auto_branch` | `true` | `NEXUS_AUTO_BRANCH` | `git_prepare` branch naming |
| `branch_prefix` | `feat` | – | branch prefix |
| `auto_commit_message` | `true` | – | generate conventional commit messages |
| `pr_template` | `.github/PULL_REQUEST_TEMPLATE.md` | – | template filled by `gh_create_pr` |
| `always_allow_tools` | `[]` | – | never prompt for these tools |
| `never_allow_tools` | `[]` | – | refuse these tools outright |
| `blocked_command_patterns` | `[]` | – | extra deny-list regexes |

## Environment variables

Everything lives in [`.env.example`](../.env.example):

```
ANTHROPIC_API_KEY      GITHUB_TOKEN / GH_TOKEN      ANTHROPIC_BASE_URL
OLLAMA_HOST            OLLAMA_MODEL                 HERMES_BIG_MODEL
OPENROUTER_API_KEY     OPENROUTER_MODEL
NEXUS_HOME             NEXUS_WORKSPACE              NEXUS_DEFAULT_AGENT
NEXUS_AUTO_ROUTE       NEXUS_ROUTER_AGENT           NEXUS_ROUTER_LLM
NEXUS_ROUTER_MIN_SCORE NEXUS_MAX_TURNS              NEXUS_YES
NEXUS_ALLOW_DANGEROUS  NEXUS_MEMORY                 NEXUS_AUTO_BRANCH
NEXUS_COMMAND_TIMEOUT  NEXUS_SERVE_TOKEN            NEXUS_SERVE_PORT
NEXUS_DEMO             NO_COLOR / NEXUS_NO_COLOR
```

`${VAR}` and `${VAR:-fallback}` are expanded anywhere inside the YAML files, so you can repoint an
agent without editing the file: `OLLAMA_HOST=http://gpu-box:11434 nexus task …`.
