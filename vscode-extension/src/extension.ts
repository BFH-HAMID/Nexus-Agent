/**
 * Nexus Agent — VS Code sidebar (Phase 5).
 *
 * The extension is a thin shell: it starts `nexus serve` (the same stdlib HTTP
 * + SSE server the browser panel uses) and loads the built-in panel UI inside a
 * webview iframe. All intelligence stays in Python, so the sidebar, the browser
 * panel and the CLI behave identically.
 *
 * Bridge protocol (panel.js <-> extension host):
 *   panel -> host : {source:"nexus-panel", type:"approval", tool, risk, preview, taskId}
 *   panel -> host : {source:"nexus-panel", type:"done", result}
 *   panel -> host : {source:"nexus-panel", type:"role", role, agent}
 *   host  -> panel: {source:"nexus-host", type:"task", task, autorun, role, readOnly}
 *   host  -> panel: {source:"nexus-host", type:"approve", approved, scope}
 *   host  -> panel: {source:"nexus-host", type:"role", role}
 */

import * as vscode from "vscode";
import { ChildProcess, spawn } from "child_process";
import * as path from "path";
import * as fs from "fs";
import * as http from "http";

const VIEW_ID = "nexusAgent.sidebar";

let serverProcess: ChildProcess | undefined;
let output: vscode.OutputChannel;
let statusItem: vscode.StatusBarItem;
let provider: NexusViewProvider | undefined;

// --------------------------------------------------------------------------- //
// config helpers
// --------------------------------------------------------------------------- //

function cfg() {
  return vscode.workspace.getConfiguration("nexus");
}

function workspaceRoot(): string {
  const configured = String(cfg().get<string>("workspaceFolder") || "").trim();
  if (configured) {
    return configured.replace("${workspaceFolder}", vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? "");
  }
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? process.cwd();
}

function baseUrl(): string {
  const host = String(cfg().get<string>("serverHost") || "127.0.0.1");
  const port = Number(cfg().get<number>("serverPort") || 8787);
  return `http://${host === "0.0.0.0" ? "127.0.0.1" : host}:${port}`;
}

function pythonPath(): string {
  const configured = String(cfg().get<string>("pythonPath") || "python").trim();
  if (configured.includes("${workspaceFolder}")) {
    return configured.replace("${workspaceFolder}", workspaceRoot());
  }
  // Prefer a project-local venv when it exists.
  const root = workspaceRoot();
  for (const candidate of [
    path.join(root, ".venv", "bin", "nexus"),
    path.join(root, "venv", "bin", "nexus"),
    path.join(root, ".venv", "Scripts", "nexus.exe"),
  ]) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return configured;
}

function getJSON(url: string, timeoutMs = 1500): Promise<any> {
  return new Promise((resolve) => {
    const req = http.get(url, { timeout: timeoutMs }, (res) => {
      let body = "";
      res.on("data", (chunk) => (body += chunk));
      res.on("end", () => {
        try {
          resolve(JSON.parse(body));
        } catch {
          resolve(undefined);
        }
      });
    });
    req.on("error", () => resolve(undefined));
    req.on("timeout", () => {
      req.destroy();
      resolve(undefined);
    });
  });
}

async function serverHealthy(): Promise<any | undefined> {
  return getJSON(`${baseUrl()}/health`);
}

// --------------------------------------------------------------------------- //
// server lifecycle
// --------------------------------------------------------------------------- //

async function startServer(silent = false): Promise<boolean> {
  const health = await serverHealthy();
  if (health) {
    if (!silent) {
      output.appendLine(`server already running at ${baseUrl()} (${health.roles} roles)`);
    }
    updateStatus("ready", health);
    return true;
  }
  if (serverProcess && !serverProcess.killed) {
    return true;
  }

  const bin = pythonPath();
  const isNexusBinary = bin.endsWith("nexus") || bin.endsWith("nexus.exe");
  const args = isNexusBinary
    ? ["serve", "--host", String(cfg().get("serverHost") || "127.0.0.1"), "--port", String(cfg().get("serverPort") || 8787)]
    : ["-m", "nexus", "serve", "--host", String(cfg().get("serverHost") || "127.0.0.1"), "--port", String(cfg().get("serverPort") || 8787)];
  if (cfg().get<boolean>("autoApprove")) {
    args.push("--yes");
  }

  output.appendLine(`starting: ${bin} ${args.join(" ")}  (cwd ${workspaceRoot()})`);
  try {
    serverProcess = spawn(bin, args, { cwd: workspaceRoot(), env: process.env });
  } catch (err: any) {
    vscode.window.showErrorMessage(
      `Nexus: could not start '${bin}'. Install with 'pip install -e .' or set nexus.pythonPath. (${err?.message ?? err})`
    );
    return false;
  }

  serverProcess.stdout?.on("data", (d) => output.append(String(d)));
  serverProcess.stderr?.on("data", (d) => output.append(String(d)));
  serverProcess.on("exit", (code) => {
    output.appendLine(`server exited with code ${code}`);
    serverProcess = undefined;
    updateStatus("stopped");
  });

  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 250));
    const up = await serverHealthy();
    if (up) {
      updateStatus("ready", up);
      if (!silent) {
        output.appendLine(`server ready at ${baseUrl()}`);
      }
      return true;
    }
  }
  vscode.window.showErrorMessage(`Nexus: server did not become healthy on ${baseUrl()} - see the Nexus output channel.`);
  updateStatus("error");
  return false;
}

function stopServer() {
  if (serverProcess && !serverProcess.killed) {
    output.appendLine("stopping server");
    serverProcess.kill("SIGTERM");
    setTimeout(() => {
      if (serverProcess && !serverProcess.killed) {
        serverProcess.kill("SIGKILL");
      }
    }, 3000);
  }
  serverProcess = undefined;
  updateStatus("stopped");
}

function updateStatus(state: string, health?: any) {
  const icons: Record<string, string> = {
    ready: "$(hubot)",
    starting: "$(sync~spin)",
    stopped: "$(circle-slash)",
    error: "$(error)",
    busy: "$(loading~spin)",
  };
  statusItem.text = `${icons[state] ?? "$(hubot)"} Nexus`;
  statusItem.tooltip = health
    ? `Nexus Agent\nworkspace: ${health.workspace}\nroles: ${health.roles}\nagents: ${(health.agents || []).join(", ")}`
    : `Nexus Agent: ${state}`;
  statusItem.command = "nexus.openPanel";
  statusItem.show();
}

// --------------------------------------------------------------------------- //
// webview
// --------------------------------------------------------------------------- //

class NexusViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = VIEW_ID;
  private view?: vscode.WebviewView;
  private pendingTask?: { task: string; autorun: boolean; readOnly: boolean; role?: string };

  constructor(private readonly context: vscode.ExtensionContext) {}

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    webviewView.webview.options = { enableScripts: true, localResourceRoots: [] };
    webviewView.webview.html = this.html();

    webviewView.webview.onDidReceiveMessage(async (msg) => {
      if (!msg || msg.source !== "nexus-panel") {
        return;
      }
      if (msg.type === "ready") {
        if (this.pendingTask) {
          const task = this.pendingTask;
          this.pendingTask = undefined;
          this.sendTask(task.task, task.autorun, task.readOnly, task.role);
        }
        return;
      }
      if (msg.type === "approval") {
        const choice = await vscode.window.showWarningMessage(
          `Nexus wants to run ${msg.tool} (${msg.risk})`,
          { modal: true, detail: String(msg.preview || "").slice(0, 1500) },
          "Approve",
          "Approve & remember",
          "Deny"
        );
        const approved = choice === "Approve" || choice === "Approve & remember";
        const scope = choice === "Approve & remember" ? "session" : "once";
        if (msg.taskId) {
          postJSON(`${baseUrl()}/api/approve/${msg.taskId}`, { approved, scope });
        }
        this.post({ type: "approve", approved, scope });
        updateStatus(approved ? "busy" : "ready");
        return;
      }
      if (msg.type === "role") {
        statusItem.text = `$(hubot) ${msg.role ?? "?"}`;
        statusItem.tooltip = `Nexus Agent\nrole: ${msg.role}\nagent: ${msg.agent}`;
        return;
      }
      if (msg.type === "done") {
        updateStatus("ready");
        const result = msg.result || {};
        if (result.ok === false) {
          vscode.window.showErrorMessage(`Nexus run failed: ${result.stopped_reason || result.error || "unknown"}`);
        } else if (result.summary) {
          vscode.window.setStatusBarMessage(`Nexus: ${result.summary}`.slice(0, 140), 8000);
        }
      }
    });

    webviewView.onDidDispose(() => (this.view = undefined));
  }

  async show() {
    if (cfg().get<boolean>("autoStartServer")) {
      await startServer(true);
    }
    await vscode.commands.executeCommand("workbench.view.extension.nexusAgent");
    await vscode.commands.executeCommand(`${VIEW_ID}.focus`);
    this.refresh();
  }

  refresh() {
    if (this.view) {
      this.view.webview.html = this.html();
    }
  }

  sendTask(task: string, autorun = true, readOnly = false, role?: string) {
    if (!this.view) {
      this.pendingTask = { task, autorun, readOnly, role };
      void this.show();
      return;
    }
    this.post({ type: "task", task, autorun, readOnly, role });
  }

  sendRole(role: string) {
    this.post({ type: "role", role });
  }

  private post(message: any) {
    this.view?.webview.postMessage({ source: "nexus-host", ...message });
  }

  private html(): string {
    const url = baseUrl();
    const nonce = getNonce();
    // The panel is served by the local nexus server; the iframe keeps the exact
    // same UI that `nexus panel` opens in a browser.
    return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none';
               style-src 'unsafe-inline';
               frame-src http: https:;
               img-src https: data:;
               script-src 'nonce-${nonce}';" />
<style>
  html, body { margin:0; padding:0; height:100%; background:#0e1116; }
  iframe { width:100%; height:100vh; border:0; display:block; }
  #offline { color:#8b98a9; font:12px ui-monospace,monospace; padding:16px; }
  #offline a { color:#4cc2ff; }
</style>
</head>
<body>
<iframe id="nexus" src="${url}/panel" title="Nexus Agent"></iframe>
<div id="offline" style="display:none">
  The Nexus server is not reachable at <a href="${url}/health">${url}</a>.<br /><br />
  Run <b>Nexus: Start local server</b> from the command palette, or start it yourself:
  <pre>nexus serve --host 127.0.0.1 --port ${new URL(url).port}</pre>
</div>
<script nonce="${nonce}">
  const vscodeApi = acquireVsCodeApi();
  const frame = document.getElementById('nexus');
  const offline = document.getElementById('offline');
  let alive = false;

  async function probe() {
    try {
      const res = await fetch('${url}/health', { method: 'GET' });
      alive = res.ok;
    } catch (e) { alive = false; }
    frame.style.display = alive ? 'block' : 'none';
    offline.style.display = alive ? 'none' : 'block';
    if (alive) { vscodeApi.postMessage({ source: 'nexus-panel', type: 'ready' }); }
  }

  window.addEventListener('message', (event) => {
    const data = event.data || {};
    if (data.source === 'nexus-panel') {
      // panel -> extension host (approvals, role changes, run finished)
      vscodeApi.postMessage(data);
      return;
    }
    if (data.source === 'nexus-host') {
      // extension host -> panel (inject a task, pin a role)
      frame.contentWindow && frame.contentWindow.postMessage(data, '*');
    }
  });

  probe();
  setInterval(probe, 5000);
</script>
</body>
</html>`;
  }
}

function getNonce(): string {
  const chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
  let out = "";
  for (let i = 0; i < 32; i++) {
    out += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return out;
}

function postJSON(url: string, payload: any) {
  const body = JSON.stringify(payload);
  const parsed = new URL(url);
  const req = http.request(
    {
      hostname: parsed.hostname,
      port: parsed.port,
      path: parsed.pathname,
      method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
      timeout: 5000,
    },
    (res) => res.resume()
  );
  req.on("error", (err) => output.appendLine(`POST ${url} failed: ${err.message}`));
  req.write(body);
  req.end();
}

// --------------------------------------------------------------------------- //
// activation
// --------------------------------------------------------------------------- //

export function activate(context: vscode.ExtensionContext) {
  output = vscode.window.createOutputChannel("Nexus");
  context.subscriptions.push(output);

  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  context.subscriptions.push(statusItem);
  updateStatus("stopped");

  provider = new NexusViewProvider(context);
  context.subscriptions.push(vscode.window.registerWebviewViewProvider(VIEW_ID, provider));

  context.subscriptions.push(
    vscode.commands.registerCommand("nexus.shareContextReference", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor || editor.document.uri.scheme !== "file") {
        vscode.window.showWarningMessage("Nexus: open a local workspace file first.");
        return;
      }
      const root = workspaceRoot();
      const relative = path.relative(root, editor.document.uri.fsPath);
      if (relative.startsWith("..") || path.isAbsolute(relative)) {
        vscode.window.showWarningMessage("Nexus: context must stay inside the selected workspace.");
        return;
      }
      // Explicit command, metadata only. Python rechecks opt-in, path/privacy,
      // TTL and policy; no document text/selection/clipboard is read here.
      const bin = pythonPath();
      const args = (bin.endsWith("nexus") || bin.endsWith("nexus.exe")) ? [] : ["-m", "nexus"];
      args.push("context", "-C", root, "signal", "vscode", JSON.stringify({path: relative, line: editor.selection.active.line + 1}));
      const child = spawn(bin, args, {cwd: root, env: process.env, stdio: "ignore"});
      child.on("error", () => vscode.window.showErrorMessage("Nexus: context bridge could not start."));
      child.on("exit", code => code === 0
        ? vscode.window.showInformationMessage("Nexus: short-lived editor reference shared locally.")
        : vscode.window.showWarningMessage("Nexus: context refused. Enable context/vscode explicitly; inspect privacy policy."));
    }),
    vscode.commands.registerCommand("nexus.startServer", async () => {
      output.show(true);
      const ok = await startServer();
      if (ok) {
        provider?.refresh();
        vscode.window.showInformationMessage(`Nexus server ready at ${baseUrl()}`);
      }
    }),
    vscode.commands.registerCommand("nexus.stopServer", () => {
      stopServer();
      vscode.window.showInformationMessage("Nexus server stopped");
    }),
    vscode.commands.registerCommand("nexus.openPanel", async () => {
      await provider?.show();
    }),
    vscode.commands.registerCommand("nexus.sendSelection", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showWarningMessage("Nexus: no active editor");
        return;
      }
      const selection = editor.selection;
      const text = selection.isEmpty ? editor.document.getText() : editor.document.getText(selection);
      const relative = path.relative(workspaceRoot(), editor.document.uri.fsPath);
      const question = await vscode.window.showInputBox({
        title: "Nexus: what should happen with this code?",
        value: `Refactor and improve ${relative} (lines ${selection.start.line + 1}-${selection.end.line + 1})`,
        prompt: "This becomes the task; the selection is attached as context.",
      });
      if (!question) {
        return;
      }
      const snippet = text.length > 6000 ? text.slice(0, 6000) + "\n…[truncated]" : text;
      provider?.sendTask(`${question}\n\nFile: ${relative}\n\`\`\`\n${snippet}\n\`\`\``, true);
    }),
    vscode.commands.registerCommand("nexus.askAboutFile", async () => {
      const editor = vscode.window.activeTextEditor;
      const relative = editor ? path.relative(workspaceRoot(), editor.document.uri.fsPath) : "this project";
      const question = await vscode.window.showInputBox({
        title: "Nexus: ask (read-only)",
        value: `Explain what ${relative} does and how it fits the rest of the repo`,
      });
      if (!question) {
        return;
      }
      provider?.sendTask(question, true, true);
    }),
    vscode.commands.registerCommand("nexus.runDoctor", async () => {
      output.show(true);
      await startServer(true);
      const health = await serverHealthy();
      if (!health) {
        output.appendLine("doctor: server not reachable");
        vscode.window.showErrorMessage("Nexus doctor: server not reachable - see output channel");
        return;
      }
      output.appendLine(JSON.stringify(health, null, 2));
      const roles = await getJSON(`${baseUrl()}/api/roles`);
      const agents = await getJSON(`${baseUrl()}/api/agents`);
      output.appendLine(`roles: ${Array.isArray(roles) ? roles.length : "?"}`);
      output.appendLine(`agents: ${JSON.stringify(agents, null, 2)}`);
      vscode.window.showInformationMessage(
        `Nexus doctor: ${health.roles} roles, agents ${(health.agents || []).join(", ")}`
      );
    }),
    vscode.commands.registerCommand("nexus.selectRole", async () => {
      await startServer(true);
      const roles = await getJSON(`${baseUrl()}/api/roles`);
      if (!Array.isArray(roles)) {
        vscode.window.showErrorMessage("Nexus: could not load roles from the server");
        return;
      }
      const picked = await vscode.window.showQuickPick(
        roles.map((r: any) => ({
          label: r.name,
          description: `${r.category} → ${r.agent}`,
          detail: r.title,
        })),
        { placeHolder: "Pin a Nexus role for the next task", matchOnDescription: true, matchOnDetail: true }
      );
      if (picked) {
        provider?.sendRole(picked.label);
      }
    })
  );

  void serverHealthy().then((health) => updateStatus(health ? "ready" : "stopped", health));
}

export function deactivate() {
  stopServer();
}
