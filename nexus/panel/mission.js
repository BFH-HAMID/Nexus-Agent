/* Mission Control: the landing page.

   Six live tiles, a short recorded-activity list and a health strip. Every
   number comes from an existing endpoint; when a measurement is missing the
   tile says "Unavailable" / "not checked" instead of rendering a placeholder
   chart. Nothing on this page starts a task; the quick actions either navigate
   or ask for a second confirmation before a policy change. */
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
        <span class="mt-hint" data-i18n="mission.tasksHint"></span>
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
          <li data-chip="sse"><span data-i18n="mission.sse"></span><b data-state="unknown">—</b></li>
          <li data-chip="ollama"><span data-i18n="mission.ollama"></span><b data-state="unknown">—</b></li>
          <li data-chip="github"><span data-i18n="mission.github"></span><b data-state="unknown">—</b></li>
          <li data-chip="telegram"><span data-i18n="mission.telegram"></span><b data-state="unknown">—</b></li>
          <li data-chip="mcp"><span data-i18n="mission.mcp"></span><b data-state="unknown">—</b></li>
          <li data-chip="disk"><span data-i18n="mission.disk"></span><b data-state="unknown">—</b></li>
        </ul>
        <p class="mission-foot" id="missionUpdated" data-i18n="mission.never"></p>
      </section>
    </div>`;
  document.querySelector('footer.statusbar')?.before(root);

  const nodes = {
    daemon: $('msDaemonState'), daemonAge: $('msDaemonAge'),
    active: $('msTasksActive'), queued: $('msTasksQueued'), failed: $('msTasksFailed'),
    approvals: $('msApprovals'), approvalTone: $('msApprovalTone'),
    cpu: $('msCpu'), ram: $('msRam'), disk: $('msDisk'), gpu: $('msGpu'),
    tokensIn: $('msTokensIn'), tokensOut: $('msTokensOut'), cost: $('msCost'),
    agents: $('msAgents'), agentsHint: $('msAgentsHint'),
    feed: $('msFeed'), feedMeta: $('msActivityMeta'), updated: $('missionUpdated'),
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

  function paintState(payload) {
    const daemon = payload.daemon || {};
    const stateText = daemon.state || 'unknown';
    /* Known daemon states get a localized word; an unrecognised value from a
       newer server is shown verbatim instead of being guessed at. */
    const knownStates = ['running', 'idle', 'stopped', 'killed', 'paused', 'unknown'];
    set(nodes.daemon, knownStates.includes(stateText) ? t('mission.state.' + stateText) : stateText, stateText);
    /* The snapshot carries the daemon state and the lock flag, not a heartbeat
       timestamp, so the tile reports the age of the reading itself. */
    const observed = Number(payload.observed_at);
    const lock = daemon.lock_held;
    if (Number.isFinite(observed) && observed > 0) {
      set(nodes.daemonAge, t('mission.snapshot', {age: age(Math.max(0, Math.floor(Date.now() / 1000) - observed))}));
    } else {
      set(nodes.daemonAge, t('mission.never'));
    }
    const daemonHint = document.getElementById('msDaemonHint');
    if (daemonHint) {
      daemonHint.textContent = typeof lock === 'boolean'
        ? t('mission.lock', {value: lock ? t('mission.yes') : t('mission.no')})
        : t('mission.daemonHint');
    }

    const jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
    const running = jobs.filter((job) => job.status === 'running').length;
    const queued = jobs.filter((job) => job.status === 'queued').length;
    const cutoff = Math.floor(Date.now() / 1000) - 86400;
    const failed = jobs.filter((job) => ['failed', 'error'].includes(job.status) && Number(job.created || 0) >= cutoff).length;
    set(nodes.active, String(running));
    set(nodes.queued, String(queued));
    set(nodes.failed, String(failed));

    const pending = Number(payload.pending_actions_total) || 0;
    set(nodes.approvals, String(pending));
    set(nodes.approvalTone, pending ? (pending > 2 ? 'high' : 'medium') : 'low', pending ? 'warn' : 'ok');
    if (nodes.approvalTone) nodes.approvalTone.textContent = pending > 2 ? t('mission.toneHigh') : pending ? t('mission.toneMedium') : t('mission.toneClear');

    const hardware = payload.hardware || {};
    set(nodes.cpu, percent(hardware.cpu_percent));
    set(nodes.ram, percent(hardware.memory_percent));
    set(nodes.disk, bytes(hardware.disk_free_bytes));

    const usage = payload.usage && payload.usage.day ? payload.usage.day : null;
    const tokensIn = usage ? Number(usage.actual_input_tokens) || 0 : null;
    const tokensOut = usage ? Number(usage.actual_output_tokens) || 0 : null;
    set(nodes.tokensIn, tokensIn === null ? unavailable() : tokensIn.toLocaleString());
    set(nodes.tokensOut, tokensOut === null ? unavailable() : tokensOut.toLocaleString());
    const cost = usage ? usage.actual_cost_usd : null;
    if (typeof cost === 'number' && Number.isFinite(cost)) set(nodes.cost, `${t('mission.cost')}: $${cost.toFixed(4)}`);
    else set(nodes.cost, `${t('mission.cost')}: ${t('mission.costUnavailable')}`);
  }

  function paintFeed(tasks) {
    const rows = (Array.isArray(tasks) ? tasks : [])
      .filter((task) => task && task.task_id)
      .sort((a, b) => (b.created || 0) - (a.created || 0))
      .slice(0, 8);
    nodes.feed.replaceChildren();
    if (!rows.length) {
      const li = document.createElement('li');
      li.className = 'mission-empty';
      li.textContent = t('mission.activityEmpty');
      nodes.feed.append(li);
      set(nodes.feedMeta, '0');
      return;
    }
    for (const task of rows) {
      const li = document.createElement('li');
      li.dataset.status = task.status || 'unknown';
      const dot = document.createElement('i');
      dot.className = 'dot';
      dot.setAttribute('aria-hidden', 'true');
      const body = document.createElement('div');
      const title = document.createElement('b');
      title.textContent = String(task.task || '').slice(0, 120) || task.task_id;
      const meta = document.createElement('span');
      meta.textContent = [task.status, task.role, task.agent].filter(Boolean).join(' · ');
      body.append(title, meta);
      const time = document.createElement('time');
      time.textContent = task.created ? new Date(task.created * 1000).toLocaleTimeString() : '—';
      li.append(dot, body, time);
      li.addEventListener('click', () => {
        nav('run');
        window.setTimeout(() => { try { window.nexusAttachTask?.(task.task_id); } catch { /* best effort */ } }, 120);
      });
      li.tabIndex = 0;
      li.setAttribute('role', 'button');
      li.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') li.click(); });
      nodes.feed.append(li);
    }
    set(nodes.feedMeta, String(rows.length));
  }

  function paintAgents(stats) {
    const agents = stats && stats.agents ? stats.agents : null;
    if (!agents) { set(nodes.agents, unavailable()); set(nodes.agentsHint, t('mission.reach')); return; }
    const names = Object.keys(agents);
    const withKeys = names.filter((name) => agents[name]?.key_present).length;
    set(nodes.agents, `${withKeys} / ${names.length}`);
    set(nodes.agentsHint, t('mission.reach'));
  }

  function paintHealth(values) {
    for (const [chip, value] of Object.entries(values)) {
      const node = document.querySelector(`#msHealth [data-chip="${chip}"] b`);
      if (!node) continue;
      node.textContent = value.text;
      node.dataset.state = value.state || 'unknown';
    }
  }

  async function loadHealth() {
    const values = {};
    const [ollama, github, desktop] = await Promise.allSettled([
      gui.api('/api/ollama'),
      gui.api('/api/github'),
      gui.api('/api/workbench/desktop'),
    ]);
    if (ollama.status === 'fulfilled' && ollama.value && !ollama.value.error) {
      const models = Array.isArray(ollama.value.models) ? ollama.value.models : [];
      values.ollama = {text: models.length ? `${models.length} models` : t('mission.down'), state: models.length ? 'ok' : 'warn'};
    } else {
      values.ollama = {text: t('mission.down'), state: 'off'};
    }
    if (github.status === 'fulfilled' && github.value && !github.value.error) {
      const value = github.value;
      const available = value.available ?? value.installed ?? false;
      values.github = {text: available ? (value.user || 'connected') : (value.reason || t('mission.disabled')), state: available ? 'ok' : 'off'};
    } else {
      values.github = {text: t('mission.notChecked'), state: 'unknown'};
    }
    if (desktop.status === 'fulfilled') {
      const payload = desktop.value?.data || desktop.value || {};
      const policies = payload.policies || {};
      const describe = (row) => {
        if (!row || row.error) return {text: t('mission.notChecked'), state: 'unknown'};
        return {text: row.settings?.enabled ? t('mission.enabled') : t('mission.disabled'), state: row.settings?.enabled ? 'ok' : 'off'};
      };
      values.mcp = describe(policies.mcp);
      values.telegram = describe(policies.telegram);
    } else {
      values.mcp = {text: t('mission.notChecked'), state: 'unknown'};
      values.telegram = {text: t('mission.notChecked'), state: 'unknown'};
    }
    paintHealth(values);
  }

  function stamp() {
    if (nodes.updated) nodes.updated.textContent = t('mission.updated', {time: new Date().toLocaleTimeString()});
  }

  let busy = false;
  async function load() {
    if (busy) return;
    busy = true;
    try {
      const [state, metrics, stats, tasks] = await Promise.allSettled([
        gui.api('/api/operations/state'),
        gui.api('/api/workbench/metrics'),
        gui.api('/api/stats'),
        gui.api('/api/tasks'),
      ]);
      if (state.status === 'fulfilled') paintState(state.value);
      if (metrics.status === 'fulfilled') {
        const value = metrics.value?.data || metrics.value || {};
        const gpu = value.gpu;
        set(nodes.gpu, t('mission.gpu', {value: gpu ? String(gpu).split('\n')[0].slice(0, 70) : t('mission.unavailable')}));
      }
      if (stats.status === 'fulfilled') paintAgents(stats.value);
      if (tasks.status === 'fulfilled') paintFeed(tasks.value);
      paintHealth({
        server: {text: document.getElementById('metaUplink')?.textContent === 'online' ? t('mission.up') : t('mission.notChecked'),
          state: document.getElementById('metaUplink')?.textContent === 'online' ? 'ok' : 'unknown'},
        sse: {text: document.getElementById('sbSse')?.dataset.state === 'live' ? t('mission.streaming') : t('mission.idle'),
          state: document.getElementById('sbSse')?.dataset.state === 'live' ? 'ok' : 'off'},
      });
      stamp();
      loadHealth();
    } finally {
      busy = false;
    }
  }

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
      const first = nodes.feed.querySelector('li[data-status]');
      if (!first) { window.nexusToast?.(t('mission.noRun'), {tone: 'info'}); return; }
      first.click();
    } else if (act === 'refresh') {
      load();
      window.nexusToast?.(t('mission.refreshed'), {tone: 'done', timeout: 1800});
    }
  });

  /* ── lifecycle ─────────────────────────────────────────────────────── */
  let timer = null;
  const startPolling = () => {
    if (timer) return;
    timer = window.setInterval(() => { if (document.visibilityState === 'visible') load(); }, 15000);
  };
  const stopPolling = () => { if (timer) { window.clearInterval(timer); timer = null; } };

  window.addEventListener('nexus:view', (event) => {
    if (event.detail === 'mission') { load(); startPolling(); }
    else stopPolling();
  });
  window.addEventListener('nexus:sse', () => { if (gui.view === 'mission') load(); });
  i18n.onChange(() => { i18n.apply(root); load(); });

  gui.ready.then(() => {
    if (gui.view === 'mission') { load(); startPolling(); }
  });

  window.nexusMission = {load, root};
})();
