/* Shared setup controls for the browser dashboard and VS Code webview. */
(() => {
  "use strict";
  const root = document.getElementById("integrations");
  if (!root) return;
  root.innerHTML = `
    <details open><summary>Connections &amp; local models</summary>
      <p>Choose a downloaded model, or keep automatic hosted routing.</p>
      <label for="localModel">Ollama model</label>
      <select id="localModel"><option value="">Auto · configured agents</option></select>
      <button type="button" id="refreshLocal">Refresh models</button>
      <p id="ollamaStatus" role="status">Checking Ollama…</p>
      <small>Selected local models use text-mode tools, including models without native tool support. No cloud fallback. Ollama must run on the same machine as the Nexus server (not necessarily your browser).</small>
      <hr><b>GitHub account</b>
      <button type="button" id="refreshGithub">Check connection</button>
      <p id="githubStatus" role="status">Checking GitHub CLI…</p>
      <p>Connect in a terminal on the machine running Nexus:</p>
      <pre>gh auth login --hostname github.com --web\ngh auth setup-git\ngh auth status</pre>
      <small>Follow the browser sign-in opened by GitHub CLI, then check connection. No token needs to be pasted here. Git/commit/PR tools use this account; writes still require approval.</small>
      <hr><b>VS Code sidebar</b>
      <p>From the repository:</p>
      <pre>cd vscode-extension\nnpm install\nnpm run package</pre>
      <p>In VS Code: Extensions → … → Install from VSIX → choose the generated .vsix. Open the Nexus activity-bar icon.</p>
      <small>Included: send selection, ask about current file, role/pipeline picker, live tool log and approve/deny. A source checkout alone does not install the extension.</small>
    </details>`;
  const select = document.getElementById("localModel");
  const token = new URLSearchParams(location.search).get("token") || "";
  async function get(path) {
    const response = await fetch(path, {headers: token ? {"X-Nexus-Token": token} : {}});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }
  window.nexusLocalModel = () => select.value;
  select.addEventListener("change", () => {
    const routing = document.getElementById("metaRouting");
    if (routing) routing.textContent = select.value ? "selected local" : "configured";
  });
  async function refreshLocal() {
    const status = document.getElementById("ollamaStatus");
    const previous = select.value;
    status.textContent = "Checking Ollama…";
    try {
      const data = await get("/api/ollama");
      select.replaceChildren(new Option("Auto · configured agents", ""));
      for (const model of data.models || []) select.add(new Option(model, model));
      if (previous && ![...select.options].some(o => o.value === previous)) {
        select.add(new Option(previous + " (unavailable — refresh or change model)", previous));
      }
      select.value = previous;
      status.textContent = data.connected
        ? `${data.models.length} downloaded model(s) · ${data.url}` + (data.models.length ? "" : " · Download a model with ollama pull <model>.")
        : data.error;
    } catch (error) { status.textContent = error.message; }
  }
  async function refreshGithub() {
    const status = document.getElementById("githubStatus");
    status.textContent = "Checking GitHub…";
    try {
      const data = await get("/api/github");
      status.textContent = data.connected ? `Connected as ${data.login}` : data.message;
    } catch (error) { status.textContent = error.message; }
  }
  document.getElementById("refreshLocal").addEventListener("click", refreshLocal);
  document.getElementById("refreshGithub").addEventListener("click", refreshGithub);
  refreshLocal();
  refreshGithub();
})();
