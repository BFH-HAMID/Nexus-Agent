/* Nexus panel / VS Code sidebar webview front-end.
   Talks to the local nexus server (same origin) over JSON + SSE. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const token = new URLSearchParams(location.search).get("token") || "";
  let taskId = null;
  let es = null;

  function api(path, options) {
    const opts = options || {};
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (token) opts.headers["X-Nexus-Token"] = token;
    return fetch(path, opts).then((r) => r.json().catch(() => ({})).then((j) => ({ ok: r.ok, status: r.status, body: j })));
  }

  function ev(cls, tag, text) {
    const div = document.createElement("div");
    div.className = "ev " + cls;
    const span = document.createElement("span");
    span.className = "tag";
    span.textContent = tag;
    div.appendChild(span);
    div.appendChild(document.createTextNode(text || ""));
    const log = $("log");
    log.appendChild(div);
    while (log.childNodes.length > 800) log.removeChild(log.firstChild);
    log.scrollTop = log.scrollHeight;
    window.scrollTo(0, document.body.scrollHeight);
  }

  function setStatus(text, live) {
    $("status").textContent = text;
    $("dot").className = "dot" + (live ? " live" : "");
  }

  function loadHealth() {
    api("/health").then(({ body }) => {
      $("ws").textContent = body.workspace || "?";
      $("version").textContent = "nexus " + (body.version || "?") +
        " · " + (body.roles || 0) + " roles · " + ((body.agents || []).join(", ") || "no agents");
    });
  }

  function loadRoles() {
    api("/api/roles").then(({ body }) => {
      const sel = $("role");
      const groups = {};
      (body || []).forEach((r) => {
        const cat = r.category || "general";
        (groups[cat] = groups[cat] || []).push(r);
      });
      Object.keys(groups).sort().forEach((cat) => {
        const og = document.createElement("optgroup");
        og.label = cat + " (" + groups[cat].length + ")";
        groups[cat].sort((a, b) => a.name.localeCompare(b.name)).forEach((r) => {
          const opt = document.createElement("option");
          opt.value = r.name;
          opt.textContent = r.name + " → " + r.agent;
          og.appendChild(opt);
        });
        sel.appendChild(og);
      });
    });
  }

  function loadPipelines() {
    api("/api/pipelines").then(({ body }) => {
      const sel = $("pipeline");
      Object.keys(body || {}).sort().forEach((name) => {
        const opt = document.createElement("option");
        const steps = ((body[name] || {}).steps || []).map((s) => s.role).join(" → ");
        opt.value = name;
        opt.textContent = name + (steps ? "  (" + steps + ")" : "");
        sel.appendChild(opt);
      });
    });
  }

  function showApproval(data) {
    $("approval").classList.remove("hidden");
    $("apTool").textContent = data.tool || "";
    $("apRisk").textContent = data.risk || "";
    $("apPreview").textContent = data.preview || JSON.stringify(data.args || {}, null, 2);
    setStatus("awaiting approval", true);
    // let a VS Code host show native approve/deny buttons
    if (window.parent !== window) {
      window.parent.postMessage({
        source: "nexus-panel", type: "approval", taskId: taskId,
        tool: data.tool || "", risk: data.risk || "", preview: $("apPreview").textContent,
      }, "*");
    }
  }

  function hideApproval() {
    $("approval").classList.add("hidden");
  }

  function decide(approved, scope) {
    if (!taskId) return;
    api("/api/approve/" + taskId, {
      method: "POST",
      body: JSON.stringify({ approved: approved, scope: scope || "once" }),
    });
    hideApproval();
  }

  function handleEvent(data) {
    const t = data.type;
    if (t === "role") {
      $("curRole").textContent = data.role || "?";
      $("curAgent").textContent = data.agent || "?";
      if (window.parent !== window) {
        window.parent.postMessage({ source: "nexus-panel", type: "role", role: data.role, agent: data.agent }, "*");
      }
      ev("role", "role", (data.role || "?") + " → " + (data.agent || "?") + "  [" + (data.method || "") + "]" +
        (data.hits && data.hits.length ? "  hits: " + data.hits.join(", ") : "") +
        (data.note ? "  (" + data.note + ")" : ""));
    } else if (t === "agent") {
      if (data.error) ev("agent", "agent", "⚠ " + (data.agent || "") + " failed: " + data.error);
      else ev("agent", "agent", data.message || ((data.agent || "") + " " + (data.model || "")));
    } else if (t === "text") {
      ev("text", "assistant", (data.text || "").trim());
    } else if (t === "tool_call") {
      ev("tool_call", data.tool, data.preview || "");
    } else if (t === "tool_result") {
      ev("tool_result", data.is_error ? "✗ error" : "✓ ok", (data.preview || "").slice(0, 600));
    } else if (t === "think") {
      ev("status", "think", "◉ thinking · " + (data.agent || "") + " " + (data.model || "") +
        (data.turn ? " · turn " + data.turn + "/" + data.max_turns : ""));
    } else if (t === "usage") {
      /* live token meters - nothing to print in the compact panel */
    } else if (t === "status") {
      ev("status", "status", data.message || "");
    } else if (t === "memory") {
      ev("status", "memory", "saved " + (data.role || "") + " → " + (data.path || ""));
    } else if (t === "error") {
      ev("error", "error", data.message || JSON.stringify(data));
    } else if (t === "approval_request") {
      showApproval(data);
    } else if (t === "approval_response") {
      hideApproval();
    } else if (t === "ping") {
      /* keep-alive */
    } else if (t === "done") {
      finish(data);
    } else {
      ev("status", t, JSON.stringify(data).slice(0, 400));
    }
  }

  function finish(data) {
    hideApproval();
    if (es) { es.close(); es = null; }
    setStatus(data.status || "done", false);
    $("run").disabled = false;
    $("cancel").disabled = true;
    const result = data.result || {};
    if (result && (result.summary || result.text || result.error)) {
      $("summary").classList.remove("hidden");
      $("summaryBody").textContent = result.error || result.summary || result.text || "";
      const meta = [
        result.role ? "role=" + result.role : "",
        result.agent ? "agent=" + result.agent : "",
        result.turns != null ? "turns=" + result.turns : "",
        result.tool_calls != null ? "tools=" + result.tool_calls : "",
        result.input_tokens != null ? "tokens=" + result.input_tokens + "in/" + result.output_tokens + "out" : "",
        result.duration_s != null ? result.duration_s + "s" : "",
      ].filter(Boolean).join("  ·  ");
      $("summaryFiles").textContent =
        ((result.changed_files || []).length ? "\nfiles: " + result.changed_files.join(", ") : "") +
        (meta ? "\n" + meta : "") +
        ((result.followups || []).length ? "\nfollow-ups: " + result.followups.join("; ") : "");
    }
    // let a VS Code host know the run ended
    if (window.parent !== window) {
      window.parent.postMessage({ source: "nexus-panel", type: "done", result: result }, "*");
    }
  }

  function stream(id) {
    const url = "/api/events/" + id + (token ? "?token=" + encodeURIComponent(token) : "");
    es = new EventSource(url);
    es.onmessage = (msg) => {
      try { handleEvent(JSON.parse(msg.data)); } catch (e) { /* ignore */ }
    };
    es.onerror = () => {
      if (es && es.readyState === EventSource.CLOSED) setStatus("stream closed", false);
    };
  }

  function run() {
    const task = $("task").value.trim();
    if (!task) { ev("error", "error", "write a task first"); return; }
    const pipeline = $("pipeline").value;
    const body = {
      task: task,
      role: $("role").value || null,
      read_only: $("readonly").checked,
      pipeline: pipeline || null,
    };
    $("log").innerHTML = "";
    $("summary").classList.add("hidden");
    $("run").disabled = true;
    $("cancel").disabled = false;
    setStatus("starting", true);
    ev("status", "task", (body.pipeline ? "pipeline " + body.pipeline + " :: " : "") + body.task);
    api("/api/task", { method: "POST", body: JSON.stringify(body) }).then(({ ok, body: res }) => {
      if (!ok || !res.task_id) {
        ev("error", "error", (res && res.error) || "could not start the task");
        setStatus("error", false);
        $("run").disabled = false;
        $("cancel").disabled = true;
        return;
      }
      taskId = res.task_id;
      $("taskInfo").textContent = "task " + taskId;
      stream(taskId);
    });
  }

  function cancel() {
    if (!taskId) return;
    api("/api/cancel/" + taskId, { method: "POST", body: "{}" });
    setStatus("cancelling", false);
  }

  $("run").addEventListener("click", run);
  $("cancel").addEventListener("click", cancel);
  $("clear").addEventListener("click", () => { $("log").innerHTML = ""; $("summary").classList.add("hidden"); });
  $("apYes").addEventListener("click", () => decide(true, "once"));
  $("apAlways").addEventListener("click", () => decide(true, "session"));
  $("apNo").addEventListener("click", () => decide(false, "once"));
  $("task").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") run();
  });
  $("role").addEventListener("change", (e) => { $("curRole").textContent = e.target.value || "auto"; });

  // messages from a VS Code webview host
  window.addEventListener("message", (e) => {
    const data = e.data || {};
    if (data.source !== "nexus-host") return;
    if (data.type === "task") { $("task").value = data.task || ""; if (data.autorun) run(); }
    if (data.type === "approve") decide(!!data.approved, data.scope);
    if (data.type === "role") { $("role").value = data.role || ""; $("curRole").textContent = data.role || "auto"; }
  });

  // tell a VS Code host we are ready to receive tasks
  if (window.parent !== window) {
    window.parent.postMessage({ source: "nexus-panel", type: "ready" }, "*");
  }

  loadHealth();
  loadRoles();
  loadPipelines();
})();
