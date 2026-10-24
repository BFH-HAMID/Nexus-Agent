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
  const token = new URLSearchParams(location.search).get("token") || "";
  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const MAX_STEPS = 400;

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
    lastToolAt: 0,
    filter: "all",
    pendingApproval: false,
  };

  /* ── tiny utils ───────────────────────────────────────────────────── */
  const esc = (v) => String(v == null ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const clamp = (n, a, b) => Math.max(a, Math.min(b, n));
  const now = () => performance.now();
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
    gemini: "var(--cyan)",
    deepseek: "var(--neon)",
    openrouter: "var(--amber)",
  }[provider] || "var(--violet)");

  async function api(path, opts) {
    const options = Object.assign({ headers: {} }, opts || {});
    options.headers["Content-Type"] = "application/json";
    if (token) options.headers["X-Nexus-Token"] = token;
    try {
      const r = await fetch(path, options);
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
    startEffects();
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
    setInterval(refreshStats, 15000);
    setInterval(tickMeters, 1000);
    requestAnimationFrame(tickPackets);
  }

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
    state.agents = s.agents || {};
    state.keys = s.keys || {};
    state.pipelines = s.pipelines || {};
    state.telemetry.maxTurns = (s.settings && s.settings.max_turns) || 24;
    $("metaWs").textContent = (s.workspace || "?").split("/").slice(-1)[0] || s.workspace;
    $("metaWs").title = s.workspace || "";
    $("metaRoles").textContent = s.roles;
    $("metaAgents").textContent = Object.keys(state.agents).length;
    $("metaMode").textContent = s.demo ? "scripted" : "live";
    $("chipMode").className = "chip" + (s.demo ? "" : " live");
    $("metaRouting").textContent = s.prefer_local ? "local-first" : "hosted";
    $("metaUptime").textContent = "up " + Math.round(s.uptime_s || 0) + "s";
    $("sbVersion").textContent = "nexus " + (s.version || "?");
    $("globeNodes").textContent = Object.keys(state.agents).length + " nodes";
    $("statRegions").textContent = String(GLOBE_NODES.length);
    $("orchCount").textContent = Object.keys(state.agents).length + " agents";
    renderOrch();
    renderKeys();
    renderPipelines();
    buildMap();
    if (s.prefer_local) $("chipLocal").classList.add("live");
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
    $("pipelineSelect").innerHTML = ['<option value="">none</option>']
      .concat(names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`)).join("");
    $("pipeList").innerHTML = names.length
      ? names.map((n) => {
        const p = state.pipelines[n] || {};
        return `<div class="pipe" data-pipe="${esc(n)}"><b>${esc(n)}</b>
          <span>${esc((p.steps || []).join(" → ") || "single step")}</span></div>`;
      }).join("")
      : '<div class="mem-body">no pipelines defined</div>';
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
  const MAP = { w: 920, h: 440, cx: 460, cy: 212, rx: 352, ry: 152 };

  function buildMap() {
    const svg = $("map");
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
      return `<g class="node ${esc(st)}" data-agent="${esc(name)}" transform="translate(${p.x.toFixed(1)},${p.y.toFixed(1)})" style="--node:${color}">
        <circle class="ring" r="21" style="stroke:${color}"/>
        <path class="hex" d="M0,-18 L16,-9 L16,9 L0,18 L-16,9 L-16,-9 Z" style="stroke:${color};stroke-opacity:.55"/>
        <text class="lbl" y="3" text-anchor="middle" fill="${color}" style="font-size:9px;opacity:.95">${esc(initials)}</text>
        <text class="sub" y="32" text-anchor="middle" style="fill:#a8d9cd">${esc(name)}</text>
        <text class="sub" y="42" text-anchor="middle" style="fill:#47706a;font-size:7px">${esc(trunc(a.model || "", 26))}</text>
      </g>`;
    });

    svg.innerHTML = `<defs>
        <radialGradient id="coreGrad">
          <stop offset="0%" stop-color="#00ffa3" stop-opacity=".55"/>
          <stop offset="70%" stop-color="#00ffa3" stop-opacity=".08"/>
          <stop offset="100%" stop-color="#00ffa3" stop-opacity="0"/>
        </radialGradient>
      </defs>
      <g>${edges.join("")}</g>
      <g>
        <circle class="core-glow" cx="${MAP.cx}" cy="${MAP.cy}" r="86"/>
        <circle cx="${MAP.cx}" cy="${MAP.cy}" r="46" fill="rgba(3,14,12,.92)" stroke="var(--neon)" stroke-opacity=".55"/>
        <circle cx="${MAP.cx}" cy="${MAP.cy}" r="56" fill="none" stroke="var(--cyan)" stroke-opacity=".22" stroke-dasharray="2 6">
          ${REDUCED ? "" : '<animateTransform attributeName="transform" type="rotate" from="0 460 212" to="360 460 212" dur="26s" repeatCount="indefinite"/>'}
        </circle>
        <text x="${MAP.cx}" y="${MAP.cy - 3}" text-anchor="middle" style="font-size:10px;letter-spacing:.22em;fill:#d9fff2">NEXUS</text>
        <text x="${MAP.cx}" y="${MAP.cy + 12}" text-anchor="middle" style="font-size:7px;letter-spacing:.3em;fill:#47706a">CORE</text>
      </g>
      <g>${nodes.join("")}</g>
      <circle class="packet" id="pkt" r="3.4" cx="${MAP.cx}" cy="${MAP.cy}" opacity="0"/>
    `;

    svg.querySelectorAll(".node").forEach((node) => {
      node.addEventListener("click", (ev) => { ev.stopPropagation(); selectNode(node.dataset.agent); });
    });
    svg.addEventListener("click", () => clearNode());
    if (state.activeAgent) paintMapActive(state.activeAgent);
    if (state.selectedNode) selectNode(state.selectedNode, true);
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

  function tickPackets() {
    const pkt = $("pkt");
    if (pkt && state.running && state.activeAgent && state.positions[state.activeAgent]) {
      const p = state.positions[state.activeAgent];
      const cycle = 1500;
      const t = (now() % cycle) / cycle;
      const back = state.phase === "tool";
      const e = back ? 1 - t : t;
      pkt.setAttribute("cx", (MAP.cx + (p.x - MAP.cx) * e).toFixed(1));
      pkt.setAttribute("cy", (MAP.cy + (p.y - MAP.cy) * e).toFixed(1));
      pkt.setAttribute("opacity", String(0.25 + 0.75 * Math.sin(Math.PI * t)));
    } else if (pkt) {
      pkt.setAttribute("opacity", "0");
    }
    requestAnimationFrame(tickPackets);
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
    tool_call: "tools", tool_result: "tools",
  };

  function addStep(kind, tag, icon, bodyHtml, meta) {
    const empty = $("streamEmpty");
    if (empty) empty.remove();
    const el = document.createElement("div");
    el.className = "step " + kind;
    el.dataset.kind = KIND_FILTER[kind] || "system";
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
    state.counters.events++;
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
          ts() + "turn " + (e.turn || "?") + "/" + (e.max_turns || "?"));
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
        state.lastToolAt = now();
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
          (preview ? `<span class="cmd">${esc(trunc(preview, 900))}</span>` : ""),
          ts() + "executing" + (isCmd ? " shell command" : ""));
        setPhase("tool", "running " + (e.tool || "tool"), trunc(preview.replace(/\s+/g, " "), 70));
        break;
      }
      case "tool_result": {
        if (state.activeAgent) setAgentStatus(state.activeAgent, state.running ? "thinking" : "idle");
        const ms = state.lastToolAt ? Math.round(now() - state.lastToolAt) : 0;
        if (ms) pushSpark("latency", ms);
        const out = String(e.preview || "");
        addStep("tool_result" + (e.is_error ? " error" : ""), e.is_error ? "result ✗" : "result", e.is_error ? "✗" : "✓",
          `<span class="txt">${esc(trunc(out, 1200))}</span>`,
          ts() + (ms ? ms + " ms" : ""));
        if (e.is_error) {
          state.counters.errors++;
          $("secErrors").textContent = state.counters.errors;
          pushSec("bad", "tool error: " + trunc(out, 80));
        }
        setPhase("thinking", "thinking · " + (state.activeAgent || ""), "");
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
        state.streamEl = null;
        state.streamTxt = null;
        state.streamChars = 0;
        const txt = String(e.text || "").trim();
        if (!txt) break;
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
        state.pendingApproval = true;
        $("approvalCard").classList.remove("hidden");
        $("apTool").textContent = e.tool || "?";
        $("apRisk").textContent = e.risk || "write";
        $("apRisk").className = "pill " + (e.risk === "danger" ? "danger" : "warn");
        $("apPreview").textContent = e.preview || JSON.stringify(e.args || {}, null, 2);
        addStep("tool_call", "approval", "⚠",
          `<b>${esc(e.tool)}</b> is waiting for your approval <span class="pill warn">${esc(e.risk || "")}</span>`,
          ts());
        setPhase("waiting", "waiting for approval · " + (e.tool || ""), "approve or deny on the right");
        break;
      }
      case "approval_response": {
        state.pendingApproval = false;
        $("approvalCard").classList.add("hidden");
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
      default:
        addStep("status", e.type, "·", esc(JSON.stringify(e).slice(0, 300)));
    }
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
    const level = risky === 0 ? "secure" : risky < 3 ? "watch" : "elevated";
    $("secLevel").textContent = level;
    $("secLevel").className = "micro" + (risky ? (risky < 3 ? " warn" : " bad") : "");
  }

  /* ── run lifecycle ────────────────────────────────────────────────── */
  async function run() {
    const task = $("taskInput").value.trim();
    if (!task) { $("taskInput").focus(); return; }
    const body = {
      task,
      role: $("roleSelect").value || null,
      read_only: $("readOnly").checked,
      auto_yes: $("autoYes").checked,
      pipeline: $("pipelineSelect").value || null,
    };
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

    const { ok, body: res } = await api("/api/task", { method: "POST", body: JSON.stringify(body) });
    if (!ok || !res.task_id) {
      state.running = false;
      $("runBtn").disabled = false;
      $("cancelBtn").disabled = true;
      stopElapsed();
      addStep("error", "error", "✗", esc((res && res.error) || "could not start the task"));
      setPhase("error", "could not start the task", "");
      $("telState").textContent = "error";
      return;
    }
    state.taskId = res.task_id;
    $("taskId").textContent = "task " + state.taskId;
    stream(state.taskId);
  }

  function resetRunState() {
    state.counters = { toolCalls: 0, danger: 0, denied: 0, errors: 0, events: 0, modelCalls: 0 };
    state.tokens = { in: 0, out: 0 };
    state.telemetry.turns = 0;
    state.telemetry.tools = 0;
    state.spark = { events: [], tokens: [], latency: [] };
    state.pendingApproval = false;
    $("approvalCard").classList.add("hidden");
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
  }

  function stream(id) {
    if (state.es) state.es.close();
    const url = "/api/events/" + id + (token ? "?token=" + encodeURIComponent(token) : "");
    const es = new EventSource(url);
    state.es = es;
    setUplink("streaming", "ok");
    es.onopen = () => setUplink("streaming", "ok");
    es.onmessage = (msg) => {
      try { onEvent(JSON.parse(msg.data)); } catch (err) { /* ignore malformed frame */ }
    };
    es.onerror = () => {
      if (!es || es.readyState === EventSource.CLOSED) {
        setUplink("stream closed", "warn");
      }
    };
  }

  function finishRun(e) {
    stopElapsed();
    state.running = false;
    if (state.es) { state.es.close(); state.es = null; }
    $("runBtn").disabled = false;
    $("cancelBtn").disabled = true;
    $("approvalCard").classList.add("hidden");
    Object.keys(state.agents).forEach((n) => setAgentStatus(n, "idle"));
    const r = e.result || {};
    const ok = r.ok !== false && e.status !== "error";
    $("telState").textContent = e.status || (ok ? "done" : "error");
    $("sbState").textContent = e.status || "done";
    setPhase(ok ? "done" : "error", (ok ? "run complete" : "run failed") + " · " + (r.role || "") + " · " + (r.agent || ""),
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
    const summary = r.error || r.summary || r.text || "";
    addStep("done", "done", ok ? "✔" : "✗",
      `<b>${ok ? "run complete" : "run failed"}</b> · ${esc(summary ? trunc(summary, 1200) : "no summary")}`,
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

  function cancel() {
    if (!state.taskId) return;
    api("/api/cancel/" + state.taskId, { method: "POST", body: "{}" });
    setPhase("waiting", "cancelling…", "");
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
    $("monEvents").textContent = String(evPerSec * 60);
    $("statCalls").textContent = String(state.counters.modelCalls);
    $("statLatency").textContent = state.spark.latency.length
      ? Math.round(state.spark.latency.slice(-3).reduce((a, b) => a + b, 0) / Math.min(3, state.spark.latency.length)) + "ms"
      : "—";
    $("monLatency").textContent = $("statLatency").textContent;
  }

  const SPARK_COLORS = {
    events: { line: "rgba(0,255,163,.9)", fill: "rgba(0,255,163,.16)" },
    tokens: { line: "rgba(34,227,255,.9)", fill: "rgba(34,227,255,.16)" },
    latency: { line: "rgba(255,176,32,.9)", fill: "rgba(255,176,32,.16)" },
  };

  function drawSpark(key) {
    const canvas = { events: $("sparkEvents"), tokens: $("sparkTokens"), latency: $("sparkLatency") }[key];
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 260;
    const h = canvas.height || 46;
    if (canvas.width !== Math.round(w * dpr)) canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h * dpr);
    const data = state.spark[key];
    const color = SPARK_COLORS[key];
    // grid
    ctx.strokeStyle = "rgba(0,255,163,.08)";
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
    ctx.lineWidth = 1.4;
    ctx.shadowColor = color.line;
    ctx.shadowBlur = 6;
    ctx.stroke();
    ctx.shadowBlur = 0;
    const last = pts[pts.length - 1];
    ctx.beginPath();
    ctx.arc(last[0] - 1, last[1], 2, 0, Math.PI * 2);
    ctx.fillStyle = color.line;
    ctx.fill();
  }

  function drawAllSparks() { ["events", "tokens", "latency"].forEach(drawSpark); }

  /* ── memory card ──────────────────────────────────────────────────── */
  async function refreshMemory(role) {
    const target = role || state.activeRole || $("roleSelect").value || "general";
    $("memRole").textContent = target;
    const { ok, body } = await api("/api/memory/" + encodeURIComponent(target));
    if (!ok || !body) return;
    $("memPath").textContent = body.path || "—";
    const text = String(body.text || "").trim();
    $("memBody").textContent = text || (body.enabled === false
      ? "memory is disabled (settings.memory_enabled=false)"
      : "no memory notes for this role yet — the agent writes here with the `remember` tool.");
  }

  /* ── UI wiring ────────────────────────────────────────────────────── */
  function wireUi() {
    $("runBtn").addEventListener("click", run);
    $("cancelBtn").addEventListener("click", cancel);
    $("clearBtn").addEventListener("click", () => {
      $("stream").innerHTML = '<div class="stream-empty" id="streamEmpty"><span class="big">◈</span><p>Cleared. Describe a task below and press run.</p></div>';
    });
    $("taskInput").addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); run(); }
    });
    $("apYes").addEventListener("click", () => decide(true, "once"));
    $("apAlways").addEventListener("click", () => decide(true, "session"));
    $("apNo").addEventListener("click", () => decide(false, "once"));
    $("roleSelect").addEventListener("change", () => refreshMemory($("roleSelect").value || null));
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
    window.addEventListener("resize", () => { drawAllSparks(); resizeRain(); });
  }

  async function decide(approved, scope) {
    if (!state.taskId) return;
    await api("/api/approve/" + state.taskId, {
      method: "POST", body: JSON.stringify({ approved: approved, scope: scope }),
    });
    $("approvalCard").classList.add("hidden");
    state.pendingApproval = false;
  }

  /* ── ambient effects ──────────────────────────────────────────────── */
  const GLOBE_NODES = [
    [23.8, 90.4], [40.7, -74.0], [51.5, -0.1], [35.7, 139.7], [19.1, 72.9],
    [37.8, -122.4], [-23.5, -46.6], [1.35, 103.8], [55.75, 37.6], [48.8, 2.35],
    [-33.9, 151.2], [52.5, 13.4],
  ];

  let rainCtx = null, rainCols = [], rainW = 0, rainH = 0;

  function resizeRain() {
    const canvas = $("rain");
    if (!canvas) return;
    rainW = canvas.width = window.innerWidth;
    rainH = canvas.height = window.innerHeight;
    const colWidth = 16;
    const count = Math.floor(rainW / colWidth);
    rainCols = Array.from({ length: count }, (_, i) => ({
      x: i * colWidth + 3,
      y: Math.random() * rainH,
      speed: 20 + Math.random() * 42,
      edge: Math.min(i, count - 1 - i) / Math.max(count * 0.14, 1),  // 0 = far edge, 1 = center
    }));
  }

  function startEffects() {
    const canvas = $("rain");
    if (!canvas || REDUCED) return;
    rainCtx = canvas.getContext("2d");
    resizeRain();
    let last = 0;
    const frame = (t) => {
      if (t - last > 55) {
        last = t;
        const ctx = rainCtx;
        ctx.fillStyle = "rgba(3,7,10,0.16)";
        ctx.fillRect(0, 0, rainW, rainH);
        ctx.font = "12px monospace";
        rainCols.forEach((c) => {
          const alpha = c.edge < 1 ? 0.55 - c.edge * 0.45 : 0.06;
          ctx.fillStyle = `rgba(0,255,163,${alpha.toFixed(3)})`;
          ctx.fillText(Math.random() > 0.5 ? "1" : "0", c.x, c.y);
          ctx.fillStyle = `rgba(180,255,235,${(alpha * 0.9).toFixed(3)})`;
          ctx.fillText(Math.random() > 0.5 ? "0" : "1", c.x, c.y - 14);
          c.y += c.speed * 0.12;
          if (c.y > rainH + 20) { c.y = -20; c.speed = 20 + Math.random() * 42; }
        });
      }
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
    startMesh();
    startGlobe();
  }

  function startMesh() {
    const canvas = $("mesh");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    let w = 0, h = 0, pts = [];
    const seed = () => {
      w = canvas.width = Math.floor(window.innerWidth * dpr);
      h = canvas.height = Math.floor(window.innerHeight * dpr);
      pts = Array.from({ length: 46 }, () => ({
        x: Math.random() * w, y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.06 * dpr, vy: (Math.random() - 0.5) * 0.06 * dpr,
      }));
    };
    seed();
    window.addEventListener("resize", seed);
    let last = 0;
    const frame = (t) => {
      if (t - last > 90) {
        last = t;
        ctx.clearRect(0, 0, w, h);
        pts.forEach((p) => { p.x += p.vx; p.y += p.vy;
          if (p.x < 0 || p.x > w) p.vx *= -1;
          if (p.y < 0 || p.y > h) p.vy *= -1; });
        for (let i = 0; i < pts.length; i++) {
          for (let j = i + 1; j < pts.length; j++) {
            const dx = pts[i].x - pts[j].x, dy = pts[i].y - pts[j].y;
            const d = Math.hypot(dx, dy);
            if (d < 190 * dpr) {
              ctx.strokeStyle = `rgba(0,255,163,${(0.10 * (1 - d / (190 * dpr))).toFixed(3)})`;
              ctx.beginPath(); ctx.moveTo(pts[i].x, pts[i].y); ctx.lineTo(pts[j].x, pts[j].y); ctx.stroke();
            }
          }
        }
        pts.forEach((p) => {
          ctx.fillStyle = "rgba(34,227,255,.35)";
          ctx.beginPath(); ctx.arc(p.x, p.y, 1.4 * dpr, 0, Math.PI * 2); ctx.fill();
        });
      }
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  function startGlobe() {
    const canvas = $("globe");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const size = 440;
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.scale(dpr, dpr);
    const cx = size / 2, cy = size / 2, R = size * 0.36;
    const nodes = GLOBE_NODES;
    const arcs = [[0, 4], [2, 8], [5, 11], [7, 3], [9, 6], [1, 0], [10, 7]];
    let angle = 0;
    const project = (lat, lon) => {
      const la = (lat * Math.PI) / 180;
      const lo = ((lon + angle) * Math.PI) / 180;
      const x = Math.cos(la) * Math.cos(lo);
      const z = Math.cos(la) * Math.sin(lo);
      const y = Math.sin(la);
      return { x: cx + x * R, y: cy - y * R, z: z, vis: z > -0.1 };
    };
    const frame = () => {
      angle += 0.34;
      ctx.clearRect(0, 0, size, size);

      // halo
      const halo = ctx.createRadialGradient(cx, cy, R * 0.2, cx, cy, R * 1.22);
      halo.addColorStop(0, "rgba(0,255,163,.14)");
      halo.addColorStop(0.65, "rgba(34,227,255,.05)");
      halo.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = halo;
      ctx.beginPath(); ctx.arc(cx, cy, R * 1.22, 0, Math.PI * 2); ctx.fill();

      // sphere
      ctx.strokeStyle = "rgba(0,255,163,.35)";
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();

      // latitudes
      ctx.strokeStyle = "rgba(34,227,255,.18)";
      for (let lat = -60; lat <= 60; lat += 30) {
        const la = (lat * Math.PI) / 180;
        const rr = Math.cos(la) * R;
        const yy = cy - Math.sin(la) * R;
        ctx.beginPath();
        ctx.ellipse(cx, yy, rr, rr * 0.22, 0, 0, Math.PI * 2);
        ctx.stroke();
      }
      // longitudes
      for (let lon = 0; lon < 180; lon += 30) {
        const pts = [];
        for (let lat = -90; lat <= 90; lat += 6) {
          const p = project(lat, lon);
          pts.push(p);
        }
        ctx.beginPath();
        pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
        ctx.strokeStyle = "rgba(34,227,255,.16)";
        ctx.stroke();
      }

      // arcs between nodes
      arcs.forEach((pair, idx) => {
        const a = nodes[pair[0]], b = nodes[pair[1]];
        if (!a || !b) return;
        const pa = project(a[0], a[1]);
        const pb = project(b[0], b[1]);
        if (!pa.vis || !pb.vis) return;
        const mx = (pa.x + pb.x) / 2, my = (pa.y + pb.y) / 2;
        const lift = 26;
        ctx.beginPath();
        ctx.moveTo(pa.x, pa.y);
        ctx.quadraticCurveTo(mx, my - lift, pb.x, pb.y);
        const t = ((Date.now() / 1400) + idx * 0.18) % 1;
        ctx.strokeStyle = `rgba(0,255,163,${(0.10 + 0.22 * Math.sin(Math.PI * t)).toFixed(3)})`;
        ctx.lineWidth = 1;
        ctx.stroke();
      });

      // nodes
      nodes.forEach((n) => {
        const p = project(n[0], n[1]);
        if (!p.vis) return;
        const depth = clamp((p.z + 1) / 2, 0, 1);
        ctx.fillStyle = `rgba(0,255,163,${(0.35 + depth * 0.6).toFixed(2)})`;
        ctx.beginPath(); ctx.arc(p.x, p.y, 1.6 + depth * 1.6, 0, Math.PI * 2); ctx.fill();
        if (depth > 0.72) {
          ctx.strokeStyle = `rgba(0,255,163,${(depth * 0.35).toFixed(2)})`;
          ctx.beginPath(); ctx.arc(p.x, p.y, 5 + depth * 3, 0, Math.PI * 2); ctx.stroke();
        }
      });

      // scan line
      const sy = cy + Math.sin(Date.now() / 900) * R;
      ctx.strokeStyle = "rgba(0,255,163,.30)";
      ctx.beginPath(); ctx.moveTo(cx - R, sy); ctx.lineTo(cx + R, sy); ctx.stroke();

      setTimeout(() => requestAnimationFrame(frame), 45);
    };
    requestAnimationFrame(frame);
  }

  window.addEventListener("error", (e) => {
    const stream = $("stream");
    if (stream && e && e.message) {
      addStep("error", "ui", "✗", esc(String(e.message)));
    }
  });

  boot();
})();
