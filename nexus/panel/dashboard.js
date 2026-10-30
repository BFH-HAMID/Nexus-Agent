/* ──────────────────────────────────────────────────────────────────────────
   Nexus Agent — Control Deck (GUI)
   Live view of an agent run: routing, thinking, exploring, tool calls,
   approvals, tokens. Talks to the same stdlib server as the CLI:
     GET  /api/stats  /api/roles  /api/agents  /api/pipelines  /api/memory/<role>
     POST /api/task   GET /api/events/<id> (SSE)   POST /api/approve|cancel/<id>
   No build step, no dependencies, no network calls outside this origin.
   ────────────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const token = ''; // Shared HttpOnly GUI session; no URL credentials.
  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const MAX_STEPS = 400;
  const processCards = new Map();

  const state = {
    stats: null,
    agents: {},
    keys: {},
    pipelines: {},
    roles: [],
    taskId: null,
    es: null,
    running: false,
    startedAt: 0,
    phase: "idle",
    activeAgent: null,
    activeRole: null,
    selectedNode: null,
    positions: {},
    status: {},                  // agent -> idle|thinking|tool|error|done
    counters: { toolCalls: 0, danger: 0, denied: 0, errors: 0, events: 0, modelCalls: 0 },
    tokens: { in: 0, out: 0 },
    telemetry: { turns: 0, maxTurns: 24, tools: 0, route: "—" },
    spark: { events: [], tokens: [], latency: [] },
    filter: "all",
    pendingApproval: false,
    approvalId: null,
    lastEventId: 0,
    liveReady: false,
  };

  /* ── tiny utils ───────────────────────────────────────────────────── */
  const esc = (v) => String(v == null ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const clamp = (n, a, b) => Math.max(a, Math.min(b, n));
  const hhmmss = (d) => d.toTimeString().slice(0, 8);
  const stamp = () => new Date().toTimeString().slice(0, 8);
  const fmt = (n) => {
    n = Number(n) || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  };
  const trunc = (s, n) => (String(s || "").length > n ? String(s).slice(0, n - 1) + "…" : String(s || ""));
  const providerColor = (provider) => ({
    gemini: "var(--provider-gemini)",
    deepseek: "var(--provider-deepseek)",
    openrouter: "var(--provider-openrouter)",
  }[provider] || "var(--provider-local)");

  async function api(path, opts) {
    const options = Object.assign({ headers: {} }, opts || {});
    options.headers["Content-Type"] = "application/json";
    if (token) options.headers["X-Nexus-Token"] = token;
    try {
      const r = await (window.nexusGui?.fetch || fetch)(path, options);
      let body = {};
      try { body = await r.json(); } catch (_) { body = {}; }
      return { ok: r.ok, status: r.status, body: body };
    } catch (err) {
      return { ok: false, status: 0, body: { error: String(err) } };
    }
  }

  /* ── boot ─────────────────────────────────────────────────────────── */
  async function boot() {
    startClock();
    // No synthetic telemetry, geographic nodes or animated traffic.

    wireUi();
    const [stats, roles] = await Promise.all([api("/api/stats"), api("/api/roles")]);
    if (stats.ok) {
      applyStats(stats.body);
      setUplink("online", "ok");
    } else {
      setUplink("offline", "bad");
    }
    if (roles.ok && Array.isArray(roles.body)) {
      state.roles = roles.body;
      fillRoles();
    }
    refreshMemory();
    refreshBrain();
    await restoreRun();
    notifyHost({type:'ready'});
    setInterval(refreshStats, 15000);
    setInterval(refreshBrain, 30000);
    setInterval(tickMeters, 1000);
  }

  window.nexusRefreshConfiguration = async () => {
    const selected=$('roleSelect').value;
    const roles=await api('/api/roles');
    if(roles.ok&&Array.isArray(roles.body)){state.roles=roles.body;fillRoles();if(state.roles.some(r=>r.name===selected))$('roleSelect').value=selected;}
    await refreshStats();
  };

  async function refreshStats() {
    const { ok, body } = await api("/api/stats");
    if (!ok) { setUplink("offline", "bad"); return; }
    applyStats(body);
  }

  function setUplink(text, kind) {
    $("metaUplink").textContent = text;
    $("uplinkDot").className = "dot " + (kind || "");
    $("chipUplink").className = "chip" + (kind === "ok" ? " live" : kind === "bad" ? " dead" : "");
    $("sbTicker").textContent = "uplink " + text +
      (state.taskId ? " · task " + state.taskId : "") +
      " · SSE /api/events · POST /api/task";
  }

  function applyStats(s) {
    state.stats = s;
    const runtime = state.activeAgent && state.agents[state.activeAgent];
    state.agents = { ...(s.agents || {}) };
    if (runtime && !state.agents[state.activeAgent]) state.agents[state.activeAgent] = runtime;
    state.keys = s.keys || {};
    state.pipelines = s.pipelines || {};
    state.telemetry.maxTurns = (s.settings && s.settings.max_turns) || 24;
    $("metaWs").textContent = (s.workspace || "?").split("/").slice(-1)[0] || s.workspace;
    $("metaWs").title = s.workspace || "";
    $("taskWorkspace").textContent = window.nexusGitHubRepository?.()
      ? "Task target: GitHub " + window.nexusGitHubRepository() + " · new task branch after permission · no clone/local tests"
      : "Task workspace: " + (s.workspace || "unavailable") + " · select a GitHub repo for direct remote tasks.";
    $("taskWorkspace").title = $("taskWorkspace").textContent;
    $("metaRoles").textContent = s.roles;
    $("metaAgents").textContent = Object.keys(state.agents).length;
    state.liveReady = !s.demo;
    $("liveNotice").textContent = s.demo
      ? "Live dashboard disabled: restart Nexus without --demo and unset NEXUS_DEMO. No simulated task will run."
      : "Live server events only · model availability is not inferred from configuration.";
    $("runBtn").disabled = state.running || !state.liveReady;
    $("metaMode").textContent = s.demo ? "blocked: demo server" : "live";
    $("chipMode").className = "chip" + (s.demo ? "" : " live");
    $("metaRouting").textContent = s.dashboard_policy?.local_only ? "local-only" : window.nexusLocalModel?.() ? "selected local" : s.prefer_local ? "local-first" : "configured";
    $("metaUptime").textContent = "up " + Math.round(s.uptime_s || 0) + "s";
    $("sbVersion").textContent = "nexus " + (s.version || "?");
    $("globeNodes").textContent = Object.keys(s.agents || {}).length + " configured agents";
    $("orchCount").textContent = Object.keys(state.agents).length + " agents";
    renderOrch();
    renderKeys();
    renderPipelines();
    buildMap();
    if (s.prefer_local) $("chipMode").classList.add("live");
  }

  function startClock() {
    const tick = () => {
      const d = new Date();
      $("clock").textContent = hhmmss(d);
      $("clockDate").textContent = d.toDateString().toUpperCase().slice(0, 20);
    };
    tick();
    setInterval(tick, 1000);
  }

  /* ── roles / pipelines / keys ─────────────────────────────────────── */
  function fillRoles() {
    const groups = {};
    state.roles.forEach((r) => (groups[r.category || "general"] = groups[r.category || "general"] || []).push(r));
    const parts = ['<option value="">auto-route</option>'];
    Object.keys(groups).sort().forEach((cat) => {
      parts.push(`<optgroup label="${esc(cat)} (${groups[cat].length})">`);
      groups[cat].sort((a, b) => a.name.localeCompare(b.name)).forEach((r) => {
        parts.push(`<option value="${esc(r.name)}">${esc(r.name)} → ${esc(r.agent)}</option>`);
      });
      parts.push("</optgroup>");
    });
    $("roleSelect").innerHTML = parts.join("");
  }

  function renderPipelines() {
    const names = Object.keys(state.pipelines).sort();
    $("pipeCount").textContent = names.length + " defined";
    const selected = $("pipelineSelect").value;
    $("pipelineSelect").innerHTML = ['<option value="">none</option>']
      .concat(names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`)).join("");
    $("pipeList").innerHTML = names.length
      ? names.map((n) => {
        const p = state.pipelines[n] || {};
        return `<button type="button" class="pipe" data-pipe="${esc(n)}"><b>${esc(n)}</b>
          <span>${esc((p.steps || []).join(" → ") || "single step")}</span></button>`;
      }).join("")
      : '<div class="mem-body">no pipelines defined</div>';
    if (names.includes(selected)) $("pipelineSelect").value = selected;
    $("pipeList").querySelectorAll(".pipe").forEach(p => p.classList.toggle("on", p.dataset.pipe === selected));
  }

  function renderKeys() {
    const order = ["gemini", "deepseek", "openrouter"];
    $("keyList").innerHTML = order.map((f) => {
      const k = state.keys[f] || {};
      const ok = k.configured;
      return `<div class="key-row">
        <span class="${ok ? "ok" : "no"}">${ok ? "✓" : "·"}</span>
        <span class="kn">${esc(f)}</span>
        <span class="kv">${ok ? esc(k.masked) + "  ·  " + esc((k.source || "").split("/").slice(-1)[0]) : "not configured — nexus keys set " + esc(f)}</span>
      </div>`;
    }).join("");
  }

  /* ── orchestration list ───────────────────────────────────────────── */
  function renderOrch() {
    const names = Object.keys(state.agents);
    $("orchList").innerHTML = names.map((name) => {
      const a = state.agents[name] || {};
      const st = state.status[name] || "idle";
      const tag = a.optional ? "local" : (a.provider || "");
      return `<div class="orch-row${state.activeAgent === name ? " active" : ""}" data-agent="${esc(name)}">
        <span class="st ${esc(st)}"></span>
        <span class="orch-name"><b>${esc(name)}</b><span>${esc(a.model || "")}</span></span>
        <span class="orch-tag">${esc(tag)}</span>
      </div>`;
    }).join("");
    names.forEach((n) => {
      const row = $("orchList").querySelector(`[data-agent="${CSS.escape(n)}"]`);
      if (row) row.addEventListener("click", () => selectNode(n));
    });
  }

  function setAgentStatus(name, status) {
    if (!name) return;
    state.status[name] = status;
    const row = $("orchList").querySelector(`.orch-row[data-agent="${CSS.escape(name)}"] .st`);
    if (row) row.className = "st " + status;
    const node = $("map") && $("map").querySelector(`.node[data-agent="${CSS.escape(name)}"]`);
    if (node) {
      node.classList.remove("thinking", "tool", "error");
      if (status === "thinking" || status === "tool" || status === "error") node.classList.add(status);
    }
  }

  function setActiveAgent(name) {
    state.activeAgent = name;
    $("mapActive").textContent = name ? "active · " + name : "idle";
    $("mapActive").className = "pill" + (name ? " live" : "");
    $("sbAgent").textContent = "agent " + (name || "—");
    $("telAgent").textContent = "agent " + (name || "—");
    $("orchList").querySelectorAll(".orch-row").forEach((row) => {
      row.classList.toggle("active", row.dataset.agent === name);
    });
    paintMapActive(name);
  }

  /* ── cognition map ────────────────────────────────────────────────── */
  const MAP = { w: 300, h: 214, cx: 150, cy: 104, rx: 104, ry: 62 };

  function buildMap() {
    const svg = $("map");
    svg.setAttribute("viewBox", `0 0 ${MAP.w} ${MAP.h}`);
    const names = Object.keys(state.agents);
    if (!names.length) return;
    const pos = {};
    names.forEach((name, i) => {
      const a = -Math.PI / 2 + (i * 2 * Math.PI) / names.length;
      pos[name] = { x: MAP.cx + MAP.rx * Math.cos(a), y: MAP.cy + MAP.ry * Math.sin(a) };
    });
    state.positions = pos;

    const edges = [];
    names.forEach((name) => {
      const p = pos[name];
      edges.push(`<line class="edge" data-edge="${esc(name)}" x1="${MAP.cx}" y1="${MAP.cy}" x2="${p.x.toFixed(1)}" y2="${p.y.toFixed(1)}"/>`);
    });
    names.forEach((name) => {
      const fb = (state.agents[name] || {}).fallback;
      if (fb && pos[fb]) {
        const a = pos[name], b = pos[fb];
        const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        const cx = mx + (MAP.cx - mx) * 0.55, cy = my + (MAP.cy - my) * 0.55;
        edges.push(`<path class="edge fb" d="M${a.x.toFixed(1)},${a.y.toFixed(1)} Q${cx.toFixed(1)},${cy.toFixed(1)} ${b.x.toFixed(1)},${b.y.toFixed(1)}"/>`);
      }
    });

    const nodes = names.map((name) => {
      const a = state.agents[name] || {};
      const p = pos[name];
      const color = providerColor(a.provider);
      const initials = name.replace(/^(openrouter|ollama)-/, "").slice(0, 3).toUpperCase();
      const st = state.status[name] || "idle";
      return `<g class="node ${esc(st)}" tabindex="0" role="button" aria-label="${esc(name + ": " + (a.model || "") + ". Configured endpoint, not a health check.")}" data-agent="${esc(name)}" transform="translate(${p.x.toFixed(1)},${p.y.toFixed(1)})" style="--node:${color}">
        <title>${esc(name + " · " + (a.model || "") + (a.provider ? " · " + a.provider : ""))}</title>
        <circle class="ring" r="17" style="stroke:${color}"/>
        <path class="hex" d="M0,-14 L12,-7.5 L12,7.5 L0,14 L-12,7.5 L-12,-7.5 Z" style="stroke:${color};stroke-opacity:.55"/>
        <text class="lbl" y="3" text-anchor="middle" fill="${color}" style="font-size:8.5px;font-weight:700">${esc(initials)}</text>
      </g>`;
    });

    svg.innerHTML = `<defs>
        <radialGradient id="coreGrad">
          <stop offset="0%" stop-color="var(--accent)" stop-opacity=".22"/>
          <stop offset="70%" stop-color="var(--accent)" stop-opacity=".05"/>
          <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
        </radialGradient>
      </defs>
      <g>${edges.join("")}</g>
      <g>
        <circle class="core-glow" cx="${MAP.cx}" cy="${MAP.cy}" r="58"/>
        <circle cx="${MAP.cx}" cy="${MAP.cy}" r="26" fill="var(--surface-2)" stroke="var(--accent)" stroke-opacity=".7"/>
        <circle cx="${MAP.cx}" cy="${MAP.cy}" r="34" fill="none" stroke="var(--border-strong)" stroke-dasharray="2 6">
          ${REDUCED ? "" : '<animateTransform attributeName="transform" type="rotate" from="0 150 104" to="360 150 104" dur="40s" repeatCount="indefinite"/>'}
        </circle>
        <text x="${MAP.cx}" y="${MAP.cy + 3}" text-anchor="middle" style="font-size:8px;letter-spacing:.1em;font-weight:700;fill:var(--text)">NEXUS</text>
      </g>
      <g>${nodes.join("")}</g>
      <circle class="packet" id="pkt" r="2.6" cx="${MAP.cx}" cy="${MAP.cy}" opacity="0"/>
    `;

    svg.querySelectorAll(".node").forEach((node) => {
      node.addEventListener("pointerenter", () => { if (!state.selectedNode) previewNode(node.dataset.agent); });
      node.addEventListener("pointerleave", () => { if (!state.selectedNode) previewNode(null); });
      node.addEventListener("focus", () => { if (!state.selectedNode) previewNode(node.dataset.agent); });
      node.addEventListener("blur", () => { if (!state.selectedNode) previewNode(null); });
      node.addEventListener("click", (ev) => { ev.stopPropagation(); selectNode(node.dataset.agent); });
      node.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault(); selectNode(node.dataset.agent);
        }
      });
    });
    svg.addEventListener("click", () => clearNode());
    if (state.activeAgent) paintMapActive(state.activeAgent);
    if (state.selectedNode) selectNode(state.selectedNode, true);
  }

  function previewNode(name) {
    const nameEl = $("ndName"), providerEl = $("ndProvider"), bodyEl = $("ndBody");
    if (!nameEl || !providerEl || !bodyEl) return;
    const info = name ? state.agents[name] || {} : null;
    nameEl.textContent = name || "—";
    providerEl.textContent = name ? (info.provider || "agent") : "—";
    bodyEl.textContent = name
      ? (info.model || "model not reported") + " · configured entry, not a health check"
      : "hover a node or select one to inspect it";
  }

  function paintMapActive(name) {
    const svg = $("map");
    if (!svg) return;
    svg.querySelectorAll(".edge").forEach((e) => e.classList.remove("hot"));
    const edge = svg.querySelector(`.edge[data-edge="${CSS.escape(name || "")}"]`);
    if (edge) edge.classList.add("hot");
  }

  function selectNode(name, keep) {
    if (!name || !state.agents[name]) return;
    state.selectedNode = name;
    const svg = $("map");
    svg.querySelectorAll(".node").forEach((n) => n.classList.toggle("sel", n.dataset.agent === name));
    const a = state.agents[name] || {};
    $("ndName").textContent = name;
    $("ndProvider").textContent = a.optional ? "local" : (a.provider || "?");
    $("ndBody").innerHTML = [
      esc(a.model || ""),
      a.key_family ? (a.key_present ? "key ✓ " + esc(a.key_source || "set") : "key ✗ nexus keys set " + esc(a.key_family)) : "no key needed",
      "fallback → " + esc(a.fallback || "none"),
      a.base_url ? esc(a.base_url) : "",
      esc(trunc(a.description || "", 140)),
    ].filter(Boolean).map((x) => `<div>${x}</div>`).join("");
    if (!keep) setActiveAgentTabHint(name);
  }

  function setActiveAgentTabHint(name) {
    // selecting a node never changes where the run goes; it just inspects
    $("ndBody").title = "informational - routing is decided by the role";
  }

  function clearNode() {
    state.selectedNode = null;
    const svg = $("map");
    svg.querySelectorAll(".node").forEach((n) => n.classList.remove("sel"));
    $("ndName").textContent = "—";
    $("ndProvider").textContent = "—";
    $("ndBody").textContent = "select a node on the map";
  }


  /* ── phase banner ─────────────────────────────────────────────────── */
  function setPhase(phase, text, meta) {
    state.phase = phase;
    const chip = $("phaseChip");
    chip.textContent = phase;
    chip.className = "pill phase " + (phase === "thinking" ? "live" : phase === "tool" ? "tool" :
      phase === "waiting" ? "wait" : "");
    const banner = $("phaseBanner");
    banner.className = "phase-banner " + (phase === "thinking" ? "" : phase === "tool" ? "tool" :
      phase === "waiting" ? "wait" : "idle");
    if (text) $("phaseText").textContent = text;
    if (meta !== undefined) $("phaseMeta").textContent = meta || "";
    $("sbState").textContent = phase;
  }

  /* ── live process stream ──────────────────────────────────────────── */
  const KIND_FILTER = {
    think: "model", text: "model", agent: "model", route: "system", role: "system",
    status: "system", memory: "system", done: "system", error: "system",
    tool_call: "tools", tool_result: "tools", tool_output: "tools", process: "tools", file_change: "tools",
  };

  function addStep(kind, tag, icon, bodyHtml, meta) {
    const empty = $("streamEmpty");
    if (empty) empty.remove();
    const el = document.createElement("div");
    el.className = "step " + kind;
    el.dataset.kind = KIND_FILTER[kind.split(" ")[0]] || "system";
    el.innerHTML = `<span class="tag"><i>${icon}</i>${esc(tag)}</span>
      <span class="body">${bodyHtml}${meta ? `<span class="meta">${esc(meta)}</span>` : ""}</span>`;
    const stream = $("stream");
    const atBottom = stream.scrollTop + stream.clientHeight >= stream.scrollHeight - 80;
    stream.appendChild(el);
    while (stream.children.length > MAX_STEPS) stream.removeChild(stream.firstChild);
    if (atBottom) stream.scrollTop = stream.scrollHeight;
    applyFilter(el);
    return el;
  }

  function processCard(event) {
    const id = event.process_id || event.call_id;
    if (!id) return null;
    let card = processCards.get(id);
    if (!card || !card.isConnected) {
      card = addStep("process", "using shell", "⌘",
        `<div class="live-process-head"><b class="live-process-status">Running command</b><button type="button" class="btn live-process-stop">Stop command &amp; task</button></div><pre class="live-command"></pre><div class="live-process-meta"></div><details class="live-output" open><summary>stdout</summary><pre data-stream="stdout"></pre></details><details class="live-output" open><summary>stderr</summary><pre data-stream="stderr"></pre></details><pre class="live-process-notice"></pre>`, ts());
      card.dataset.processId = id;
      const taskId = state.taskId, button = card.querySelector(".live-process-stop");
      button.disabled = !state.running;
      button.addEventListener("click", async () => {
        if (taskId !== state.taskId || !state.running) { button.disabled = true; return; }
        button.disabled = true;
        const accepted = await cancel();
        if (!accepted && state.running && taskId === state.taskId) button.disabled = false;
        else button.textContent = "Stop requested";
      });
      processCards.set(id, card);
      while (processCards.size > 64) processCards.delete(processCards.keys().next().value);
    }
    return card;
  }

  function showProcess(event) {
    const card = processCard(event); if (!card) return;
    if (event.command) card.querySelector(".live-command").textContent = event.command;
    if (event.cwd) card.querySelector(".live-process-meta").textContent = "cwd: " + event.cwd + " · pid: " + event.pid;
    const active = event.status === "running";
    card.querySelector(".live-process-status").textContent = active ? "Running command" : (event.status || "Process update") + (event.exit_code != null ? " · exit " + event.exit_code : "");
    card.querySelector(".live-process-stop").disabled = !active || !state.running;
    if (!active) card.querySelector(".live-process-stop").hidden = true;
  }

  function showOutput(event) {
    const card = processCard(event); if (!card) return;
    const target = event.stream === "stderr" ? card.querySelector('[data-stream="stderr"]') : event.stream === "stdout" ? card.querySelector('[data-stream="stdout"]') : card.querySelector(".live-process-notice");
    const stream = $("stream"), follow = stream.scrollTop + stream.clientHeight >= stream.scrollHeight - 80;
    const text = String(event.text || "");
    const remaining = Math.max(0, 64_000 - target.textContent.length);
    target.textContent += text.slice(0, remaining);
    if (text.length > remaining) card.querySelector(".live-process-notice").textContent = "Live output display capped at 64,000 characters per stream.";
    if (follow) stream.scrollTop = stream.scrollHeight;
  }

  function applyFilter(el) {
    const f = state.filter;
    el.classList.toggle("hidden-filter", f !== "all" && el.dataset.kind !== f);
  }

  function refilterAll() {
    $("stream").querySelectorAll(".step").forEach(applyFilter);
  }

  const ts = () => stamp() + " · ";

  /* ── live event handling ──────────────────────────────────────────── */
  function onEvent(e) {
    if (e.event_id) {
      if (e.event_id <= state.lastEventId) return;
      state.lastEventId = e.event_id;
    }
    if (e.type === "ping") return;
    // Older/alternate emitters used "route" for the same router decision the
    // server now sends as "role"; normalise so one renderer handles it.
    if (e.type === "route") e = Object.assign({}, e, { type: "role" });
    state.counters.events++;
    const remoteTask=e.type==='github_task'?e.task:e.result?.github_task;
    if(remoteTask)state.githubRepo=remoteTask.repository;
    window.dispatchEvent(new CustomEvent("nexus:event", {detail:e}));
    if (e.agent && e.model && !state.agents[e.agent]) {
      state.agents[e.agent] = {model: e.model, provider: e.provider || "openai-compatible", tool_mode: e.tool_mode || "text"};
      renderOrch(); buildMap();
    }
    switch (e.type) {
      case "role": {
        state.activeRole = e.role;
        state.telemetry.role = e.role;
        state.telemetry.route = e.method || "?";
        $("routeRole").textContent = e.role || "—";
        $("routeMethod").textContent = e.method || "—";
        $("routeMethod").className = "pill" + (e.method === "llm" ? " live" : "");
        $("routeHits").textContent = (e.agent ? "→ " + e.agent : "") +
          (e.hits && e.hits.length ? "  ·  hits: " + e.hits.join(", ") : "") +
          (e.note ? "  ·  " + e.note : "");
        $("sbRole").textContent = "role " + (e.role || "—");
        $("telRole").textContent = e.role || "—";
        $("telRoute").textContent = "route " + (e.method || "—");
        addStep("role", "route", "⇄",
          `<b>${esc(e.role)}</b> → <b>${esc(e.agent || "?")}</b>  <span class="pill">${esc(e.method || "")}</span>`,
          ts() + (e.hits && e.hits.length ? "hits: " + e.hits.join(", ") : "router decision"));
        setPhase("routing", "routing · " + (e.role || ""), e.method || "");
        refreshMemory();
        break;
      }
      case "agent": {
        if (e.agent) {
          setActiveAgent(e.agent);
          if (e.error) {
            setAgentStatus(e.agent, "error");
            state.counters.errors++;
            addStep("error", "agent", "⚠", `<b>${esc(e.agent)}</b> failed: ${esc(e.error)}`);
            pushSec("bad", e.agent + ": " + trunc(e.error, 90));
          } else if (e.message) {
            addStep("agent", "agent", "⇢", esc(e.message));
          } else {
            setAgentStatus(e.agent, "idle");
            addStep("agent", "model", "→",
              `<b>${esc(e.agent)}</b> · ${esc(e.model || "")} <span class="pill">${esc(e.provider || "")}</span>` +
              (e.tool_mode && e.tool_mode !== "native" ? ` <span class="pill warn">tools: ${esc(e.tool_mode)}</span>` : ""));
          }
        }
        break;
      }
      case "think": {
        state.counters.modelCalls++;
        state.telemetry.turns = e.turn || state.telemetry.turns;
        if (e.agent) {
          if (state.activeAgent && state.activeAgent !== e.agent) setAgentStatus(state.activeAgent, "idle");
          setActiveAgent(e.agent);
          setAgentStatus(e.agent, "thinking");
        }
        addStep("think", "think", "◉",
          `thinking · <b>${esc(e.agent || "")}</b> <span class="pill">${esc(e.model || "")}</span>`,
          ts() + "turn " + (e.turn || "?") + "/" + (e.max_turns || "?") + " · waiting for model response");
        setPhase("thinking", "thinking · " + (e.agent || "") + " · " + (e.model || ""),
          "turn " + (e.turn || "?") + "/" + (e.max_turns || "?"));
        updateTelemetry();
        break;
      }
      case "usage": {
        state.tokens.in = e.input_tokens || state.tokens.in;
        state.tokens.out = e.output_tokens || state.tokens.out;
        $("telIn").textContent = fmt(state.tokens.in);
        $("telOut").textContent = fmt(state.tokens.out);
        $("monTokens").textContent = fmt(state.tokens.in) + " / " + fmt(state.tokens.out);
        pushSpark("tokens", (e.call_input_tokens || 0) + (e.call_output_tokens || 0));
        break;
      }
      case "tool_call": {
        state.counters.toolCalls++;
        state.telemetry.tools++;
        if (state.activeAgent) setAgentStatus(state.activeAgent, "tool");
        const risk = e.risk || "write";
        if (risk === "danger") {
          state.counters.danger++;
          pushSec("warn", "danger-risk tool: " + (e.tool || "?"));
        }
        $("secToolCalls").textContent = state.counters.toolCalls;
        $("secDanger").textContent = state.counters.danger;
        $("telTools").textContent = state.telemetry.tools;
        const preview = String(e.preview || "");
        const isCmd = /^\$|^git |^npm |^python|^pytest|^ls |^cat /m.test(preview.slice(0, 40));
        addStep("tool_call", e.tool || "tool", "✎",
          `<b>${esc(e.tool)}</b> <span class="pill ${risk === "danger" ? "danger" : risk === "read" ? "" : "warn"}">${esc(risk)}</span>` +
          (preview ? `<span class="cmd">${esc(trunc(preview, 900))}</span>` : "") +
          (e.args ? `<details class="live-output"><summary>Arguments</summary><pre>${esc(trunc(JSON.stringify(e.args, null, 2), 24_000))}</pre></details>` : ""),
          ts() + "executing" + (isCmd ? " shell command" : ""));
        setPhase("tool", "running " + (e.tool || "tool"), trunc(preview.replace(/\s+/g, " "), 70));
        break;
      }
      case "tool_result": {
        if (state.activeAgent) setAgentStatus(state.activeAgent, state.running ? "thinking" : "idle");
        const ms = Number.isFinite(e.duration_ms) ? e.duration_ms : null;
        if (ms !== null) pushSpark("latency", ms);
        const out = String(e.preview || "");
        addStep("tool_result" + (e.is_error ? " error" : ""), e.is_error ? "result ✗" : "result", e.is_error ? "✗" : "✓",
          `<span class="txt">${esc(trunc(out, 1200))}</span>` +
          (e.output ? `<details class="live-output"><summary>Retained tool output (${Number(e.chars) || String(e.output).length} characters)</summary><pre>${esc(trunc(e.output, 64_000))}</pre></details>` : ""),
          ts() + (ms ? ms + " ms" : ""));
        if (e.is_error) {
          state.counters.errors++;
          $("secErrors").textContent = state.counters.errors;
          pushSec("bad", "tool error: " + trunc(out, 80));
        }
        setPhase("thinking", "thinking · " + (state.activeAgent || ""), "");
        break;
      }
      case "process":
        showProcess(e);
        break;
      case "tool_output":
        showOutput(e);
        break;
      case "file_change": {
        const diff = String(e.diff || "").slice(0, 24_000).split("\n").map(line => `<span class="${line.startsWith("+") ? "diff-add" : line.startsWith("-") ? "diff-remove" : "diff-context"}">${esc(line)}</span>`).join("\n");
        addStep("file_change", "code edit", "✎", `<b>${esc(e.action || "updated")} ${esc(e.path)}</b><details class="live-output" open><summary>Applied diff</summary><pre class="live-diff">${diff}</pre></details>`, ts() + (e.note || "Applied file change"));
        break;
      }
      case "text_delta": {
        // live tokens: append into one growing "assistant" step
        const piece = String(e.text || "");
        if (!piece) break;
        if (!state.streamEl || !state.streamEl.isConnected) {
          state.streamEl = addStep("text", "assistant", "▸",
            `<span class="txt"></span>`, ts() + (e.role || "") + " · streaming");
          state.streamTxt = state.streamEl.querySelector(".txt");
          state.streamChars = 0;
        }
        state.streamChars += piece.length;
        if (state.streamChars < 8000) state.streamTxt.textContent += piece;
        const stream = $("stream");
        if (stream.scrollTop + stream.clientHeight >= stream.scrollHeight - 120) {
          stream.scrollTop = stream.scrollHeight;
        }
        break;
      }
      case "text": {
        const txt = String(e.text || "").trim();
        const existing = e.streamed && state.streamTxt?.isConnected ? state.streamTxt : null;
        if (existing) {
          if (txt) existing.textContent = trunc(txt, 4000);
          else state.streamEl.remove(); // Parsed tool-only output is not assistant prose.
        }
        state.streamEl = null;
        state.streamTxt = null;
        state.streamChars = 0;
        if (!txt || existing) break;
        addStep("text", "assistant", "▸", `<span class="txt">${esc(trunc(txt, 4000))}</span>`, ts() + (e.role || ""));
        break;
      }
      case "status": {
        const msg = String(e.message || "");
        if (!msg) break;
        addStep("status", "system", "·", esc(msg), ts());
        if (/falling back/i.test(msg)) {
          addStep("agent", "agent", "⇢", esc(msg));
        }
        break;
      }
      case "memory": {
        addStep("memory", "memory", "🧠", `saved memory · <b>${esc(e.role || "")}</b> · ${esc(e.path || "")}`);
        refreshMemory(e.role);
        break;
      }
      case "error": {
        state.counters.errors++;
        $("secErrors").textContent = state.counters.errors;
        addStep("error", "error", "✗", esc(e.message || JSON.stringify(e)));
        pushSec("bad", trunc(e.message || "error", 90));
        setPhase("error", "error · " + trunc(e.message || "", 80), "");
        break;
      }
      case "approval_request": {
        showApproval(e);
        $("apTool").textContent = e.tool || "?";
        $("apRisk").textContent = e.risk || "write";
        $("apRisk").className = "pill " + (e.risk === "danger" ? "danger" : "warn");
        $("apPreview").textContent = (e.preview || JSON.stringify(e.args || {}, null, 2)) +
          "\n\n" + (e.proposal?.available ? e.proposal.diff : e.proposal?.reason || "Diff unavailable for this tool");
        addStep("tool_call", "approval", "⚠",
          `<b>${esc(e.tool)}</b> is waiting for your approval <span class="pill warn">${esc(e.risk || "")}</span>`,
          ts());
        setPhase("waiting", "waiting for approval · " + (e.tool || ""), "approve or deny above the log");
        break;
      }
      case "approval_response": {
        hideApproval();
        if (e.approved === false) {
          state.counters.denied++;
          $("secDenied").textContent = state.counters.denied;
          pushSec("warn", "denied: " + (e.tool || "tool"));
          addStep("status", "approval", "✕", "denied " + esc(e.tool || "tool"));
        } else {
          addStep("status", "approval", "✓", "approved " + esc(e.tool || "tool") + " (" + esc(e.scope || "once") + ")");
        }
        break;
      }
      case "ping":
        break;
      case "done":
        finishRun(e);
        break;
      case "skill": {
        addStep("memory", "skill", "◆",
          `<b>${esc(e.name || e.skill || "skill")}</b> ${esc(e.action || "updated")}` +
          (e.summary ? `<span class="txt">${esc(trunc(e.summary, 800))}</span>` : ""),
          ts());
        refreshBrain();
        break;
      }
      default: {
        const label = e.type ? String(e.type).replace(/_/g, " ") : "event";
        const summary = e.message || e.text || e.status || e.detail || "";
        addStep("status", label, "·",
          esc(trunc(summary || label + " event received", 300)) +
          `<details class="live-output"><summary>Event details</summary><pre>${esc(trunc(JSON.stringify(e, null, 2), 4000))}</pre></details>`,
          ts());
      }
    }
    renderRunMetrics();
    updateSecurityLevel();
  }

  function pushSec(kind, text) {
    const log = $("secLog");
    const row = document.createElement("div");
    row.className = kind;
    row.textContent = stamp() + "  " + text;
    log.prepend(row);
    while (log.children.length > 8) log.removeChild(log.lastChild);
  }

  function updateSecurityLevel() {
    const c = state.counters;
    const risky = c.danger + c.denied + c.errors;
    const level = risky === 0 ? "no danger calls" : risky < 3 ? "watch" : "elevated";
    $("secLevel").textContent = level;
    $("secLevel").className = "micro" + (risky ? (risky < 3 ? " warn" : " bad") : "");
  }

  /* ── run lifecycle ────────────────────────────────────────────────── */
  async function run() {
    if (state.running || !state.liveReady) return;
    actionError("");
    const task = $("taskInput").value.trim();
    if (!task) { $("taskInput").focus(); return; }
    const body = {
      task,
      role: $("roleSelect").value || null,
      read_only: $("readOnly").checked,
      auto_yes: $("autoYes").checked,
      pipeline: $("pipelineSelect").value || null,
      ...(window.nexusGitHubRepository?.() ? {github_repo: window.nexusGitHubRepository()} : {}),
    };
    state.githubRepo = body.github_repo || null;
    state.taskId = null;
    state.lastEventId = 0;
    $("clearBtn").disabled = true;
    $("stream").innerHTML = "";
    $("secLog").innerHTML = "";
    resetRunState();
    state.running = true;
    state.startedAt = Date.now();
    $("runBtn").disabled = true;
    $("cancelBtn").disabled = false;
    $("taskId").textContent = "starting…";
    $("sbTask").textContent = body.pipeline ? body.pipeline + " :: " + trunc(task, 60) : trunc(task, 70);
    $("telState").textContent = "running";
    setPhase("routing", "routing · " + (body.role || "auto"), body.pipeline ? "pipeline " + body.pipeline : "");
    addStep("role", "task", "▶", esc(task), body.pipeline ? "pipeline " + body.pipeline : "one-shot run");
    startElapsed();

    const { ok, body: res } = await api("/api/task", { method: "POST", body: JSON.stringify({ ...body, ...window.nexusWorkbenchOptions?.(), local_model: window.nexusLocalModel?.() || null }) });
    if (!ok || !res.task_id) {
      state.running = false;
      $("runBtn").disabled = false;
      $("cancelBtn").disabled = true;
      $("clearBtn").disabled = false;
      actionError(res.error || "Could not start task");
      stopElapsed();
      addStep("error", "error", "✗", esc((res && res.error) || "could not start the task"));
      setPhase("error", "could not start the task", "");
      $("telState").textContent = "error";
      return;
    }
    state.taskId = res.task_id;
    try { sessionStorage.setItem("nexus.activeTask", state.taskId); } catch (_) {}
    $("taskId").textContent = "task " + state.taskId;
    stream(state.taskId);
  }

  function resetRunState() {
    window.dispatchEvent(new CustomEvent("nexus:run-reset"));
    processCards.clear();
    state.counters = { toolCalls: 0, danger: 0, denied: 0, errors: 0, events: 0, modelCalls: 0 };
    state.tokens = { in: 0, out: 0 };
    state.telemetry.turns = 0;
    state.telemetry.tools = 0;
    state.spark = { events: [], tokens: [], latency: [] };
    hideApproval();
    ["secToolCalls", "secDanger", "secDenied", "secErrors"].forEach((id) => ($(id).textContent = "0"));
    ["telTurns", "telTools", "telIn", "telOut"].forEach((id) => ($(id).textContent = "0"));
    $("telTime").textContent = "0.0s";
    $("monEvents").textContent = "0";
    $("monTokens").textContent = "0 / 0";
    $("monLatency").textContent = "—";
    $("turnBar").style.width = "0%";
    $("routeRole").textContent = "—";
    $("routeMethod").textContent = "—";
    $("routeHits").textContent = "waiting for a route decision…";
    Object.keys(state.agents).forEach((n) => setAgentStatus(n, "idle"));
    setActiveAgent(null);
    drawAllSparks();
    renderRunMetrics();
  }

  function stream(id) {
    renderRunMetrics();
    if (state.es) state.es.close();
    const url = "/api/events/" + id + (token ? "?token=" + encodeURIComponent(token) : "");
    const es = new EventSource(url);
    state.es = es;
    setUplink("streaming", "ok");
    es.onopen = () => setUplink("streaming", "ok");
    es.onmessage = (msg) => {
      if (state.es !== es || state.taskId !== id) return;
      try { onEvent(JSON.parse(msg.data)); } catch (err) { actionError("Event could not be displayed: " + err.message); }
    };
    es.onerror = () => {
      if (!es || es.readyState === EventSource.CLOSED) {
        setUplink("stream closed", "warn");
      } else {
        setUplink("reconnecting", "warn");
        actionError("Connection interrupted. Reconnecting; approval is not assumed successful.");
      }
    };
  }

  function finishRun(e) {
    stopElapsed();
    state.running = false;
    processCards.forEach(card => { card.querySelector(".live-process-stop").disabled = true; });
    if (state.es) { state.es.close(); state.es = null; }
    $("runBtn").disabled = !state.liveReady;
    $("cancelBtn").disabled = true;
    $("clearBtn").disabled = false;
    hideApproval();
    try { sessionStorage.removeItem("nexus.activeTask"); } catch (_) {}
    Object.keys(state.agents).forEach((n) => setAgentStatus(n, "idle"));
    const r = e.result || {};
    const blocked = r.execution?.state === "blocked";
    const ok = r.ok === true && e.status === "done" && !blocked;
    const outcome = e.status === "cancelled" ? "run cancelled" : blocked ? "run blocked" : ok ?
      (r.execution?.required ? "run finished · acceptance unverified" : "run complete") : "run failed";
    $("telState").textContent = e.status || (ok ? "done" : "error");
    $("sbState").textContent = e.status || "done";
    setPhase(e.status === "cancelled" ? "cancelled" : ok ? "done" : "error", outcome + " · " + (r.role || "") + " · " + (r.agent || ""),
      (r.duration_s != null ? r.duration_s + "s" : ""));
    setUplink("online", "ok");

    if (r.input_tokens != null) {
      state.tokens.in = r.input_tokens;
      state.tokens.out = r.output_tokens || 0;
      $("telIn").textContent = fmt(state.tokens.in);
      $("telOut").textContent = fmt(state.tokens.out);
    }
    if (r.turns != null) state.telemetry.turns = r.turns;
    if (r.tool_calls != null) state.telemetry.tools = r.tool_calls;
    updateTelemetry();
    $("telTime").textContent = (r.duration_s != null ? r.duration_s.toFixed(1) : "0.0") + "s";
    if (r.role) { $("telRole").textContent = r.role; $("sbRole").textContent = "role " + r.role; }
    if (r.agent) $("telAgent").textContent = "agent " + r.agent;

    const steps = Array.isArray(r.steps) ? r.steps : [];
    const summary = r.error || (blocked ? r.stopped_reason : "") || r.summary || r.text || "";
    addStep("done", "done", ok ? "✔" : "✗",
      `<b>${esc(outcome)}</b> · ${esc(summary ? trunc(summary, 1200) : "no summary")}`,
      ts() + [r.role, r.agent, r.turns != null ? r.turns + " turns" : "",
        r.tool_calls != null ? r.tool_calls + " tools" : "",
        r.duration_s != null ? r.duration_s + "s" : ""].filter(Boolean).join(" · "));
    if (steps.length) {
      addStep("status", "pipeline", "⇉",
        steps.map((s, i) => `${i + 1}. ${esc(s.role)} → ${esc(s.agent)} ${s.ok ? "✓" : "✗"}`).join("  ·  "),
        "pipeline steps");
    }
    if ((r.followups || []).length) {
      addStep("status", "next", "↳", (r.followups || []).map(esc).join("<br>"));
    }
    refreshStats();
  }

  async function cancel() {
    if (!state.taskId || !state.running) return false;
    const taskId = state.taskId;
    $("cancelBtn").disabled = true;
    const {ok, body} = await api("/api/cancel/" + taskId, {method: "POST", body: "{}"});
    if (!state.running || state.taskId !== taskId) return false;
    if (!ok) {
      actionError(body.error || "Stop failed. Please retry.");
      $("cancelBtn").disabled = false;
      return false;
    }
    hideApproval();
    actionError("");
    addStep("status", "stop process", "■", "Stop requested for this task; awaiting actual process termination events.", ts());
    setPhase("waiting", "stop requested · cancelling current operation", "no further tools will start; model/network requests may wait for timeout");
    return true;
  }

  function updateTelemetry() {
    $("telTurns").textContent = state.telemetry.turns || 0;
    const max = state.telemetry.maxTurns || 24;
    $("turnBar").style.width = clamp((state.telemetry.turns / max) * 100, 0, 100).toFixed(1) + "%";
  }

  let elapsedTimer = null;
  function startElapsed() {
    stopElapsed();
    elapsedTimer = setInterval(() => {
      const s = (Date.now() - state.startedAt) / 1000;
      $("telTime").textContent = s.toFixed(1) + "s";
    }, 200);
  }
  function stopElapsed() {
    if (elapsedTimer) clearInterval(elapsedTimer);
    elapsedTimer = null;
  }

  /* ── meters / sparklines ──────────────────────────────────────────── */
  function pushSpark(key, value) {
    const arr = state.spark[key];
    arr.push(Number(value) || 0);
    if (arr.length > 60) arr.shift();
    drawSpark(key);
  }

  function tickMeters() {
    const evPerSec = state.counters.events;
    state.counters.events = 0;
    if (state.running) pushSpark("events", evPerSec);
    $("monEvents").textContent = String(evPerSec);
    renderRunMetrics();
  }

  function renderRunMetrics() {
    $("statCalls").textContent = String(state.counters.modelCalls);
    $("statToolCalls").textContent = String(state.counters.toolCalls);
    const samples = state.spark.latency.slice(-3);
    const average = samples.length ? Math.round(samples.reduce((a, b) => a + b, 0) / samples.length) + "ms" : null;
    $("statLatency").textContent = average || "No results yet";
    $("statLatency").title = average ? "Mean duration of the last three completed tools" : "No tool durations recorded in this run yet";
    $("monLatency").textContent = average || "—";
    $("liveRunNote").textContent = state.taskId
      ? "Task " + state.taskId + (state.running ? " · live server events. Tool timing appears after completion." : " · retained run events. Open another run from History to view its metrics.")
      : "No Nexus task selected. Start a task here or open one from History. These metrics do not track this Arena chat.";
  }

  const themeVar = (name, fallback) => {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  };
  const sparkColors = () => ({
    events: { line: themeVar("--chart-1", "#2dd4a4"), fill: themeVar("--chart-1-fill", "rgba(45,212,164,.18)") },
    tokens: { line: themeVar("--chart-2", "#6aa8ff"), fill: themeVar("--chart-2-fill", "rgba(106,168,255,.18)") },
    latency: { line: themeVar("--chart-3", "#f4bb52"), fill: themeVar("--chart-3-fill", "rgba(244,187,82,.16)") },
  });

  function drawSpark(key) {
    const canvas = { events: $("sparkEvents"), tokens: $("sparkTokens"), latency: $("sparkLatency") }[key];
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 260;
    const h = 46;
    if (canvas.width !== Math.round(w * dpr)) canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h * dpr);
    const data = state.spark[key];
    const color = sparkColors()[key];
    // grid
    ctx.strokeStyle = themeVar("--chart-grid", "rgba(139,156,181,.14)");
    ctx.lineWidth = 1;
    for (let i = 1; i < 3; i++) {
      const y = (h / 3) * i;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    if (data.length < 2) return;
    const max = Math.max.apply(null, data.concat([1]));
    const stepX = w / Math.max(data.length - 1, 1);
    const pts = data.map((v, i) => [i * stepX, h - 3 - (v / max) * (h - 8)]);
    ctx.beginPath();
    ctx.moveTo(pts[0][0], h);
    pts.forEach((p) => ctx.lineTo(p[0], p[1]));
    ctx.lineTo(pts[pts.length - 1][0], h);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, color.fill);
    grad.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.strokeStyle = color.line;
    ctx.lineWidth = 2;
    ctx.stroke();
    const last = pts[pts.length - 1];
    ctx.beginPath();
    ctx.arc(last[0] - 1, last[1], 2, 0, Math.PI * 2);
    ctx.fillStyle = color.line;
    ctx.fill();
  }

  function drawAllSparks() { ["events", "tokens", "latency"].forEach(drawSpark); }
  window.addEventListener("nexus:theme", () => drawAllSparks());

  /* ── memory card ──────────────────────────────────────────────────── */
  async function refreshMemory(role) {
    const target = role || state.activeRole || $("roleSelect").value || "general";
    $("memRole").textContent = target;
    const { ok, body } = await api("/api/memory/" + encodeURIComponent(target));
    if (!ok || !body) { $("memBody").textContent = "Unavailable: " + (body.error || "memory request failed"); return; }
    $("memPath").textContent = body.path || "—";
    const text = String(body.text || "").trim();
    $("memBody").textContent = text || (body.enabled === false
      ? "memory is disabled (settings.memory_enabled=false)"
      : "no memory notes for this role yet — the agent writes here with the `remember` tool.");
  }

  /* ── learning brain (skills + memory + recall) ───────────────────── */
  async function refreshBrain() {
    const { ok, body } = await api("/api/brain");
    if (!ok || !body || !body.skills) { $("brainPill").textContent = "unavailable"; renderBrainList([], body.error || "Learning data unavailable"); return; }
    const skills = body.skills || {};
    const mem = body.memory || {};
    const runs = body.sessions || {};
    $("brainSkills").textContent = fmt(skills.total || 0);
    $("brainRuns").textContent = fmt(runs.sessions || 0);
    const entries = ((mem.memory || {}).entries || []).length + ((mem.user || {}).entries || []).length;
    $("brainMemo").textContent = fmt(entries);
    const learning = body.learning || {};
    $("brainPill").textContent = learning.enabled === false ? "disabled" : "enabled";
    $("brainMeta").textContent = (skills.categories || []).length + " categories · " +
      (runs.fts ? "FTS5 recall" : "recall") + (learning.agent ? " · configured: " + learning.agent : "");
    renderBrainList((skills.recent || []).slice(0, 8).map((s) => ({
      title: s.name, sub: s.description, tag: (s.uses ? s.uses + "×" : s.category || "skill"),
    })), "no skills yet — the agent writes them after non-trivial runs (or `nexus hermes install`)");
  }

  function renderBrainList(items, empty) {
    const host = $("brainList");
    if (!items.length) {
      host.innerHTML = '<div class="brain-empty">' + esc(empty) + "</div>";
      return;
    }
    host.innerHTML = items.map((it) =>
      '<div class="brain-item"><b>' + esc(it.title) + "</b> <em>" + esc(it.tag) + "</em><span>" +
      esc(trunc(it.sub || "", 96)) + "</span></div>").join("");
  }

  async function recall(query) {
    const path = "/api/recall" + (query ? "?q=" + encodeURIComponent(query) : "");
    const { ok, body } = await api(path);
    if (!ok || !Array.isArray(body)) { renderBrainList([], body.error || "Recall unavailable"); return; }
    renderBrainList(body.map((row) => ({
      title: (row.role || "run") + " · " + new Date((row.created || 0) * 1000).toISOString().slice(0, 10),
      sub: row.task || row.summary || "",
      tag: row.id,
    })), "no past runs match that yet");
  }

  // Preserve the existing VS Code host protocol in the canonical GUI.
  let trustedHostOrigin='';
  function notifyHost(data) {
    if(window.parent!==window&&(trustedHostOrigin||data.type==='ready'))window.parent.postMessage({source:'nexus-panel',...data},trustedHostOrigin||'*');
  }
  window.addEventListener('message',e=>{
    if(window.parent===window||e.source!==window.parent)return;
    let origin;try{origin=new URL(e.origin);}catch{return;}
    if(origin.protocol!=='vscode-webview:'&&!(origin.protocol==='https:'&&/^[a-z0-9-]+\.vscode-cdn\.net$/.test(origin.hostname)))return;
    const data=e.data||{};if(data.source!=='nexus-host')return;
    trustedHostOrigin=e.origin;
    if(data.type==='role')$('roleSelect').value=data.role||'';
    if(data.type==='task'){
      window.nexusGui?.show('run');$('taskInput').value=String(data.task||'');
      if(data.role)$('roleSelect').value=data.role;
      $('readOnly').checked=!!data.readOnly;
      if(data.autorun)$('runBtn').click();
    }
    if(data.type==='approve')decide(!!data.approved,data.scope==='session'?'session':'once');
  });
  window.addEventListener('nexus:event',({detail:e})=>{
    if(e.type==='approval_request')notifyHost({type:'approval',tool:e.tool,risk:e.risk,preview:e.preview});
    if(e.type==='done')notifyHost({type:'done',result:e.result});
  });

  /* ── UI wiring ────────────────────────────────────────────────────── */
  function wireUi() {
    $("runBtn").addEventListener("click", run);
    $("cancelBtn").addEventListener("click", cancel);
    $("clearBtn").addEventListener("click", () => {
      if (state.running) return;
      $("stream").innerHTML = '<div class="stream-empty" id="streamEmpty"><span class="big">◈</span><p>Cleared. Describe a task below and press run.</p></div>';
    });
    $("taskInput").addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); run(); }
    });
    $("apYes").addEventListener("click", () => decide(true, "once"));
    $("apAlways").addEventListener("click", () => decide(true, "session"));
    $("apNo").addEventListener("click", () => decide(false, "once"));
    $("pipelineSelect").addEventListener("change", () => {
      $("pipeList").querySelectorAll(".pipe").forEach(p => p.classList.toggle("on", p.dataset.pipe === $("pipelineSelect").value));
    });
    $("roleSelect").addEventListener("change", () => refreshMemory($("roleSelect").value || null));
    $("brainGo").addEventListener("click", () => recall($("brainQuery").value.trim()));
    $("brainQuery").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); recall($("brainQuery").value.trim()); }
    });
    $("filters").addEventListener("click", (e) => {
      const btn = e.target.closest(".filter");
      if (!btn) return;
      state.filter = btn.dataset.f;
      $("filters").querySelectorAll(".filter").forEach((b) => b.classList.toggle("on", b === btn));
      refilterAll();
    });
    $("pipeList").addEventListener("click", (e) => {
      const row = e.target.closest(".pipe");
      if (!row) return;
      const name = row.dataset.pipe;
      $("pipelineSelect").value = $("pipelineSelect").value === name ? "" : name;
      $("pipeList").querySelectorAll(".pipe").forEach((p) => p.classList.toggle("on", p === row && $("pipelineSelect").value === name));
    });
    window.addEventListener("resize", () => { drawAllSparks(); });
  }

  function actionError(message) {
    $("actionError").textContent = message;
    $("actionError").classList.toggle("hidden", !message);
  }

  function showApproval(pending) {
    state.pendingApproval = true;
    state.approvalId = pending.approval_id || null;
    $("approvalCard").classList.remove("hidden");
    ["apYes", "apAlways", "apNo"].forEach(id => { $(id).disabled = false; });
    $("approvalCard").focus({preventScroll: true});
  }

  function hideApproval() {
    state.pendingApproval = false;
    state.approvalId = null;
    $("approvalCard").classList.add("hidden");
  }

  async function decide(approved, scope) {
    if (!state.taskId || !state.pendingApproval) return;
    const submittedId = state.approvalId;
    ["apYes", "apAlways", "apNo"].forEach(id => { $(id).disabled = true; });
    const {ok, body} = await api("/api/approve/" + state.taskId, {
      method: "POST", body: JSON.stringify({approved, scope, approval_id: submittedId}),
    });
    if (!state.running || state.approvalId !== submittedId) return;
    if (!ok) {
      actionError(body.error || "Approval was not received. Please retry.");
      ["apYes", "apAlways", "apNo"].forEach(id => { $(id).disabled = false; });
      return;
    }
    hideApproval();
    actionError("");
    setPhase("thinking", approved ? "approval received · resuming" : "denial received", "");
  }

  window.nexusSelectedTask = () => ({id:state.taskId, github_repo:state.githubRepo || null});
  window.nexusAttachTask = async id => {
    const {ok, body} = await api("/api/task/" + encodeURIComponent(id));
    if (!ok) throw new Error(body.error || "Run is no longer live; inspect durable history instead");
    if (state.es) state.es.close();
    resetRunState(); state.lastEventId = 0; state.taskId = id; state.githubRepo = body.github_repo || body.options?.github_repo || body.result?.github_task?.repository || null; state.running = true;
    state.startedAt = body.created * 1000;
    $("stream").innerHTML = "";
    $("runBtn").disabled = true; $("clearBtn").disabled = true; $("cancelBtn").disabled = false;
    $("taskId").textContent = "task " + id;
    try { sessionStorage.setItem("nexus.activeTask", id); } catch (_) {}
    startElapsed(); stream(id);
  };

  async function restoreRun() {
    let id;
    try { id = sessionStorage.getItem("nexus.activeTask"); } catch (_) { return; }
    if (!id || !state.liveReady) return;
    const {ok, body} = await api("/api/task/" + encodeURIComponent(id));
    if (!ok) { actionError(body.error || "Previous task unavailable"); return; }
    state.taskId = id;
    state.githubRepo = body.github_repo || body.options?.github_repo || body.result?.github_task?.repository || null;
    state.running = true;
    state.startedAt = body.created * 1000;
    $("runBtn").disabled = true;
    $("clearBtn").disabled = true;
    $("cancelBtn").disabled = false;
    $("taskId").textContent = "task " + id;
    $("taskInput").value = body.task || "";
    startElapsed();
    stream(id); // Replay actual events, including any outstanding approval.
  }

  // Pointer orbit gives the SVG network depth without a GPU-heavy 3D library.
  // Keep model names real: the map uses configured agents from /api/stats.
  (() => {
    const svg = $("map");
    let dragging = false, startX = 0, startY = 0, yaw = 0, pitch = 0;
    let baseYaw = 0, basePitch = 0;
    svg.addEventListener("pointerdown", (event) => {
      if (REDUCED || event.target.closest(".node")) return;
      dragging = true; startX = event.clientX; startY = event.clientY;
      baseYaw = yaw; basePitch = pitch;
      svg.setPointerCapture(event.pointerId);
    });
    svg.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      yaw = Math.max(-24, Math.min(24, baseYaw + (event.clientX - startX) * .12));
      pitch = Math.max(-18, Math.min(18, basePitch - (event.clientY - startY) * .12));
      svg.style.transform = `rotateX(${pitch}deg) rotateY(${yaw}deg)`;
    });
    const end = () => { dragging = false; };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
    $("resetMap").addEventListener("click", () => {
      yaw = pitch = 0; svg.style.transform = "";
    });
  })();

  // Theme backgrounds are CSS only; no fabricated traffic or geographic nodes.

  window.addEventListener("error", (e) => {
    const stream = $("stream");
    if (stream && e && e.message) {
      addStep("error", "ui", "✗", esc(String(e.message)));
    }
  });

  boot();
})();
