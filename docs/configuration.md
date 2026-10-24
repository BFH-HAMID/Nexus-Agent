# Configuration

Three YAML files and a handful of environment variables — **no `.env` file needed**. No code changes, ever.

API keys are supplied once in the terminal (or exported as environment variables) and stored
outside the repo; see [API keys](#api-keys-no-env-file) below.

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
bundled ones, and your `~/.config/nexus/agents.yaml` can repoint `deepseek` at a different model
(or a self-hosted OpenAI-compatible box) for every project at once.

**`roles.d/` drop-in packs.** Any `roles.d/*.yaml` at each layer is merged on top of the main
`roles.yaml`, lowest precedence first. This is where [`nexus addagent`](addagent.md) writes
imported agents — the repo ships five committed packs under `<repo>/roles.d/` (852 roles), and
`/addagent` writes project-local packs to `<project>/.nexus/roles.d/`. A pack is just a
`roles.yaml` layer, so you can hand-edit or delete one at any time.

## API keys (no `.env` file)

Keys are **not** part of the YAML layering. For every agent they resolve in this order:

| # | Source | Example |
| --- | --- | --- |
| 1 | inline `api_key:` in `agents.yaml` | not recommended (it lands in git) |
| 2 | environment variable | `export GEMINI_API_KEY=...`, or `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY` |
| 3 | the terminal store | `nexus keys set gemini` → `~/.config/nexus/keys.json` (mode `600`) |

```bash
nexus keys                        # what is configured, masked + where it came from
nexus keys set deepseek           # hidden prompt, saved to the store
echo "$KEY" | nexus keys set deepseek   # non-interactive (CI)
nexus keys set openrouter --session     # this process only, nothing written
nexus keys test                   # calls /models on each configured provider
nexus keys remove deepseek
```

The store lives in `$NEXUS_HOME/keys.json` (default `~/.config/nexus/keys.json`) and is written
atomically with `chmod 600`. Set `NEXUS_KEYS_FILE` to move it, or `NEXUS_NO_PROMPT=1` /
`--no-key-prompt` to disable the first-run prompt.

A `.env` file is **still read** if one already exists (`<workspace>/.env`, `./.env`,
`$NEXUS_HOME/.env`, first wins; already-set environment variables are never overwritten), but
nothing requires it any more — `nexus init` no longer writes one.

## `agents.yaml`

```yaml
default_agent: gemini          # used by roles with agent: "" and as the last resort

agents:
  gemini:
    provider: gemini           # gemini | deepseek | openrouter | openai-compatible | anthropic
    model: ${GEMINI_MODEL:-gemini-3.8-flash}
    api_key_env: GEMINI_API_KEY
    # base_url is optional for the three API providers - it defaults to the
    # vendor's OpenAI-compatible endpoint
    max_tokens: 8192           # per-call output cap
    temperature: null          # null = provider default
    timeout: 180               # seconds
    supports_tools: true
    tool_mode: native          # native | text | none
    fallback: deepseek         # try this agent when the call fails / no key
    description: …
    # any other key is passed through to the API request as-is (e.g. top_p, reasoning)

  openrouter-uncensored:
    provider: openrouter
    model: ${OPENROUTER_UNCENSORED_MODEL:-cognitivecomputations/dolphin-mistral-24b-venice-edition}
    api_key_env: OPENROUTER_API_KEY
    tool_mode: text            # JSON tool contract in the prompt, parsed back
    fallback: openrouter

  # self-hosted / local models still work through the same adapter
  local:
    provider: openai-compatible
    model: llama3.1:8b
    base_url: ${OLLAMA_HOST:-http://localhost:11434}/v1
    api_key: ollama            # the OpenAI shim requires a key; Ollama ignores it
```

Built-in base URLs (override with `base_url:` at any time):

| `provider` | Endpoint |
| --- | --- |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` |
| `deepseek` | `https://api.deepseek.com/v1` |
| `openrouter` | `https://openrouter.ai/api/v1` |

Everyone else speaks the same dialect, so `provider: openai-compatible` + a `base_url` covers
vLLM, llama.cpp, LM Studio, Together, Groq…

### `tool_mode`

| Mode | What happens | Use for |
| --- | --- | --- |
| `native` | tools are sent as function-calling `tools` (OpenAI dialect) | Gemini, DeepSeek, OpenRouter, most hosted models |
| `text` | the tool schemas are rendered into the system prompt as a JSON contract; the reply is parsed for a fenced `{"name": …, "arguments": …}` block | `dolphin-mixtral`, `dolphin-mistral`, any model whose chat template has no tool parser |
| `none` (`supports_tools: false`) | no tools at all — pure conversation | chat-only endpoints, embeddings-ish models |

Text mode also rescues native models that *leak* tool calls into prose (a common local-model
bug): if the response contains a JSON tool object and no structured `tool_calls`, Nexus parses it
and keeps the loop going.

### `fallback`

`ProviderRegistry.call_with_fallback` walks the chain on any `ProviderError`, emits an
`agent` event for each hop (`⚠ agent deepseek failed: …` / `⇢ falling back to agent 'gemini'`),
and refuses to loop (`a → b → a` stops after both were tried). Cycles and dead ends are reported,
not hung on.

The shipped chains are deliberately cross-provider — `gemini → deepseek → openrouter → gemini` —
so **one configured key is enough** to run the whole roster. A missing key fails locally and
instantly (`agent 'gemini' has no API key - run \`nexus keys set gemini\``), then the fallback
takes over.

### Model IDs used by default

| Agent | Model | Notes |
| --- | --- | --- |
| `gemini` | `${GEMINI_MODEL:-gemini-3.8-flash}` | default workhorse for code |
| `gemini-big` | `${GEMINI_BIG_MODEL:-gemini-4-argon}` | flagship reasoning, 1M context |
| `gemini-flash` | `${GEMINI_FAST_MODEL:-gemini-3.5-flash-lite}` | the routing classifier |
| `deepseek` | `${DEEPSEEK_MODEL:-deepseek-flash}` | cheap bulk work, tool calling |
| `deepseek-pro` | `${DEEPSEEK_PRO_MODEL:-deepseek-v4-pro}` | hard reasoning |
| `openrouter` | `${OPENROUTER_MODEL:-openrouter/auto}` | any hosted open model |
| `openrouter-uncensored` | `${OPENROUTER_UNCENSORED_MODEL:-cognitivecomputations/dolphin-mistral-24b-venice-edition}` | uncensored, text-mode tools |

Model ids move fast: `nexus models <agent>` lists what the provider actually serves right now,
and every id above is overridable from the environment without editing the YAML.

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
| `router_agent` | `gemini-flash` | `NEXUS_ROUTER_AGENT` | which agent runs the classifier |
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

Nothing has to be exported — `nexus keys set` is the interactive path. For CI, or if you prefer
your shell profile:

```
GEMINI_API_KEY         DEEPSEEK_API_KEY             OPENROUTER_API_KEY
GEMINI_MODEL           GEMINI_BIG_MODEL             GEMINI_FAST_MODEL
DEEPSEEK_MODEL         DEEPSEEK_PRO_MODEL
OPENROUTER_MODEL       OPENROUTER_UNCENSORED_MODEL
GITHUB_TOKEN / GH_TOKEN
NEXUS_KEYS_FILE        NEXUS_HOME                   NEXUS_WORKSPACE
NEXUS_DEFAULT_AGENT    NEXUS_AUTO_ROUTE             NEXUS_ROUTER_AGENT
NEXUS_ROUTER_LLM       NEXUS_ROUTER_MIN_SCORE       NEXUS_MAX_TURNS
NEXUS_YES              NEXUS_ALLOW_DANGEROUS        NEXUS_MEMORY
NEXUS_AUTO_BRANCH      NEXUS_COMMAND_TIMEOUT        NEXUS_SERVE_TOKEN
NEXUS_SERVE_PORT       NEXUS_DEMO                   NEXUS_NO_PROMPT
NO_COLOR / NEXUS_NO_COLOR
```

`${VAR}` and `${VAR:-fallback}` are expanded anywhere inside the YAML files, so you can repoint an
agent without editing the file: `GEMINI_MODEL=gemini-3.6-flash nexus task …`.
