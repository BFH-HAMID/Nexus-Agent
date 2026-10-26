/* Shared setup controls for the browser dashboard and VS Code webview. */
(() => {
  "use strict";
  const root = document.getElementById("integrations");
  if (!root) return;
  root.innerHTML = `
    <details open><summary>Local models</summary>
      <p>Choose a downloaded model, or keep automatic hosted routing.</p>
      <label for="localModel">Ollama model</label>
      <select id="localModel"><option value="">Auto · configured agents</option></select>
      <button type="button" id="refreshLocal">Refresh models</button>
      <p id="ollamaStatus" role="status">Checking Ollama…</p>
      <small>Selected local models use text-mode tools, including models without native tool support. No cloud fallback. Ollama must run on the same machine as the Nexus server (not necessarily your browser).</small>
    </details>
    <section id="serverMonitor" aria-label="Server hardware monitoring">
      <hr><div class="hardware-head"><b>SERVER HARDWARE</b><button type="button" id="refreshHardware">Refresh</button></div>
      <p id="hardwareStatus" role="status">Reading server measurements…</p>
      <div class="hardware-grid">
        <label>CPU <b id="hardwareCpu">Unavailable</b><meter id="hardwareCpuMeter" min="0" max="100" value="0" hidden></meter></label>
        <label>RAM <b id="hardwareRam">Unavailable</b><meter id="hardwareRamMeter" min="0" max="100" value="0" hidden></meter></label>
        <label>Workspace disk <b id="hardwareDisk">Unavailable</b></label>
        <label>Nexus process RAM <b id="hardwareProcess">Unavailable</b></label>
      </div>
      <div id="hardwareGpu">GPU measurements unavailable</div>
      <small id="hardwareNote">Actual server environment, not this browser. Refreshes every 5 seconds while visible.</small>
    </section>`;
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
  const $ = id => document.getElementById(id);
  const finite = v => typeof v === "number" && Number.isFinite(v);
  function bytes(v) { return finite(v) ? (v / 1073741824).toFixed(2) + " GiB" : "Unavailable"; }
  function meter(id, value) { $(id).hidden = !finite(value); $(id).value = finite(value) ? value : 0; }
  function unavailable(message) {
    ["hardwareCpu", "hardwareRam", "hardwareDisk", "hardwareProcess"].forEach(id => $(id).textContent = "Unavailable");
    meter("hardwareCpuMeter", null); meter("hardwareRamMeter", null);
    $("hardwareGpu").textContent = "GPU measurements unavailable";
    $("hardwareStatus").textContent = message;
  }
  let measuring = false;
  async function refreshHardware() {
    if (measuring) return;
    measuring = true; $("refreshHardware").disabled = true;
    try {
      const observed = await get("/api/workbench/metrics"), data = observed.data;
      if (!data || typeof data !== "object") throw new Error("Server measurements unavailable");
      $("hardwareCpu").textContent = finite(data.cpu_percent) ? data.cpu_percent.toFixed(1) + "%" + (data.cpu_cores ? " · " + data.cpu_cores + " cores" : "") : "Unavailable";
      $("hardwareRam").textContent = data.ram ? bytes(data.ram.used) + " / " + bytes(data.ram.total) : "Unavailable";
      $("hardwareDisk").textContent = data.disk ? bytes(data.disk.used) + " / " + bytes(data.disk.total) : "Unavailable";
      $("hardwareProcess").textContent = bytes(data.nexus_process_rss);
      meter("hardwareCpuMeter", data.cpu_percent); meter("hardwareRamMeter", data.ram?.percent);
      $("hardwareGpu").replaceChildren();
      for (const gpu of data.gpu_devices || []) {
        const row = document.createElement("p");
        row.textContent = gpu.name + " · GPU " + (finite(gpu.utilization_percent) ? gpu.utilization_percent + "%" : "unavailable") + " · VRAM " + (finite(gpu.memory_used_mib) ? gpu.memory_used_mib + " MiB" : "unavailable") + " / " + (finite(gpu.memory_total_mib) ? gpu.memory_total_mib + " MiB" : "unavailable");
        $("hardwareGpu").append(row);
      }
      if (!$("hardwareGpu").childElementCount) $("hardwareGpu").textContent = data.gpu_error || "GPU monitoring unavailable; no readable NVIDIA device";
      $("hardwareStatus").textContent = "Measured " + (finite(observed.checked_at) ? new Date(observed.checked_at * 1000).toLocaleTimeString() : "now") + " · " + (observed.source || "server") + (data.measurement_error ? " · partial measurements unavailable" : "");
      $("hardwareNote").textContent = (data.note || "Actual server environment, not this browser.") + " Refreshes every 5 seconds while visible.";
    } catch (error) { unavailable("Measurements unavailable: " + error.message); }
    finally { measuring = false; $("refreshHardware").disabled = false; }
  }
  $("refreshLocal").addEventListener("click", refreshLocal);
  $("refreshHardware").addEventListener("click", refreshHardware);
  setInterval(() => { if (!document.hidden) refreshHardware(); }, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshHardware(); });
  refreshLocal(); refreshHardware();
})();
