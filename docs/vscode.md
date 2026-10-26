# VS Code integration (Phase 5)

Two levels, both working:

1. **Tasks + debug configs** — no install, no build, works the moment you clone the repo.
2. **Sidebar extension** — an activity-bar view with live role/agent status and native
   approve/deny buttons instead of a terminal `y/N`.

## 1. Tasks and debugging (zero setup)

`.vscode/tasks.json` and `.vscode/launch.json` are committed. With `nexus` on your PATH
(`pip install -e .` inside the workspace venv):

`Ctrl+Shift+P` → **Tasks: Run Task** →

| Task | What happens |
| --- | --- |
| **Nexus: chat** | the REPL in a dedicated terminal |
| **Nexus: ask (read-only)** | prompts for a question, runs in plan mode |
| **Nexus: task…** | prompts for a task, asks before each write |
| **Nexus: task --yes (no prompts)** | same, auto-approving non-dangerous calls |
| **Nexus: plan (read-only)** | implementation plan, mutations refused |
| **Nexus: pick a role…** | quick-pick over roles, then a task prompt |
| **Nexus: pipeline…** | pick `ship-feature` / `bug-hunt` / … and describe the goal |
| **Nexus: roles** | print the team |
| **Nexus: doctor** | health check |
| **Nexus: serve sidebar (background)** | start the HTTP+SSE server on port 8787 |
| **Nexus: open panel (browser)** | serve + open `/panel` |
| **Nexus: run tests** | `python -m pytest -q` |

`F5` → **Nexus: chat (debug)**, **Nexus: task (debug)**, **Nexus: ask (read-only, debug)**,
**Nexus: pipeline ship-feature (debug)**, **Nexus: serve sidebar (debug)**, **Nexus: doctor**,
**Pytest: nexus tests**. They run with `justMyCode: false`, so you can step into `router.py` and
`agent.py`. No `.env` file is needed: API keys come from `nexus keys set` (or your shell
environment). Set them once from the integrated terminal:

```bash
nexus keys set gemini
nexus doctor
```

## 2. Sidebar extension

```bash
cd vscode-extension
npm install
npm run compile        # or npm run watch
```

Open the repo root in VS Code and press **F5** (Extension Development Host), then click the
**Nexus** icon in the activity bar. Or package it:

```bash
npm run package        # nexus-agent-sidebar-0.2.0.vsix
code --install-extension nexus-agent-sidebar-0.2.0.vsix
```

### What you get

* **Live header**: workspace, current role, current agent/model, run status dot.
* **Role picker** grouped by category (all 107), plus a **pipeline** picker.
* **Read-only** and **auto-approve** toggles.
* **Event log** mirroring the CLI: routing decision, agent switch/fallback, assistant prose,
  every tool call and its result (errors in red).
* **Approval box** with *Approve* / *Approve & remember this tool* / *Deny* — plus a native VS
  Code modal when the extension hosts the view.
* **Result panel**: summary, follow-ups, files touched, turns/tool calls/tokens/duration.
* `Ctrl/Cmd+Shift+N` opens the sidebar; `Ctrl/Cmd+Alt+N` sends the current selection as a task
  (also in the editor context menu).

### Commands

| Command | Effect |
| --- | --- |
| `Nexus: Open sidebar` | focus the view (starts the server if `autoStartServer`) |
| `Nexus: Start local server` | spawn `nexus serve` and report readiness |
| `Nexus: Stop local server` | terminate it |
| `Nexus: Send selection as task` | selection + file path become the task context |
| `Nexus: Ask about current file (read-only)` | plan-mode question about the open file |
| `Nexus: Pin a role…` | quick-pick over `/api/roles`, pushes the choice into the panel |
| `Nexus: Doctor (health check)` | dumps `/health`, role and agent info to the output channel |

### Settings

| Setting | Default | Notes |
| --- | --- | --- |
| `nexus.pythonPath` | `python` | a project `.venv/bin/nexus` is auto-detected first |
| `nexus.serverHost` | `127.0.0.1` | keep it loopback |
| `nexus.serverPort` | `8787` | |
| `nexus.autoStartServer` | `true` | start `nexus serve` when the view opens |
| `nexus.autoApprove` | `false` | runs the server with `--yes` — approvals stop prompting |
| `nexus.workspaceFolder` | `""` | project Nexus works in (default: first workspace folder) |

### How it is wired

The extension contains **no agent logic**. It:

1. spawns `nexus serve --host 127.0.0.1 --port <port>` (health-polling `/health` until ready),
2. loads `<server>/panel` inside a webview iframe with a strict CSP,
3. relays `postMessage` traffic both ways:
   * panel → host: `ready`, `approval` (→ native modal → `POST /api/approve/<id>`), `role`, `done`
   * host → panel: `task` (with `autorun`), `approve`, `role`
4. keeps a status-bar item showing the current role and, on completion, the summary.

Because the panel and the CLI talk to the same server and the same Python core, behaviour,
approvals and events are identical everywhere. If the server is down, the webview shows the exact
command to start it and re-probes every 5 seconds.

### Demo mode in the sidebar

No API key yet? Start the server with `--demo` (or set `NEXUS_DEMO=1`) and the sidebar runs a
scripted end-to-end task: routing, file read, a write you can approve or deny, a memory note, a
shell command, an `ask_agent` consultation and a final summary — with zero tokens spent.

### Connection setup and downloaded Ollama models

Both UIs include **Connections & local models**. Refresh and select any model
installed on `OLLAMA_HOST` (default `http://localhost:11434`). Selection pins that
run's roles, router and learning model locally with text-mode tools and **no hosted
fallback**. Model instruction-following determines tool reliability. Demo remains
scripted. Ollama must be reachable from the Nexus server, not just the browser.

The GitHub card checks the server machine's GitHub CLI account. Install GitHub CLI
there, run `gh auth login --hostname github.com --web`, then `gh auth setup-git`.
Complete browser sign-in and click **Check connection**. No credentials are stored
by this UI. Repository permissions and tool approvals still apply.

To install the extension after packaging, use **Extensions → … → Install from
VSIX** and choose the generated `.vsix` file. A source checkout is not an install.
