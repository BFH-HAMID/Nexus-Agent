# Architecture

## The one-paragraph version

A task string enters `NexusAgent.run()`. The **router** turns it into a **role** (keyword
fast-match, LLM classifier fallback). The role carries a **persona** (system prompt) and an
**agent** name; the agent name resolves to a **provider** (Gemini, DeepSeek, OpenRouter — or any
OpenAI-compatible endpoint) in `agents.yaml`. The provider is called with the persona, the conversation and the **tool schemas**;
tool calls come back, go through the **approval gate**, execute in the **tool layer**, and their
results are appended as `tool_result` blocks. The loop repeats until the model stops calling
tools, calls `finish`, hits `max_turns`, or errors out. Events stream to the CLI renderer *and* to
the HTTP/SSE server the VS Code sidebar embeds.

## Module map

| Module | Responsibility | Key types |
| --- | --- | --- |
| `nexus/types.py` | provider-agnostic data model | `Role`, `Keyword`, `Message`, `TextBlock`, `ToolCall`, `ToolResultBlock`, `Completion`, `ToolSpec`, `Risk`, `Event`/`EventType`, `Usage`, `Provider`, `ProviderError` |
| `nexus/config.py` | layered YAML loading, `${VAR}` expansion, settings | `AgentConfig`, `Role`, `RoleRegistry`, `Settings`, `Pipeline`, `NexusConfig`, `load_config()` |
| `nexus/providers.py` | model adapters (Gemini / DeepSeek / OpenRouter, OpenAI dialect) + fallback chain | `OpenAICompatProvider`, `AnthropicProvider`, `ProviderRegistry`, `parse_tool_call_from_text()` |
| `nexus/router.py` | task → role | `Router`, `RouteDecision`, `CLASSIFIER_PROMPT` |
| `nexus/tools.py` | tool implementations + safety | `ToolRegistry`, `ToolContext`, `ApprovalGate`, `PathGuard`, `RoleMemory`, `build_tools()` |
| `nexus/agent.py` | the loop, sessions, compaction, pipelines | `NexusAgent`, `RunResult`, `render_system_prompt()` |
| `nexus/cli.py` | argparse UI, REPL, `/commands`, `doctor` | `main()`, `emit_console()`, `interactive_approval()` |
| `nexus/server.py` | stdlib HTTP + SSE for sidebar/panel | `ServerState`, `Session`, `serve()` |
| `nexus/panel/` | the web UI (also embedded by the extension) | `panel.html`, `panel.js` |
| `nexus/demo.py` | scripted offline run | `DemoRunner`, `patch_agent_for_demo()` |
| `nexus/keys.py` | API key store (`~/.config/nexus/keys.json`), env resolution | `save_key()`, `get_key()`, `key_source()` |
| `nexus/env.py` | *optional* legacy `.env` loading (with or without python-dotenv) | `load_dotenv_files()` |

Dependency direction is strictly one-way:

```
types ← config ← providers ← router ← agent ← cli
   ↑        ↑         ↑          ↑        ↑
   └────────┴─────────┴── tools ─┴────────┴── server / demo
```

`tools.py` never imports `agent.py`; `agent.py` never imports `cli.py`. That is why the same loop
can be driven from a terminal, an HTTP request or a test with zero mocking of the surroundings.

## Event stream

Everything the UI shows is an `Event`. Producers never print directly.

| Type | Emitted when | Payload highlights |
| --- | --- | --- |
| `status` | informational line | `message` |
| `role` | routing decision | `role`, `agent`, `method`, `score`, `hits`, `note` |
| `agent` | provider chosen / failed / fell back | `agent`, `model`, `provider`, `tool_mode`, or `error` / `message` |
| `text` | the model wrote prose | `text`, `role` |
| `tool_call` | a tool is about to run | `tool`, `args`, `preview`, `risk` |
| `tool_result` | a tool returned | `tool`, `is_error`, `preview`, `chars` |
| `approval` | waiting for a human | `tool`, `args`, `preview`, `risk`, `blocked` |
| `memory` | a role note was saved | `role`, `path` |
| `error` | unrecoverable for this run | `message` |
| `done` | run finished | `status`, `result` |

The CLI maps these to coloured lines; the server serializes them as SSE `data:` frames; the panel
renders them as a log plus the approval box. Adding a UI means adding a handler — nothing else.

## Routing internals

```
route(task, forced=None)
├── forced?            → RouteDecision(method="manual")
├── keyword_route(task)                     # stage 1, ~0 ms
│   ├── score every role: Σ weight of word-boundary keyword hits
│   ├── best < settings.router_min_score    → None (fall through)
│   ├── tie at the top + LLM enabled        → llm_route(task, prefer=tied)
│   └── else                                → RouteDecision(method="keyword", score, hits)
├── llm_route(task)                          # stage 2, one cheap call per 40-role chunk
│   ├── CLASSIFIER_PROMPT (verbatim from the roadmap), temperature 0, max_tokens 32
│   ├── _extract_role(): strip fences/quotes/prose → first a-z0-9- token
│   ├── sanitize_role_name() + registry lookup
│   └── unknown → RouteDecision(method="default", role=general)
└── nothing matched → general
```

Word-boundary matching matters: `api` must not fire inside `rapid`, but `next.js`, `c++` and
`node:fs` must still match as substrings. Keywords may carry weights
(`- text: nmap` / `weight: 3`) so specific roles beat generic ones.

## Provider internals

Both adapters normalize to `Completion(content=[TextBlock|ToolCall], stop_reason, usage)`:

* **OpenAI dialect** (Gemini / DeepSeek / OpenRouter / anything compatible) —
  `<base>/chat/completions` with `tools` as function schemas; tool results become `role: "tool"`
  messages. `_endpoint()` accepts the three real-world base shapes (`…/v1`, Gemini's
  `…/v1beta/openai`, or a full `…/chat/completions` URL). A hosted provider with no key fails
  locally and instantly instead of burning a request.
* **Anthropic** (legacy, only if you point `agents.yaml` at it) — official SDK when installed,
  raw `httpx` otherwise. `tool_use` blocks in, `tool_result` blocks out,
  `anthropic-version: 2023-06-01`.

Both apply `_retry` (3 attempts, linear backoff) for transient statuses
(408/409/425/429/5xx/529) and connection errors. Both recover text-mode tool calls when
`tool_mode: text`, or when a native model leaks the call into prose.

`ProviderRegistry.call_with_fallback` owns the Phase 8 chain: try → on `ProviderError` emit a
warning event → hop to `provider.fallback` → repeat, never revisiting a name.

## The loop, precisely

```
run(task, role=None, read_only=False, max_turns=None, history=None)
 1. route (or force) → role, agent_cfg
 2. build ToolContext + ToolRegistry (+ role whitelist, + read-only pruning)
 3. system = BASE_SYSTEM + persona + focus + runtime + tool hints + role memory [+ plan mode]
 4. messages = history + [user: task]
 5. loop while turn < max_turns:
      compact if over context_char_budget
      provider, completion = call_with_fallback(agent_cfg.name, system, messages, specs)
      emit text; append assistant blocks
      no tool calls → stop_reason = completion.stop_reason; break
      for each call: gate.check → registry.call → append ToolResultBlock
      3 consecutive errors → append a plain-text nudge (never a phantom tool_use_id)
      finish called → stop_reason = "finish"; break
      6 consecutive errors → stop_reason = "too many tool errors"; break
 6. RunResult(text, role, agent, turns, usage, tool_calls, changed_files, summary,
              followups, stopped_reason, duration_s, messages, ok)
```

`chat_turn()` is `run()` with `history=self.history`, and `reset()` clears history, the pinned
role, the usage counters and the session-scoped tool approvals.

## Server / sidebar

`nexus serve` is stdlib-only (`ThreadingHTTPServer`). One `Session` per POST `/api/task`, running
the real `NexusAgent` in a daemon thread. The session keeps a bounded event log (replayed to late
subscribers) and a live `queue.Queue` for the SSE stream, with a 15 s heartbeat and a `None`
sentinel that closes streams when the run ends.

Approvals block the worker thread on a `threading.Event` (10 min timeout) until
`POST /api/approve/<id>` supplies a decision — so the sidebar's buttons are the *same* gate the
terminal's `y/N` uses, not a parallel implementation.

Binding `0.0.0.0` without `NEXUS_SERVE_TOKEN` prints a warning; with a token set, every request
needs `X-Nexus-Token` (or `?token=…`, because `EventSource` cannot send headers).

## Testing strategy

* providers are scripted (`FakeProvider`) — no network, no keys, deterministic;
* the registry auto-fills any agent named in `agents.yaml` so routing to `deepseek`/`openrouter`
  never explodes in a test, and the key store is redirected to `tmp_path`;
* tools run against a real throwaway git repo in `tmp_path`;
* the server tests bind an ephemeral port and speak real HTTP/SSE;
* `--demo` exercises the whole loop as an integration test.

## What's next

| Idea | Sketch |
| --- | --- |
| Token streaming | `content_block_delta` / `stream: true` → new `EventType.TEXT_DELTA`; the SSE pipe is already incremental |
| MCP tools | an `MCPToolSource` that registers remote tools into `ToolRegistry` with the same risk classes |
| Sessions & resume | persist `RunResult.messages` to `.nexus/sessions/<id>.json`, add `nexus resume <id>` |
| Per-repo role learning | promote frequently corrected routes into `.nexus/roles.yaml` keywords automatically |
| Cost accounting | per-agent price table in `agents.yaml`, `Usage` already tracks tokens; show `~$` in `/stats` |
| Windows | shell tool assumes POSIX (`shlex`, `echo`); add a `cmd`/PowerShell path and CI on windows-latest |
