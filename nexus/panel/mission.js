/* Mission Control: the landing page.

   One request (`/api/mission/summary`) paints the six tiles, the recorded
   activity list and the health strip; `/api/mission/stream` then keeps them
   fresh with five named events (heartbeat, task.update, approval.new,
   metrics.tick, alert.new). Every number says when it was observed; when a
   section is missing the tile says "Unavailable" instead of inventing a value.
   If the stream is blocked (a proxy that buffers SSE, an offline tab) the page
   falls back to polling and the pill says "polling" — it never pretends to be
   live. Nothing on this page starts a task. */
(() => {
  'use strict';

  const gui = window.nexusGui;
  const i18n = window.nexusI18n;
  if (!gui || !i18n) return;
  const t = i18n.t;
  const $ = (id) => document.getElementById(id);

  const root = document.createElement('section');
  root.id = 'view-mission';
  root.className = 'gui-modules mission-page';
  root.dataset.guiView = 'mission';
  root.hidden = gui.view !== 'mission';
  root.innerHTML = `
    <header class="mission-head">
      <div>
        <p class="mission-eyebrow" data-i18n="mission.eyebrow">Mission Control</p>
        <h1 data-i18n="mission.title">What the agent is doing right now</h1>
        <p class="mission-sub" data-i18n="mission.subtitle"></p>
      </div>
      <div class="mission-quick" role="group" aria-label="Quick actions">
        <button class="btn primary" type="button" data-act="compose"><span class="btn-label" data-i18n="mission.compose"></span></button>
        <button class="btn" type="button" data-act="approvals"><span class="btn-label" data-i18n="mission.review"></span></button>
        <button class="btn" type="button" data-act="dnd"><span class="btn-label" data-i18n="mission.dnd"></span></button>
        <button class="btn" type="button" data-act="lastrun"><span class="btn-label" data-i18n="mission.lastRun"></span></button>
        <button class="btn ghost" type="button" data-act="refresh"><span class="btn-label" data-i18n="mission.refresh"></span></button>
        <span class="mission-stream" id="msStream" data-state="connecting" role="status" aria-live="polite"
              title="Server-sent events; every reading is timestamped" data-i18n-aria="mission.streamAria">
          <i class="dot" aria-hidden="true"></i><span data-i18n="mission.stream.connecting">connecting…</span>
        </span>
      </div>
    </header>

    <div class="mission-tiles" id="missionTiles">
      <button class="mission-tile" type="button" data-go="approvals" aria-describedby="msDaemonAge msDaemonHint">
        <span class="mt-head"><svg class="ic" aria-hidden="true"><use href="#i-shield"/></svg><span data-i18n="mission.daemon"></span></span>
        <b class="mt-value" id="msDaemonState" data-state="unknown">—</b>
        <span class="mt-hint"><span id="msDaemonAge">—</span> · <span id="msDaemonHint" data-i18n="mission.daemonHint"></span></span>
      </button>
      <button class="mission-tile" type="button" data-go="run">
        <span class="mt-head"><svg class="ic" aria-hidden="true"><use href="#i-chat"/></svg><span data-i18n="mission.tasks"></span></span>
        <span class="mt-split">
          <span><b id="msTasksActive">—</b><small data-i18n="mission.active"></small></span>
          <span><b id="msTasksQueued">—</b><small data-i18n="mission.queued"></small></span>
          <span><b id="msTasksFailed">—</b><small data-i18n="mission.failed"></small></span>
        </span>
        <span class="mt-hint" id="msTasksHint" data-i18n="mission.tasksHint"></span>
      </button>
      <button class="mission-tile" type="button" data-go="approvals">
        <span class="mt-head"><svg class="ic" aria-hidden="true"><use href="#i-lock"/></svg><span data-i18n="mission.approvals"></span>
          <span class="pill warn" id="msApprovalTone">—</span></span>
        <b class="mt-value" id="msApprovals">—</b>
        <span class="mt-hint" data-i18n="mission.approvalsHint"></span>
      </button>
      <button class="mission-tile" type="button" data-go="run">
        <span class="mt-head"><svg class="ic" aria-hidden="true"><use href="#i-cpu"/></svg><span data-i18n="mission.resources"></span></span>
        <span class="mt-split">
          <span><b id="msCpu">—</b><small>CPU</small></span>
          <span><b id="msRam">—</b><small>RAM</small></span>
          <span><b id="msDisk">—</b><small data-i18n="mission.disk"></small></span>
        </span>
        <span class="mt-hint" id="msGpu">—</span>
      </button>
      <button class="mission-tile" type="button" data-go="cost">
        <span class="mt-head"><svg class="ic" aria-hidden="true"><use href="#i-coin"/></svg><span data-i18n="mission.usage"></span></span>
        <span class="mt-split">
          <span><b id="msTokensIn">—</b><small data-i18n="mission.tokensIn"></small></span>
          <span><b id="msTokensOut">—</b><small data-i18n="mission.tokensOut"></small></span>
        </span>
        <span class="mt-hint" id="msCost" data-i18n="mission.usageHint"></span>
      </button>
      <button class="mission-tile" type="button" data-go="roles">
        <span class="mt-head"><svg class="ic" aria-hidden="true"><use href="#i-users"/></svg><span data-i18n="mission.agents"></span></span>
        <span class="mt-split"><span><b id="msAgents">—</b><small data-i18n="mission.withKeys"></small></span></span>
        <span class="mt-hint" id="msAgentsHint" data-i18n="mission.reach"></span>
      </button>
    </div>

    <div class="mission-grid">
      <section class="card mission-activity" aria-labelledby="msActivityTitle">
        <div class="card-head"><span class="sq" aria-hidden="true"></span>
          <h2 id="msActivityTitle" data-i18n="mission.activity"></h2>
          <span class="spacer"></span><span class="micro" id="msActivityMeta">—</span>
        </div>
        <ol class="mission-feed" id="msFeed"><li class="mission-empty" data-i18n="mission.activityEmpty"></li></ol>
      </section>
      <section class="card mission-health" aria-labelledby="msHealthTitle">
        <div class="card-head"><span class="sq" aria-hidden="true"></span>
          <h2 id="msHealthTitle" data-i18n="mission.health"></h2>
          <span class="spacer"></span><span class="micro" data-i18n="mission.healthHint"></span>
        </div>
        <ul class="health-strip" id="msHealth">
          <li data-chip="server"><span data-i18n="mission.server"></span><b data-state="unknown">—</b></li>
          <li data-chip="stream"><span data-i18n="mission.streamLabel"></span><b data-state="unknown">—</b></li>
          <li data-chip="ollama"><span>Ollama</span><b data-state="unknown">—</b></li>
          <li data-chip="github"><span>GitHub</span><b data-state="unknown">—</b></li>
          <li data-chip="telegram"><span>Telegram</span><b data-state="unknown">—</b></li>
          <li data-chip="mcp"><span>MCP</span><b data-state="unknown">—</b></li>
          <li data-chip="disk"><span data-i18n="mission.disk"></span><b data-state="unknown">—</b></li>
        </ul>
        <p class="mission-foot" id="missionUpdated" data-i18n="mission.never"></p>
      </section>
    </div>`;
  document.querySelector('footer.statusbar')?.before(root);

  const nodes = {
    daemon: $('msDaemonState'), daemonAge: $('msDaemonAge'), daemonHint: $('msDaemonHint'),
    active: $('msTasksActive'), queued: $('msTasksQueued'), failed: $('msTasksFailed'), tasksHint: $('msTasksHint'),
    approvals: $('msApprovals'), approvalTone: $('msApprovalTone'),
    cpu: $('msCpu'), ram: $('msRam'), disk: $('msDisk'), gpu: $('msGpu'),
    tokensIn: $('msTokensIn'), tokensOut: $('msTokensOut'), cost: $('msCost'),
    agents: $('msAgents'), agentsHint: $('msAgentsHint'),
    feed: $('msFeed'), feedMeta: $('msActivityMeta'), updated: $('missionUpdated'),
    stream: $('msStream'),
  };

  const unavailable = () => t('mission.unavailable');
  const set = (node, value, tone) => {
    if (!node) return;
    node.textContent = value;
    if (tone) node.dataset.state = tone;
  };
  const percent = (value) => (typeof value === 'number' && Number.isFinite(value) ? Math.round(value) + '%' : unavailable());
  const bytes = (value) => {
    if (typeof value !== 'number' || !Number.isFinite(value)) return unavailable();
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let size = value;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    return `${size.toFixed(size >= 10 ? 0 : 1)} ${units[unit]}`;
  };
  const age = (seconds) => {
    if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return unavailable();
    if (seconds < 90) return Math.round(seconds) + 's';
    if (seconds < 5400) return Math.round(seconds / 60) + 'm';
    return Math.round(seconds / 3600) + 'h';
  };
  const observedAgo = (stamp) => {
    const value = Number(stamp);
    if (!Number.isFinite(value) || value <= 0) return null;
    return age(Math.max(0, Math.floor(Date.now() / 1000) - value));
  };
  const sectionOk = (section) => !!section && section.state === 'ok';

  /* ── painting from the summary / stream payloads ───────────────────── */

  function paintDaemon(daemon) {
    if (!sectionOk(daemon)) {
      set(nodes.daemon, unavailable(), 'unknown');
      set(nodes.daemonAge, daemon?.reason || t('mission.never'));
      set(nodes.daemonHint, t('mission.daemonHint'));
      return;
    }
    const known = ['running', 'idle', 'stopped', 'killed', 'paused', 'unknown'];
    const state = String(daemon.status || 'unknown');
    set(nodes.daemon, known.includes(state) ? t('mission.state.' + state) : state, state);
    const ago = observedAgo(daemon.observed_at);
    set(nodes.daemonAge, ago ? t('mission.snapshot', {age: ago}) : t('mission.never'));
    set(nodes.daemonHint, typeof daemon.lock_held === 'boolean'
      ? t('mission.lock', {value: daemon.lock_held ? t('mission.yes') : t('mission.no')})
      : t('mission.daemonHint'));
  }

  function paintTasks(tasks) {
    if (!sectionOk(tasks)) {
      set(nodes.active, unavailable()); set(nodes.queued, unavailable()); set(nodes.failed, unavailable());
      set(nodes.tasksHint, tasks?.reason || t('mission.notChecked'));
      return;
    }
    set(nodes.active, String(tasks.active ?? 0));
    set(nodes.queued, String(tasks.queued ?? 0));
    set(nodes.failed, String(tasks.failed_24h ?? 0));
    const ago = observedAgo(tasks.observed_at);
    set(nodes.tasksHint, ago ? t('mission.sampledWindow', {age: ago}) : t('mission.tasksHint'));
  }

  function paintApprovals(approvals) {
    if (!sectionOk(approvals)) {
      set(nodes.approvals, unavailable());
      set(nodes.approvalTone, '—', 'unknown');
      return;
    }
    const pending = Number(approvals.pending) || 0;
    set(nodes.approvals, String(pending));
    if (nodes.approvalTone) {
      nodes.approvalTone.dataset.state = pending ? 'warn' : 'ok';
      nodes.approvalTone.textContent = pending > 2 ? t('mission.toneHigh')
        : pending ? t('mission.toneMedium') : t('mission.toneClear');
    }
  }

  function paintResources(resources) {
    if (!sectionOk(resources)) {
      set(nodes.cpu, unavailable()); set(nodes.ram, unavailable()); set(nodes.disk, unavailable());
      set(nodes.gpu, t('mission.gpu', {value: resources?.reason || unavailable()}));
      return;
    }
    set(nodes.cpu, percent(resources.cpu_percent));
    set(nodes.ram, percent(resources.ram_percent));
    set(nodes.disk, bytes(resources.disk_free_bytes));
    /* "nvidia-smi not installed" is more useful than a bare "Unavailable". */
    const gpu = String(resources.gpu || resources.gpu_reason || t('mission.unavailable')).slice(0, 70);
    const ago = observedAgo(resources.observed_at);
    set(nodes.gpu, `${t('mission.gpu', {value: gpu})}${ago ? ' · ' + t('mission.sampled', {age: ago}) : ''}`);
  }

  function paintUsage(usage) {
    if (!sectionOk(usage)) {
      set(nodes.tokensIn, unavailable()); set(nodes.tokensOut, unavailable());
      set(nodes.cost, `${t('mission.cost')}: ${t('mission.costUnavailable')}`);
      return;
    }
    set(nodes.tokensIn, typeof usage.tokens_in === 'number' ? usage.tokens_in.toLocaleString() : unavailable());
    set(nodes.tokensOut, typeof usage.tokens_out === 'number' ? usage.tokens_out.toLocaleString() : unavailable());
    set(nodes.cost, typeof usage.cost_usd === 'number' && Number.isFinite(usage.cost_usd)
      ? `${t('mission.cost')}: $${usage.cost_usd.toFixed(4)}`
      : `${t('mission.cost')}: ${t('mission.costUnavailable')}`);
  }

  function paintAgents(agents) {
    if (!sectionOk(agents) || typeof agents.total !== 'number') {
      set(nodes.agents, unavailable()); set(nodes.agentsHint, t('mission.reach'));
      return;
    }
    set(nodes.agents, `${agents.with_keys ?? 0} / ${agents.total}`);
    set(nodes.agentsHint, t('mission.reach'));
  }

  function paintHealth(health) {
    if (!health || typeof health !== 'object') return;
    for (const [chip, value] of Object.entries(health)) {
      paintHealthChip(chip, value);
    }
  }

  function paintHealthChip(chip, value) {
    const node = document.querySelector(`#msHealth [data-chip="${chip}"] b`);
    if (!node) return;
    const state = value && value.state ? value.state : 'unknown';
    node.dataset.state = state;
    node.textContent = value && value.text
      ? (state === 'unknown' && value.text === 'not checked' ? t('mission.notChecked') : String(value.text))
      : '—';
    node.title = value && value.checked_at
      ? new Date(value.checked_at * 1000).toLocaleTimeString()
      : t('mission.notChecked');
  }

  function paintDiskChip(resources) {
    const node = document.querySelector('#msHealth [data-chip="disk"] b');
    if (!node) return;
    if (!sectionOk(resources) || typeof resources.disk_free_bytes !== 'number') {
      node.dataset.state = 'unknown';
      node.textContent = t('mission.notChecked');
      return;
    }
    const free = resources.disk_free_bytes / 1024 ** 3;
    node.dataset.state = free < 5 ? 'warn' : 'ok';
    node.textContent = `${free.toFixed(1)} GB`;
  }

  function feedRow({ title, meta, time, status, kind }) {
    const li = document.createElement('li');
    li.dataset.status = status || 'unknown';
    if (kind) li.dataset.kind = kind;
    const dot = document.createElement('i');
    dot.className = 'dot';
    dot.setAttribute('aria-hidden', 'true');
    const body = document.createElement('div');
    const name = document.createElement('b');
    name.textContent = String(title || '').slice(0, 120);
    const detail = document.createElement('span');
    detail.textContent = meta || '';
    body.append(name, detail);
    const stamp = document.createElement('time');
    stamp.textContent = time || '—';
    li.append(dot, body, stamp);
    return li;
  }

  function paintActivity(activity) {
    const rows = (Array.isArray(activity) ? activity : []).filter((row) => row && row.id);
    nodes.feed.replaceChildren();
    if (!rows.length) {
      const li = document.createElement('li');
      li.className = 'mission-empty';
      li.textContent = t('mission.activityEmpty');
      nodes.feed.append(li);
      set(nodes.feedMeta, '0');
      return;
    }
    for (const task of rows.slice(0, 8)) {
      const li = feedRow({
        title: task.task || task.id,
        meta: [task.status, task.role, task.agent].filter(Boolean).join(' · '),
        time: task.created ? new Date(task.created * 1000).toLocaleTimeString() : '—',
        status: task.status,
      });
      li.tabIndex = 0;
      li.setAttribute('role', 'button');
      const open = () => {
        nav('run');
        window.setTimeout(() => { try { window.nexusAttachTask?.(task.id); } catch { /* best effort */ } }, 120);
      };
      li.addEventListener('click', open);
      li.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') li.click(); });
      nodes.feed.append(li);
    }
    set(nodes.feedMeta, String(rows.length));
  }

  function pushAlert(alert) {
    if (!alert || !alert.id) return;
    if (nodes.feed.querySelector(`[data-alert="${CSS.escape(alert.id)}"]`)) return;
    const li = feedRow({
      title: alert.title || t('mission.alert'),
      meta: [t('mission.alert'), alert.detail, alert.source].filter(Boolean).join(' · '),
      time: alert.observed_at ? new Date(alert.observed_at * 1000).toLocaleTimeString() : '—',
      status: alert.severity === 'high' ? 'failed' : 'queued',
      kind: 'alert',
    });
    li.dataset.alert = alert.id;
    li.dataset.severity = alert.severity || 'warn';
    const empty = nodes.feed.querySelector('.mission-empty');
    if (empty) empty.remove();
    nodes.feed.prepend(li);
    while (nodes.feed.children.length > 9) nodes.feed.lastElementChild.remove();
    set(nodes.feedMeta, String(nodes.feed.querySelectorAll('li[data-status]').length));
    if (alert.severity !== 'info') {
      window.nexusToast?.(`${alert.title}: ${alert.detail || ''}`.trim(), {tone: alert.severity === 'high' ? 'error' : 'warn', title: t('mission.alert')});
    }
  }

  function stampAt(observed) {
    if (!nodes.updated) return;
    const value = Number(observed);
    const when = Number.isFinite(value) && value > 0 ? new Date(value * 1000) : new Date();
    nodes.updated.textContent = t('mission.updated', {time: when.toLocaleTimeString()});
    nodes.updated.dataset.observed = String(value || '');
  }

  /* The last summary is kept so a language switch can re-render the computed
     strings (ages, costs, state words) at once instead of waiting a poll. */
  let lastSummary = null;

  function paintSummary(data) {
    lastSummary = data;
    paintDaemon(data.daemon);
    paintTasks(data.tasks);
    paintApprovals(data.approvals);
    paintResources(data.resources);
    paintUsage(data.usage);
    paintAgents(data.agents);
    paintActivity(data.activity);
    paintHealth(data.health);
    paintDiskChip(data.resources);
    for (const alert of data.alerts || []) pushAlert(alert);
    stampAt(data.observed_at);
    if (nodes.stream && nodes.stream.dataset.state === 'connecting') setStream('polling');
  }

  /* ── live feed ─────────────────────────────────────────────────────── */

  const STREAM_LABEL = { connecting: 'mission.stream.connecting', live: 'mission.stream.live',
    reconnecting: 'mission.stream.reconnecting', polling: 'mission.stream.polling', offline: 'mission.stream.offline' };
  let streamState = 'connecting';

  function setStream(state) {
    streamState = state;
    window.dispatchEvent(new CustomEvent('nexus:feed', {detail: state === 'live' ? 'streaming' : 'idle'}));
    if (!nodes.stream) return;
    nodes.stream.dataset.state = state;
    const label = nodes.stream.querySelector('span');
    if (label) {
      label.dataset.i18n = STREAM_LABEL[state] || STREAM_LABEL.offline;
      label.textContent = t(STREAM_LABEL[state] || STREAM_LABEL.offline);
    }
  }

  let source = null;
  let retryTimer = null;
  let attempts = 0;
  let lastEventAt = 0;

  function handle(name, payload) {
    lastEventAt = Date.now();
    if (name === 'heartbeat') {
      paintHealth(payload.health);
      paintUsage(payload.usage);
      paintAgents(payload.agents);
      paintDaemon(payload.daemon);
      for (const alert of payload.alerts || []) pushAlert(alert);
      if (payload.observed_at) stampAt(payload.observed_at);
    } else if (name === 'metrics.tick') {
      paintResources(payload);
      paintDiskChip(payload);
      stampAt(payload.observed_at);
    } else if (name === 'task.update') {
      paintTasks(payload);
      paintActivity(payload.activity);
      stampAt(payload.observed_at);
    } else if (name === 'approval.new') {
      paintApprovals(payload);
      if (Array.isArray(payload.new_ids) && payload.new_ids.length) {
        window.nexusToast?.(t('mission.newApproval'), {tone: 'warn', title: t('mission.approvals')});
        window.dispatchEvent(new CustomEvent('nexus:pending', {detail: {pending_actions_total: payload.pending}}));
      }
    } else if (name === 'alert.new') {
      pushAlert(payload);
      paintHealthChip(payload.source === 'gpu' ? 'gpu' : payload.source, {state: payload.severity === 'info' ? 'warn' : 'off', text: payload.detail, checked_at: payload.observed_at});
    }
    if (streamState !== 'live') setStream('live');
  }

  function closeStream() {
    window.clearTimeout(retryTimer);
    retryTimer = null;
    if (source) {
      source.close();
      source = null;
    }
  }

  function openStream() {
    if (source || typeof window.EventSource !== 'function') { if (!source) setStream('polling'); return; }
    const url = typeof gui.streamUrl === 'function' ? gui.streamUrl('/api/mission/stream') : '/api/mission/stream';
    let es;
    try {
      es = new EventSource(url);
    } catch {
      setStream('polling');
      return;
    }
    source = es;
    setStream(attempts ? 'reconnecting' : 'connecting');
    es.onopen = () => {
      attempts = 0;
      setStream('live');
      if (nodes.stream) nodes.stream.title = t('mission.streamTitle', {time: new Date().toLocaleTimeString()});
    };
    for (const name of ['heartbeat', 'task.update', 'approval.new', 'metrics.tick', 'alert.new']) {
      es.addEventListener(name, (event) => {
        try {
          handle(name, JSON.parse(event.data));
        } catch { /* a malformed frame must not break the page */ }
      });
    }
    es.onerror = () => {
      closeStream();
      attempts += 1;
      setStream(attempts > 3 ? 'polling' : 'reconnecting');
      const delay = Math.min(30000, 1500 * attempts);
      retryTimer = window.setTimeout(() => { if (gui.view === 'mission' && document.visibilityState === 'visible') openStream(); }, delay);
    };
  }

  /* ── summary request + polling fallback ─────────────────────────────── */

  let busy = false;
  async function load() {
    if (busy) return;
    busy = true;
    try {
      const payload = await gui.api('/api/mission/summary');
      const data = payload?.data || payload;
      if (data && typeof data === 'object') paintSummary(data);
    } catch (error) {
      if (nodes.stream) nodes.stream.title = `${t('mission.stream.offline')}: ${error.message}`;
      if (streamState === 'live') setStream('reconnecting');
      else if (streamState !== 'polling') setStream('offline');
    } finally {
      busy = false;
    }
  }

  let timer = null;
  const startPolling = () => {
    if (timer) return;
    timer = window.setInterval(() => {
      if (document.visibilityState === 'visible' && streamState !== 'live') load();
    }, 15000);
  };
  const stopPolling = () => { if (timer) { window.clearInterval(timer); timer = null; } };

  /* ── quick actions ─────────────────────────────────────────────────── */

  const nav = (id) => window.nexusNav?.go(id);

  function compose() {
    nav('run');
    window.setTimeout(() => document.getElementById('taskInput')?.focus(), 140);
  }

  root.querySelectorAll('[data-go]').forEach((node) => {
    node.addEventListener('click', () => nav(node.dataset.go));
  });

  let dndArmed = false;
  function armDnd(button) {
    if (!dndArmed) {
      dndArmed = true;
      button.classList.add('armed');
      button.querySelector('.btn-label').textContent = t('mission.dndConfirm');
      window.setTimeout(() => {
        if (!dndArmed) return;
        dndArmed = false;
        button.classList.remove('armed');
        button.querySelector('.btn-label').textContent = t('mission.dnd');
      }, 6000);
      return;
    }
    dndArmed = false;
    button.classList.remove('armed');
    button.querySelector('.btn-label').textContent = t('mission.dnd');
    gui.api('/api/operations/action/delivery', {quiet: true, minutes: 60})
      .then(() => window.nexusToast?.(t('mission.dndDone'), {tone: 'done'}))
      .catch((error) => window.nexusToast?.(`DND: ${error.message}`, {tone: 'error'}));
  }

  root.querySelector('.mission-quick').addEventListener('click', (event) => {
    const button = event.target.closest('[data-act]');
    if (!button) return;
    const act = button.dataset.act;
    if (act === 'compose') compose();
    else if (act === 'approvals') nav('approvals');
    else if (act === 'dnd') armDnd(button);
    else if (act === 'lastrun') {
      const first = nodes.feed.querySelector('li[data-status]:not([data-kind="alert"])');
      if (!first) { window.nexusToast?.(t('mission.noRun'), {tone: 'info'}); return; }
      first.click();
    } else if (act === 'refresh') {
      load();
      window.nexusToast?.(t('mission.refreshed'), {tone: 'done', timeout: 1800});
    }
  });

  /* ── lifecycle ─────────────────────────────────────────────────────── */

  function activate() {
    load();
    openStream();
    startPolling();
  }

  function deactivate() {
    stopPolling();
    closeStream();
    setStream('connecting');
  }

  window.addEventListener('nexus:view', (event) => {
    if (event.detail === 'mission') activate();
    else deactivate();
  });

  document.addEventListener('visibilitychange', () => {
    if (gui.view !== 'mission') return;
    if (document.visibilityState === 'visible') activate();
    else deactivate();
  });

  i18n.onChange(() => {
    i18n.apply(root);
    if (lastSummary) paintSummary(lastSummary);
    else load();
  });

  gui.ready.then(() => {
    if (gui.view === 'mission') activate();
  });

  window.nexusMission = {
    load,
    root,
    state: () => ({stream: streamState, lastEventAt, attempts}),
    streamUrl: () => (typeof gui.streamUrl === 'function' ? gui.streamUrl('/api/mission/stream') : '/api/mission/stream'),
  };
})();
