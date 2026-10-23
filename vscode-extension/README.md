# Nexus Agent — VS Code sidebar (Phase 5)

A thin shell around the Python core: it starts `nexus serve` and loads the
built-in panel UI in a webview, so the sidebar shows the **active role/agent
live** and gives you **Approve / Deny buttons** instead of a terminal `y/N`.

## Run it (F5)

```bash
cd vscode-extension
npm install
npm run compile       # or: npm run watch
```

Then in VS Code: open this repo root, press **F5** (`Extension Development
Host`), and click the **Nexus** icon in the activity bar.

Or from the Extension Development Host command palette:

| Command | What it does |
| --- | --- |
| `Nexus: Open sidebar` | focus the panel (Ctrl/Cmd+Shift+N) |
| `Nexus: Start local server` | spawn `nexus serve` |
| `Nexus: Stop local server` | kill it again |
| `Nexus: Send selection as task` | editor context menu / Ctrl+Alt+N |
| `Nexus: Ask about current file (read-only)` | plan-mode question |
| `Nexus: Pin a role…` | quick-pick over all 107 roles |
| `Nexus: Doctor (health check)` | dump server health to the output channel |

## Settings

| Setting | Default | Notes |
| --- | --- | --- |
| `nexus.pythonPath` | `python` | interpreter/entrypoint with nexus installed; a project `.venv/bin/nexus` is auto-detected |
| `nexus.serverHost` | `127.0.0.1` | keep loopback unless you know why you need otherwise |
| `nexus.serverPort` | `8787` | |
| `nexus.autoStartServer` | `true` | start the server when the sidebar opens |
| `nexus.autoApprove` | `false` | runs the server with `--yes`; approvals stop prompting |
| `nexus.workspaceFolder` | `""` | project Nexus works in (default: first workspace folder) |

## Package it

```bash
npm run package      # produces nexus-agent-sidebar-0.1.0.vsix
code --install-extension nexus-agent-sidebar-0.1.0.vsix
```
