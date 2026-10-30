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
      <div class="hardware-head"><div class="hardware-title"><span class="hardware-beacon" aria-hidden="true"></span><div><small>RESOURCE TELEMETRY</small><b>SERVER HARDWARE</b></div></div><button type="button" id="refreshHardware" aria-label="Refresh hardware measurements"><span aria-hidden="true">↻</span> Refresh</button></div>
      <div class="hardware-sample"><span id="hardwareState">READING</span><p id="hardwareStatus" role="status">Reading server measurements…</p></div>
      <div class="hardware-grid">
        <div class="hardware-card hardware-primary hardware-cpu"><div class="hardware-dial no-reading" id="hardwareCpuDial" aria-hidden="true"><span>CPU</span></div><div class="hardware-card-copy"><span class="hardware-label">PROCESSOR LOAD</span><b id="hardwareCpu">Unavailable</b><small>System utilization</small></div><meter id="hardwareCpuMeter" aria-label="CPU utilization" min="0" max="100" value="0" hidden></meter></div>
        <div class="hardware-card hardware-ram"><div class="hardware-card-top"><span class="hardware-label"><span aria-hidden="true">▥</span> MEMORY</span><span class="hardware-unit">RAM</span></div><b id="hardwareRam">Unavailable</b><div class="hardware-track" aria-hidden="true"><span id="hardwareRamFill"></span></div><meter id="hardwareRamMeter" aria-label="RAM utilization" min="0" max="100" value="0" hidden></meter><small>Used / installed</small></div>
        <div class="hardware-card hardware-mini"><span class="hardware-label"><span aria-hidden="true">▤</span> WORKSPACE DISK</span><b id="hardwareDisk">Unavailable</b><div class="hardware-track" aria-hidden="true"><span id="hardwareDiskFill"></span></div><small>Used / capacity</small></div>
        <div class="hardware-card hardware-mini hardware-process"><span class="hardware-label"><span aria-hidden="true">◈</span> NEXUS PROCESS</span><b id="hardwareProcess">Unavailable</b><small>Resident memory · RSS</small></div>
      </div>
      <div class="hardware-gpu-panel"><div class="hardware-card-top"><span class="hardware-label"><span aria-hidden="true">▦</span> GPU / VRAM</span><span id="hardwareGpuState">UNAVAILABLE</span></div><div id="hardwareGpu">GPU measurements unavailable</div></div>
      <details class="hardware-details"><summary>Measurement details <span>5s refresh</span></summary><small id="hardwareNote">Actual server environment, not this browser. Refreshes every 5 seconds while visible.</small></details>
    </section>`;
  const select = document.getElementById("localModel");
  const token = ''; // Shared HttpOnly GUI session; no URL credentials.
  async function get(path) {
    const response = await (window.nexusGui?.fetch || fetch)(path, {headers: token ? {"X-Nexus-Token": token} : {}});
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
  function utilization(id, value) {
    $(id).style.setProperty("--usage", finite(value) ? Math.max(0, Math.min(100, value)) + "%" : "0%");
    $(id).classList.toggle("no-reading", !finite(value));
  }
  function unavailable(message) {
    ["hardwareCpu", "hardwareRam", "hardwareDisk", "hardwareProcess"].forEach(id => $(id).textContent = "Unavailable");
    meter("hardwareCpuMeter", null); meter("hardwareRamMeter", null);
    $("hardwareGpu").textContent = "GPU measurements unavailable";
    $("hardwareStatus").textContent = message;
    $("serverMonitor").dataset.state = "unavailable"; $("hardwareState").textContent = "OFFLINE";
    $("hardwareGpuState").textContent = "UNAVAILABLE";
    ["hardwareCpuDial", "hardwareRamFill", "hardwareDiskFill"].forEach(id => utilization(id, null));
  }
  let measuring = false;
  async function refreshHardware() {
    if (measuring) return;
    measuring = true; $("refreshHardware").disabled = true;
    $("serverMonitor").dataset.state = "loading"; $("serverMonitor").setAttribute("aria-busy", "true");
    $("hardwareState").textContent = "READING";
    try {
      const observed = await get("/api/workbench/metrics"), data = observed.data;
      if (!data || typeof data !== "object") throw new Error("Server measurements unavailable");
      $("hardwareCpu").textContent = finite(data.cpu_percent) ? data.cpu_percent.toFixed(1) + "%" + (data.cpu_cores ? " · " + data.cpu_cores + " cores" : "") : "Unavailable";
      $("hardwareRam").textContent = data.ram ? bytes(data.ram.used) + " / " + bytes(data.ram.total) : "Unavailable";
      $("hardwareDisk").textContent = data.disk ? bytes(data.disk.used) + "\n/ " + bytes(data.disk.total) : "Unavailable";
      $("hardwareProcess").textContent = bytes(data.nexus_process_rss);
      meter("hardwareCpuMeter", data.cpu_percent); meter("hardwareRamMeter", data.ram?.percent);
      utilization("hardwareCpuDial", data.cpu_percent);
      const ratio = (used, total) => finite(used) && finite(total) && total > 0 ? used / total * 100 : null;
      utilization("hardwareRamFill", finite(data.ram?.percent) ? data.ram.percent : ratio(data.ram?.used, data.ram?.total));
      utilization("hardwareDiskFill", finite(data.disk?.percent) ? data.disk.percent : ratio(data.disk?.used, data.disk?.total));
      $("hardwareGpuState").textContent = data.gpu_devices?.length ? "NVIDIA" : "UNAVAILABLE";
      $("hardwareGpu").replaceChildren();
      for (const gpu of data.gpu_devices || []) {
        const row = document.createElement("p");
        row.textContent = gpu.name + " · GPU " + (finite(gpu.utilization_percent) ? gpu.utilization_percent + "%" : "unavailable") + " · VRAM " + (finite(gpu.memory_used_mib) ? gpu.memory_used_mib + " MiB" : "unavailable") + " / " + (finite(gpu.memory_total_mib) ? gpu.memory_total_mib + " MiB" : "unavailable");
        row.className = "hardware-gpu-device";
        if (finite(gpu.utilization_percent)) {
          const track = document.createElement("div"), fill = document.createElement("span");
          track.className = "hardware-track"; track.setAttribute("aria-hidden", "true");
          fill.style.setProperty("--usage", Math.max(0, Math.min(100, gpu.utilization_percent)) + "%");
          track.append(fill); row.append(track);
        }
        $("hardwareGpu").append(row);
      }
      if (!$("hardwareGpu").childElementCount) $("hardwareGpu").textContent = data.gpu_error || "GPU monitoring unavailable; no readable NVIDIA device";
      $("hardwareStatus").textContent = "Measured " + (finite(observed.checked_at) ? new Date(observed.checked_at * 1000).toLocaleTimeString() : "now") + " · " + (observed.source || "server") + (data.measurement_error ? " · partial measurements unavailable" : "");
      $("serverMonitor").dataset.state = data.measurement_error ? "partial" : "ready";
      $("hardwareState").textContent = data.measurement_error ? "PARTIAL" : "SAMPLED";
      $("hardwareNote").textContent = (data.note || "Actual server environment, not this browser.") + " Refreshes every 5 seconds while visible.";
    } catch (error) { unavailable("Measurements unavailable: " + error.message); }
    finally { measuring = false; $("refreshHardware").disabled = false; $("serverMonitor").setAttribute("aria-busy", "false"); }
  }
  $("refreshLocal").addEventListener("click", refreshLocal);
  $("refreshHardware").addEventListener("click", refreshHardware);
  setInterval(() => { if (!document.hidden) refreshHardware(); }, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshHardware(); });
  refreshLocal(); refreshHardware();
})();
