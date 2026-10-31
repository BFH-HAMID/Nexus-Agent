/* Workflow Builder: the canvas, the versions, the runs and the schedules.

   Everything here is a *view* over machinery that already exists. Saving calls
   the Studio's immutable version store, running calls the same DAG engine the
   Run button calls, and every write node still passes its own approval gate.
   This page adds no second executor and no shortcut around a token.

   Schedules are the one piece this page owns: a persisted list in
   `.nexus/workflows/schedules.json`, ticked by the server. A window that opens
   while the server is down is counted as missed, never replayed. */
(() => {
  'use strict';

  const gui = window.nexusGui;
  const i18n = window.nexusI18n;
  if (!gui || !i18n) return;
  const t = i18n.t;
  const $ = (id) => document.getElementById(id);

  const TABS = ['workflows', 'builder', 'runs', 'schedules'];
  const KINDS = ['agent', 'research', 'reviewer', 'verification', 'approval'];
  const CONDITIONS = ['success', 'failure', 'always'];
  const STATUS_TONE = {running: 'accent', done: 'ok', skipped: 'warn', failed: 'error', error: 'error',
    cancelled: 'warn', pending: '', notstarted: '', 'not-started': 'warn'};
  const BUDGET_DEFAULTS = {turns: 24, tools: 100, tokens: 32000, seconds: 300};
  const BUDGET_CEILING = {turns: 100, tools: 1000, tokens: 1000000, seconds: 3600};

  const root = document.createElement('section');
  root.id = 'view-workflows';
  root.className = 'gui-modules workflows-page';
  root.dataset.guiView = 'workflows';
  root.hidden = gui.view !== 'workflows';
  root.innerHTML = `
    <header class="workflows-head">
      <div>
        <p class="workflows-eyebrow" data-i18n="workflows.eyebrow">Workflow Builder</p>
        <h1 data-i18n="workflows.title">Pipelines you can see, review and schedule</h1>
        <p class="workflows-sub" data-i18n="workflows.subtitle"></p>
      </div>
      <div class="workflows-quick" role="group" aria-label="Workflow actions">
        <button class="btn" type="button" id="wfRefresh"><span class="btn-label" data-i18n="workflows.refresh"></span></button>
        <button class="btn ghost" type="button" id="wfStudio"><span class="btn-label" data-i18n="workflows.openStudio"></span></button>
        <span class="workflows-stamp" id="wfUpdated" data-i18n="workflows.never"></span>
      </div>
    </header>

    <p class="workflows-banner" id="wfError" role="alert" hidden></p>

    <div class="workflows-toolbar">
      <div class="workflows-tabs" role="tablist" aria-label="Workflow Builder sections">
        <button type="button" role="tab" id="wfTabWorkflows" data-wf-tab="workflows" aria-selected="true" aria-controls="wfPanelWorkflows" data-i18n="workflows.tabWorkflows"></button>
        <button type="button" role="tab" id="wfTabBuilder" data-wf-tab="builder" aria-selected="false" aria-controls="wfPanelBuilder" data-i18n="workflows.tabBuilder"></button>
        <button type="button" role="tab" id="wfTabRuns" data-wf-tab="runs" aria-selected="false" aria-controls="wfPanelRuns" data-i18n="workflows.tabRuns"></button>
        <button type="button" role="tab" id="wfTabSchedules" data-wf-tab="schedules" aria-selected="false" aria-controls="wfPanelSchedules" data-i18n="workflows.tabSchedules"></button>
      </div>
      <span class="workflows-chip" id="wfTotal" role="status" aria-live="polite">—</span>
    </div>

    <div class="workflows-body">
      <section class="workflows-panel" id="wfPanelWorkflows" role="tabpanel" aria-labelledby="wfTabWorkflows" data-wf-panel="workflows">
        <div class="workflows-kpis" id="wfKpis"></div>
        <div class="workflows-split">
          <div class="workflows-column">
            <div class="workflows-cardhead">
              <h2 data-i18n="workflows.familiesTitle"></h2>
              <div class="workflows-actions">
                <button class="btn primary" type="button" id="wfNew"><span class="btn-label" data-i18n="workflows.newWorkflow"></span></button>
              </div>
            </div>
            <p class="workflows-help" data-i18n="workflows.familiesLead"></p>
            <div class="workflows-list" id="wfFamilies"></div>
            <p class="workflows-empty" id="wfFamiliesEmpty" hidden data-i18n="workflows.familiesEmpty"></p>
          </div>
          <aside class="workflows-side">
            <article class="workflows-card">
              <div class="workflows-cardhead"><h2 data-i18n="workflows.rulesTitle"></h2><span class="workflows-chip" id="wfModeStamp">—</span></div>
              <ul class="workflows-plain" id="wfNotes"></ul>
            </article>
            <article class="workflows-card">
              <div class="workflows-cardhead"><h2 data-i18n="workflows.limitsTitle"></h2><span class="workflows-chip" id="wfStoreStamp">—</span></div>
              <dl class="workflows-facts" id="wfLimits"></dl>
              <p class="workflows-help" data-i18n="workflows.limitsNote"></p>
            </article>
          </aside>
        </div>
      </section>

      <section class="workflows-panel" id="wfPanelBuilder" role="tabpanel" aria-labelledby="wfTabBuilder" data-wf-panel="builder" hidden>
        <div class="workflows-card workflows-builderbar">
          <div class="workflows-draft">
            <label class="workflows-field">
              <span data-i18n="workflows.draftName"></span>
              <input type="text" id="wfDraftName" maxlength="100" autocomplete="off" spellcheck="false" data-i18n-aria="workflows.draftNameAria">
            </label>
            <span class="workflows-chip" id="wfDraftSource">—</span>
            <label class="workflows-field workflows-loadpick">
              <span data-i18n="workflows.loadVersion"></span>
              <select id="wfLoadPick"></select>
            </label>
          </div>
          <div class="workflows-actions">
            <button class="btn" type="button" id="wfLoad"><span class="btn-label" data-i18n="workflows.loadVersion"></span></button>
            <button class="btn" type="button" id="wfValidate"><span class="btn-label" data-i18n="workflows.validate"></span></button>
            <button class="btn" type="button" id="wfSave"><span class="btn-label" data-i18n="workflows.save"></span></button>
            <button class="btn ghost" type="button" id="wfRunDraft"><span class="btn-label" data-i18n="workflows.run"></span></button>
          </div>
        </div>

        <div class="workflows-addrow" id="wfAddRow" role="group" aria-label="Add a node"></div>
        <p class="workflows-status" id="wfBuilderStatus" role="status" aria-live="polite"></p>
        <div id="wfConfirmHost"></div>
        <p class="workflows-help" id="wfValidateOut" hidden></p>

        <div class="workflows-canvaswrap" id="wfCanvasWrap" tabindex="0" role="group" aria-label="Workflow graph, scrolls sideways">
          <div class="workflows-canvas" id="wfCanvas" aria-hidden="true"></div>
        </div>
        <p class="workflows-help" data-i18n="workflows.canvasNote"></p>

        <div class="workflows-split workflows-canvas-split">
          <div class="workflows-column">
            <ol class="workflows-nodeList" id="wfNodeList" aria-label="Workflow nodes"></ol>
          </div>
          <aside class="workflows-side">
            <article class="workflows-card" id="wfInspector">
              <h2 data-i18n="workflows.inspectorTitle"></h2>
              <p class="workflows-empty" data-i18n="workflows.inspectorEmpty"></p>
            </article>
            <article class="workflows-card">
              <div class="workflows-cardhead"><h2 data-i18n="workflows.diffTitle"></h2></div>
              <div class="workflows-inline">
                <label class="workflows-field"><span data-i18n="workflows.diffFrom"></span><select id="wfDiffA"></select></label>
                <label class="workflows-field"><span data-i18n="workflows.diffTo"></span><select id="wfDiffB"></select></label>
                <button class="btn" type="button" id="wfDiffGo"><span class="btn-label" data-i18n="workflows.diffGo"></span></button>
              </div>
              <div class="workflows-diff" id="wfDiffOut"></div>
            </article>
          </aside>
        </div>
      </section>

      <section class="workflows-panel" id="wfPanelRuns" role="tabpanel" aria-labelledby="wfTabRuns" data-wf-panel="runs" hidden>
        <div class="workflows-split">
          <div class="workflows-column">
            <div class="workflows-cardhead">
              <h2 data-i18n="workflows.runsTitle"></h2>
              <span class="workflows-chip" id="wfRunCount">—</span>
            </div>
            <p class="workflows-help" data-i18n="workflows.runsLead"></p>
            <div class="workflows-list" id="wfRuns"></div>
            <p class="workflows-empty" id="wfRunsEmpty" hidden data-i18n="workflows.runsEmpty"></p>
          </div>
          <aside class="workflows-side">
            <article class="workflows-card" id="wfRunDetail">
              <h2 data-i18n="workflows.runDetailTitle"></h2>
              <p class="workflows-empty" data-i18n="workflows.runDetailEmpty"></p>
            </article>
            <article class="workflows-card">
              <div class="workflows-cardhead"><h2 data-i18n="workflows.compareTitle"></h2></div>
              <div class="workflows-inline">
                <label class="workflows-field"><span data-i18n="workflows.diffFrom"></span><select id="wfCompareA"></select></label>
                <label class="workflows-field"><span data-i18n="workflows.diffTo"></span><select id="wfCompareB"></select></label>
                <button class="btn" type="button" id="wfCompareGo"><span class="btn-label" data-i18n="workflows.diffGo"></span></button>
              </div>
              <div class="workflows-diff" id="wfRunDiffOut"></div>
            </article>
          </aside>
        </div>
      </section>

      <section class="workflows-panel" id="wfPanelSchedules" role="tabpanel" aria-labelledby="wfTabSchedules" data-wf-panel="schedules" hidden>
        <div class="workflows-kpis" id="wfScheduler"></div>
        <div class="workflows-split">
          <div class="workflows-column">
            <div class="workflows-cardhead">
              <h2 data-i18n="workflows.schedulesTitle"></h2>
              <span class="workflows-chip" id="wfScheduleCount">—</span>
            </div>
            <p class="workflows-help" data-i18n="workflows.schedulesLead"></p>
            <div id="wfScheduleConfirm"></div>
            <div class="workflows-list" id="wfSchedules"></div>
            <p class="workflows-empty" id="wfSchedulesEmpty" hidden data-i18n="workflows.schedulesEmpty"></p>
          </div>
          <aside class="workflows-side">
            <article class="workflows-card">
              <div class="workflows-cardhead"><h2 data-i18n="workflows.scheduleNewTitle"></h2></div>
              <form class="workflows-form" id="wfScheduleForm" novalidate>
                <label class="workflows-field"><span data-i18n="workflows.scheduleVersion"></span><select id="wfSchWorkflow"></select></label>
                <label class="workflows-field"><span data-i18n="workflows.scheduleKind"></span>
                  <select id="wfSchKind">
                    <option value="interval" data-i18n="workflows.kindInterval"></option>
                    <option value="cron" data-i18n="workflows.kindCron"></option>
                    <option value="manual" data-i18n="workflows.kindManual"></option>
                  </select>
                </label>
                <label class="workflows-field" id="wfSchMinutesField"><span data-i18n="workflows.scheduleMinutes"></span>
                  <input type="number" id="wfSchMinutes" min="5" max="10080" step="5" value="30" inputmode="numeric">
                </label>
                <label class="workflows-field" id="wfSchCronField" hidden><span data-i18n="workflows.scheduleCron"></span>
                  <input type="text" id="wfSchCron" value="0 9 * * 1-5" autocomplete="off" spellcheck="false" data-i18n-aria="workflows.scheduleCronAria">
                </label>
                <label class="workflows-inline workflows-check"><input type="checkbox" id="wfSchEnabled" checked><span data-i18n="workflows.scheduleEnabled"></span></label>
                <ul class="workflows-plain" id="wfSchPreview"></ul>
                <div class="workflows-actions">
                  <button class="btn" type="button" id="wfSchPreviewBtn"><span class="btn-label" data-i18n="workflows.preview"></span></button>
                  <button class="btn primary" type="submit" id="wfSchSave"><span class="btn-label" data-i18n="workflows.scheduleSave"></span></button>
                </div>
                <p class="workflows-status" id="wfSchStatus" role="status" aria-live="polite"></p>
                <div id="wfSchConfirm"></div>
              </form>
            </article>
            <article class="workflows-card">
              <h2 data-i18n="workflows.schedulerTitle"></h2>
              <dl class="workflows-facts" id="wfSchedulerFacts"></dl>
              <p class="workflows-help" data-i18n="workflows.schedulerNote"></p>
            </article>
          </aside>
        </div>
      </section>
    </div>
  `;
  (document.querySelector('main.deck') || document.body).after(root);

  const state = {
    tab: 'workflows',
    overview: null, families: [], runs: [], schedules: null, scheduler: null,
    inspected: [], run: null, revision: '', familyLimit: 8,
    draft: {name: '', nodes: [], family: '', sourceId: '', sourceVersion: null,
            order: [], layout: null, writes: [], validatedAt: 0},
    selected: '',
    busy: false, lastRead: 0, diff: null, runDiff: null, preview: [],
  };

  /* ── small helpers ──────────────────────────────────────────────────── */
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const message = (error) => error?.payload?.error?.message || error?.message || String(error);
  const clock = (value) => {
    if (!value) return '—';
    const date = new Date(value * 1000);
    return Number.isNaN(date.getTime()) ? '—'
      : date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  };
  const stamp = (value) => {
    if (!value) return '—';
    const date = new Date(value * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
  };
  const utcTitle = (value) => (value ? new Date(value * 1000).toISOString() : '');
  function relative(value) {
    if (!value) return '—';
    const seconds = Math.round(value - Date.now() / 1000);
    const size = Math.abs(seconds);
    const unit = size < 60 ? 'seconds' : size < 3600 ? 'minutes' : 'hours';
    const count = size < 60 ? size : size < 3600 ? Math.round(size / 60) : Math.round(size / 3600);
    const key = seconds >= 0 ? `workflows.in.${unit}` : `workflows.ago.${unit}`;
    return t(key, {count: Math.max(1, count)});
  }
  function chip(text, tone) {
    const span = el('span', 'workflows-chip', text);
    if (tone) span.dataset.tone = tone;
    return span;
  }
  function setStatus(node, text, tone = '') {
    if (!node) return;
    node.textContent = text;
    node.dataset.tone = tone;
    if (text) window.setTimeout(() => { if (node.textContent === text) { node.textContent = ''; node.dataset.tone = ''; } }, 9000);
  }
  function facts(host, rows) {
    if (!host) return;
    host.replaceChildren();
    for (const [key, value] of rows) {
      host.append(el('dt', '', key), el('dd', '', value === null || value === undefined || value === '' ? '—' : String(value)));
    }
  }
  function line(text) {
    const li = el('li', '', text);
    return li;
  }
  const statusTone = (status) => STATUS_TONE[String(status || '')] ?? '';
  const statusLabel = (status) => {
    const key = `workflows.status.${status}`;
    const value = t(key);
    return value === key ? String(status || '—') : value;
  };
  function noteText(note) {
    const key = `workflows.note.${note.id}`;
    const value = t(key);
    return value === key ? String(note.text || note.id) : value;
  }
  const nodeById = (ident) => state.draft.nodes.find((node) => node.id === ident);
  /* A background poll must never rebuild a subtree somebody is typing into. */
  function isTyping() {
    const active = document.activeElement;
    return !!active && root.contains(active) && ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName);
  }
  function describeTrigger(trigger, fallback) {
    if (!trigger) return fallback || '—';
    if (trigger.kind === 'interval') return t('workflows.describe.interval', {minutes: trigger.minutes});
    if (trigger.kind === 'cron') return t('workflows.describe.cron', {cron: trigger.cron});
    if (trigger.kind === 'manual') return t('workflows.describe.manual');
    return fallback || '—';
  }

  /* Server layout maths, mirrored so the draft can be drawn before saving. */
  function layoutOf(nodes) {
    const depth = {};
    const remaining = new Map(nodes.map((node) => [node.id, new Set(node.depends_on || [])]));
    for (let pass = 0; pass <= nodes.length; pass += 1) {
      let progress = false;
      for (const [ident, deps] of [...remaining.entries()]) {
        if ([...deps].every((dep) => dep in depth)) {
          depth[ident] = 1 + Math.max(-1, ...[...deps].map((dep) => depth[dep]));
          remaining.delete(ident);
          progress = true;
        }
      }
      if (!remaining.size || !progress) break;
    }
    for (const ident of remaining.keys()) depth[ident] = 0;
    const layers = {};
    for (const node of nodes) (layers[depth[node.id]] ||= []).push(node.id);
    const ordered = [];
    for (const index of Object.keys(layers).map(Number).sort((a, b) => a - b)) {
      layers[index].forEach((ident, row) => ordered.push({id: ident, layer: index, row, x: index * 264 + 20, y: row * 132 + 20}));
    }
    const edges = nodes.flatMap((node) => (node.depends_on || []).map((dep) => ({from: dep, to: node.id})));
    return {nodes: ordered, edges, layers: Object.keys(layers).length,
      width: Math.max(0, ...ordered.map((item) => item.x)) + 220,
      height: Math.max(0, ...ordered.map((item) => item.y)) + 132};
  }

  /* ── the canvas: edges as SVG, cards positioned on the server's grid ── */
  const COL_W = 264;
  const ROW_H = 132;
  const PAD = 20;
  const CARD_W = 200;
  const CARD_H = 64;

  /* The server owns the layers; the canvas draws them left to right. A graph
     wider than the column scrolls sideways rather than folding into a shape
     that no longer reads as a pipeline. */
  function paintCanvas(host, layout, decorate) {
    if (!host) return;
    const nodes = layout?.nodes || [];
    const width = Math.max(240, layout?.width || 0);
    const height = Math.max(160, layout?.height || 0);
    host.style.setProperty('--wf-width', `${width}px`);
    host.style.setProperty('--wf-height', `${height}px`);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'workflows-edges');
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('preserveAspectRatio', 'xMinYMin meet');
    const positions = new Map(nodes.map((item) => [item.id, item]));
    /* Arrowheads are drawn as plain triangles: a marker would need its own defs
       per canvas and silently disappears under some headless renderers. */
    const arrow = (x, y) => {
      const tip = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      tip.setAttribute('d', `M ${x - 9} ${y - 4.5} L ${x} ${y} L ${x - 9} ${y + 4.5} z`);
      tip.setAttribute('class', 'workflows-arrowhead');
      svg.append(tip);
    };
    for (const edge of layout?.edges || []) {
      const from = positions.get(edge.from);
      const to = positions.get(edge.to);
      if (!from || !to) continue;
      const x1 = from.x + CARD_W;
      const y1 = from.y + CARD_H / 2;
      const x2 = to.x - 10;
      const y2 = to.y + CARD_H / 2;
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', Math.abs(y1 - y2) < 2
        ? `M ${x1} ${y1} L ${x2} ${y2}`
        : `M ${x1} ${y1} C ${x1 + (x2 - x1) / 2} ${y1}, ${x1 + (x2 - x1) / 2} ${y2}, ${x2} ${y2}`);
      path.setAttribute('class', 'workflows-edge');
      svg.append(path);
      arrow(to.x - 1, y2);
    }
    host.replaceChildren(svg);
    for (const item of nodes) {
      const spec = decorate(item) || {};
      const card = el('button', 'workflows-node');
      card.type = 'button';
      card.tabIndex = -1;
      card.style.left = `${item.x}px`;
      card.style.top = `${item.y}px`;
      card.dataset.node = item.id;
      if (spec.status) card.dataset.status = spec.status;
      if (spec.selected) card.dataset.selected = 'true';
      card.append(el('b', '', item.id));
      card.append(el('span', 'workflows-nodekind', spec.kind || ''));
      if (spec.meta) card.append(el('span', 'workflows-nodemeta', spec.meta));
      if (spec.onclick) card.onclick = spec.onclick;
      host.append(card);
    }
    if (!nodes.length) host.append(el('p', 'workflows-empty', t('workflows.canvasEmpty')));
  }

  /* ── tabs ───────────────────────────────────────────────────────────── */
  function switchTab(name) {
    if (!TABS.includes(name)) return;
    state.tab = name;
    root.querySelectorAll('[data-wf-tab]').forEach((node) => node.setAttribute('aria-selected', String(node.dataset.wfTab === name)));
    root.querySelectorAll('[data-wf-panel]').forEach((node) => { node.hidden = node.dataset.wfPanel !== name; });
    if (name === 'workflows') loadFamilies().catch(() => {});
    if (name === 'runs') loadRuns().catch(() => {});
    if (name === 'schedules') loadSchedules().catch(() => {});
    if (name === 'builder') paintBuilder();
    schedule();
  }

  root.querySelectorAll('[data-wf-tab]').forEach((node) => { node.onclick = () => switchTab(node.dataset.wfTab); });
  root.querySelector('[role="tablist"]').addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...root.querySelectorAll('[data-wf-tab]')];
    const index = tabs.findIndex((tab) => tab.dataset.wfTab === state.tab);
    const next = event.key === 'Home' ? tabs[0]
      : event.key === 'End' ? tabs[tabs.length - 1]
        : tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
    next.focus();
    switchTab(next.dataset.wfTab);
  });

  /* ── overview ───────────────────────────────────────────────────────── */
  function paintOverview() {
    const body = state.overview;
    if (!body) return;
    const host = $('wfKpis');
    host.replaceChildren();
    const kpis = [
      ['workflows.kpiFamilies', body.families, ''],
      ['workflows.kpiVersions', body.versions, ''],
      ['workflows.kpiRuns', body.runs, ''],
      ['workflows.kpiSchedules', `${body.enabled_schedules}/${body.schedules}`, body.enabled_schedules ? 'ok' : ''],
      ['workflows.kpiNext', body.next_run_at ? relative(body.next_run_at) : t('workflows.none'), body.next_run_at ? 'accent' : ''],
    ];
    for (const [key, value, tone] of kpis) {
      const card = el('div', 'workflows-count');
      if (tone) card.dataset.tone = tone;
      card.append(el('b', '', String(value)), el('span', '', t(key)));
      if (key === 'workflows.kpiNext' && body.next_run_at) card.title = stamp(body.next_run_at);
      host.append(card);
    }
    const blocked = [body.busy ? t('workflows.busy') : '', body.latch?.stopped ? t('workflows.latched') : '',
      body.queue_paused ? t('workflows.paused') : ''].filter(Boolean);
    $('wfModeStamp').textContent = body.demo ? t('workflows.demoMode') : t('workflows.liveMode');
    $('wfModeStamp').dataset.tone = body.demo ? 'warn' : 'ok';
    $('wfStoreStamp').textContent = body.store || '—';
    $('wfStoreStamp').title = body.store || '';
    $('wfNotes').replaceChildren(...(body.notes || []).map((note) => line(noteText(note))));
    if (blocked.length) $('wfNotes').append(line(t('workflows.blockedNow', {reasons: blocked.join(', ')})));
    const limits = body.limits || {};
    facts($('wfLimits'), [
      [t('workflows.limitSchedules'), limits.schedules],
      [t('workflows.limitInterval'), (limits.interval || []).join('–')],
      [t('workflows.limitRuns'), limits.runs_kept],
      [t('workflows.limitNodes'), limits.nodes],
      [t('workflows.tokenSave'), body.tokens?.save],
      [t('workflows.tokenSchedule'), body.tokens?.schedule],
      [t('workflows.tokenRun'), body.tokens?.run],
    ]);
    setBanner(body.error ? t('workflows.storeDamaged', {error: body.error}) : '');
    setTotal();
  }

  function setTotal() {
    const body = state.overview;
    if (!body) return;
    const waiting = (state.inspected || []).filter((run) => run.status === 'running').length;
    $('wfTotal').textContent = waiting ? t('workflows.runningCount', {count: waiting})
      : t('workflows.savedCount', {count: body.versions});
    $('wfTotal').dataset.tone = waiting ? 'accent' : '';
  }

  function setBanner(text) {
    const node = $('wfError');
    node.hidden = !text;
    node.textContent = text || '';
  }

  async function loadOverview() {
    state.overview = await gui.api('/api/workflows/overview').then((body) => body.data);
    state.lastRead = state.overview.observed_at;
    $('wfUpdated').textContent = t('workflows.updated', {time: clock(state.lastRead)});
    paintOverview();
  }

  /* ── versions ───────────────────────────────────────────────────────── */
  async function loadFamilies() {
    const body = await gui.api('/api/workflows/families').then((payload) => payload.data);
    state.families = body.families || [];
    paintFamilies();
    paintVersionPickers();
    paintSchedulePickers();
    setTotal();
  }

  function paintFamilies() {
    const host = $('wfFamilies');
    host.replaceChildren();
    const shown = state.families.slice(0, state.familyLimit);
    for (const family of shown) {
      const card = el('article', 'workflows-item');
      const head = el('div', 'workflows-itemhead');
      head.append(el('h3', 'workflows-itemtitle', family.name || family.family));
      const chips = el('div', 'workflows-chips');
      chips.append(chip(t('workflows.versionsCount', {count: family.versions.length}), 'accent'));
      chips.append(chip(t('workflows.latestVersion', {version: family.latest.version ?? '—'}), ''));
      head.append(chips);
      card.append(head, el('p', 'workflows-meta', t('workflows.updatedAt', {time: stamp(family.latest.updated_at)})));
      const actions = el('div', 'workflows-actions');
      const open = el('button', 'btn', t('workflows.openVersion'));
      open.type = 'button';
      open.dataset.wfOpen = family.latest.id;
      open.onclick = () => openInBuilder(family.latest.id);
      const compare = el('button', 'btn ghost', t('workflows.compareVersions'));
      compare.type = 'button';
      compare.onclick = () => {
        const select = $('wfDiffA');
        select.value = family.versions[family.versions.length - 1]?.id || '';
        $('wfDiffB').value = family.latest.id;
        switchTab('builder');
        runVersionDiff();
      };
      actions.append(open, compare);
      card.append(actions);

      const list = el('ol', 'workflows-versions');
      const versionRow = (version) => {
        const row = el('li', 'workflows-versionRow');
        const button = el('button', 'workflows-version');
        button.type = 'button';
        button.onclick = () => openInBuilder(version.id);
        button.title = version.id;
        button.append(el('span', '', `${t('workflows.versionTag', {version: version.version ?? '—'})} · ${stamp(version.updated_at)}`),
          el('span', 'workflows-meta', t('workflows.nodesCount', {count: version.nodes})));
        row.append(button);
        row.append(el('code', 'workflows-id', version.id));
        return row;
      };
      const shown = family.versions.slice(0, 4);
      for (const version of shown) list.append(versionRow(version));
      if (family.versions.length > shown.length) {
        /* older versions stay reachable without letting one family fill the page */
        const more = el('details', 'workflows-more');
        more.append(el('summary', '', t('workflows.moreVersions', {count: family.versions.length - shown.length})));
        const rest = el('ol', 'workflows-versions');
        for (const version of family.versions.slice(shown.length)) rest.append(versionRow(version));
        more.append(rest);
        const wrapper = el('li');
        wrapper.append(more);
        list.append(wrapper);
      }
      card.append(list);
      host.append(card);
    }
    $('wfFamiliesEmpty').hidden = state.families.length > 0;
    if (state.families.length > shown.length) {
      const more = el('button', 'btn ghost workflows-showall',
        t('workflows.showAllFamilies', {count: state.families.length}));
      more.type = 'button';
      more.onclick = () => { state.familyLimit = Number.MAX_SAFE_INTEGER; paintFamilies(); };
      host.append(more);
    } else if (state.familyLimit > 8 && state.families.length > 8) {
      const less = el('button', 'btn ghost workflows-showall', t('workflows.showFewerFamilies'));
      less.type = 'button';
      less.onclick = () => { state.familyLimit = 8; paintFamilies(); };
      host.append(less);
    }
  }

  function paintVersionPickers() {
    const options = [];
    for (const family of state.families) {
      for (const version of family.versions) {
        options.push({id: version.id, label: `${family.name || family.family} · v${version.version ?? '?'} · ${stamp(version.updated_at)}`});
      }
    }
    for (const select of [$('wfLoadPick'), $('wfDiffA'), $('wfDiffB'), $('wfCompareA'), $('wfCompareB'), $('wfSchWorkflow')]) {
      if (!select) continue;
      const previous = select.value;
      select.replaceChildren();
      select.append(new Option(t('workflows.pick'), ''));
      for (const option of options) select.append(new Option(option.label, option.id));
      if (options.some((option) => option.id === previous)) select.value = previous;
    }
  }

  async function openInBuilder(identifier) {
    setBanner('');
    try {
      const detail = await gui.api(`/api/workflows/version?id=${encodeURIComponent(identifier)}`).then((body) => body.data);
      state.draft = {
        name: detail.name, nodes: (detail.definition.nodes || []).map((node) => ({...node})),
        family: detail.family || '', sourceId: detail.id, sourceVersion: detail.version,
        order: detail.order || [], layout: detail.layout, writes: detail.writes || [], validatedAt: 0,
      };
      state.selected = state.draft.nodes[0]?.id || '';
      $('wfDraftName').value = state.draft.name;
      switchTab('builder');
      paintBuilder();
      setStatus($('wfBuilderStatus'), t('workflows.loaded', {id: detail.id, version: detail.version}), 'ok');
    } catch (error) {
      setBanner(message(error));
    }
  }

  /* ── the builder ────────────────────────────────────────────────────── */
  function newDraft() {
    state.draft = {
      name: t('workflows.defaultName'), nodes: [
        {id: 'research', kind: 'research', role: 'general', depends_on: [], condition: 'success',
          task: 'Read the repository and report exactly what this workflow is about to change.',
          budgets: {...BUDGET_DEFAULTS}, read_only: true},
        {id: 'agent', kind: 'agent', role: 'general', depends_on: ['research'], condition: 'success',
          task: 'Implement the reviewed change inside the workspace, one focused edit at a time.',
          budgets: {...BUDGET_DEFAULTS}, read_only: false},
      ],
      family: '', sourceId: '', sourceVersion: null, order: [], layout: null, writes: [], validatedAt: 0,
    };
    state.selected = 'research';
    $('wfDraftName').value = state.draft.name;
  }

  function nextNodeId() {
    for (let index = 1; index <= 40; index += 1) {
      const candidate = `n${index}`;
      if (!nodeById(candidate)) return candidate;
    }
    return `n${Date.now() % 10000}`;
  }

  function addNode(kind) {
    const ident = nextNodeId();
    const role = 'general';
    state.draft.nodes.push({id: ident, kind, role, task: '', depends_on: [], condition: 'success',
      budgets: {...BUDGET_DEFAULTS}, read_only: kind === 'research' || kind === 'reviewer'});
    state.draft.layout = layoutOf(state.draft.nodes);
    state.selected = ident;
    paintBuilder();
    $('wfInspector')?.querySelector('textarea')?.focus();
  }

  function removeNode(ident) {
    state.draft.nodes = state.draft.nodes.filter((node) => node.id !== ident)
      .map((node) => ({...node, depends_on: (node.depends_on || []).filter((dep) => dep !== ident)}));
    if (state.selected === ident) state.selected = state.draft.nodes[0]?.id || '';
    state.draft.layout = layoutOf(state.draft.nodes);
    paintBuilder();
  }

  function paintBuilder() {
    const draft = state.draft;
    if (!draft.nodes.length && !draft.sourceId && !draft.name) newDraft();
    if ($('wfDraftName') !== document.activeElement) $('wfDraftName').value = draft.name;
    $('wfDraftSource').textContent = draft.sourceId
      ? t('workflows.sourceVersion', {id: draft.sourceId, version: draft.sourceVersion ?? '—'})
      : t('workflows.sourceUnsaved');
    $('wfDraftSource').dataset.tone = draft.sourceId ? '' : 'warn';

    const addRow = $('wfAddRow');
    if (!addRow.childElementCount) {
      addRow.append(el('span', 'workflows-meta', t('workflows.addNode')));
      for (const kind of KINDS) {
        const button = el('button', 'workflows-add', t(`workflows.kind.${kind}`));
        button.type = 'button';
        button.dataset.wfAdd = kind;
        button.onclick = () => addNode(kind);
        addRow.append(button);
      }
    }

    const layout = draft.layout || layoutOf(draft.nodes);
    paintCanvas($('wfCanvas'), layout, (item) => {
      const node = nodeById(item.id);
      return {
        kind: node ? t(`workflows.kind.${node.kind}`) : '',
        meta: node ? (node.read_only ? t('workflows.readOnly') : t('workflows.writes')) : '',
        selected: item.id === state.selected,
        onclick: () => { state.selected = item.id; paintBuilder(); },
      };
    });

    const list = $('wfNodeList');
    list.replaceChildren();
    orderedNodes().forEach((node, index) => {
      const row = el('li', 'workflows-nodeRow');
      if (node.id === state.selected) row.dataset.selected = 'true';
      const button = el('button', 'workflows-nodePick');
      button.type = 'button';
      button.setAttribute('aria-pressed', String(node.id === state.selected));
      button.setAttribute('aria-label', t('workflows.selectNode', {id: node.id}));
      button.append(el('span', 'workflows-index', String(index + 1)), el('b', '', node.id),
        chip(t(`workflows.kind.${node.kind}`), 'accent'));
      if (!node.read_only) button.append(chip(t('workflows.writes'), 'warn'));
      button.onclick = () => { state.selected = node.id; paintBuilder(); };
      const remove = el('button', 'btn ghost workflows-remove', t('workflows.removeNode'));
      remove.type = 'button';
      remove.setAttribute('aria-label', t('workflows.removeNodeAria', {id: node.id}));
      remove.onclick = () => removeNode(node.id);
      row.append(button, remove);
      list.append(row);
    });
    paintInspector();
    paintSchedulePickers();
  }

  function orderedNodes() {
    const known = new Set(state.draft.nodes.map((node) => node.id));
    const first = state.draft.order.filter((ident) => known.has(ident)).map(nodeById).filter(Boolean);
    const rest = state.draft.nodes.filter((node) => !state.draft.order.includes(node.id));
    return [...first, ...rest];
  }

  function paintInspector() {
    const host = $('wfInspector');
    const node = nodeById(state.selected);
    host.replaceChildren(el('h2', '', t('workflows.inspectorTitle')));
    if (!node) {
      host.append(el('p', 'workflows-empty', t('workflows.inspectorEmpty')));
      return;
    }
    const form = el('form', 'workflows-form');
    form.noValidate = true;

    const idField = el('label', 'workflows-field');
    idField.append(el('span', '', t('workflows.nodeId')));
    const idInput = el('input');
    idInput.type = 'text';
    idInput.value = node.id;
    idInput.maxLength = 40;
    idInput.autocomplete = 'off';
    idInput.spellcheck = false;
    idInput.addEventListener('change', () => {
      const next = idInput.value.trim();
      if (!/^[A-Za-z][A-Za-z0-9_-]{0,39}$/.test(next) || nodeById(next)) {
        setStatus($('wfBuilderStatus'), t('workflows.badNodeId'), 'error');
        idInput.value = node.id;
        return;
      }
      const previous = node.id;
      node.id = next;
      for (const other of state.draft.nodes) {
        other.depends_on = (other.depends_on || []).map((dep) => (dep === previous ? next : dep));
      }
      state.selected = next;
      paintBuilder();
    });
    idField.append(idInput);

    const kindField = el('label', 'workflows-field');
    kindField.append(el('span', '', t('workflows.nodeKind')));
    const kindSelect = el('select');
    for (const kind of KINDS) kindSelect.append(new Option(t(`workflows.kind.${kind}`), kind, false, kind === node.kind));
    kindSelect.onchange = () => {
      node.kind = kindSelect.value;
      node.read_only = ['research', 'reviewer'].includes(node.kind) || node.read_only === true;
      paintBuilder();
    };
    kindField.append(kindSelect);

    const roleField = el('label', 'workflows-field');
    roleField.append(el('span', '', t('workflows.nodeRole')));
    const roleInput = el('input');
    roleInput.type = 'text';
    roleInput.value = node.role || 'general';
    roleInput.setAttribute('list', 'wfRoleList');
    roleInput.autocomplete = 'off';
    roleInput.spellcheck = false;
    roleInput.onchange = () => { node.role = roleInput.value.trim(); paintBuilder(); };
    const roles = state.overview?.vocabulary?.roles || [];
    const datalist = el('datalist');
    datalist.id = 'wfRoleList';
    for (const role of roles) datalist.append(new Option(role, role));
    roleField.append(roleInput, datalist);

    const taskField = el('label', 'workflows-field');
    taskField.append(el('span', '', t('workflows.nodeTask')));
    const taskInput = el('textarea');
    taskInput.rows = 4;
    taskInput.value = node.task || '';
    taskInput.maxLength = 20000;
    taskInput.addEventListener('change', () => { node.task = taskInput.value; state.draft.validatedAt = 0; });
    taskField.append(taskInput);

    let checkField = null;
    if (node.kind === 'verification') {
      checkField = el('label', 'workflows-field');
      checkField.append(el('span', '', t('workflows.nodeCheck')));
      const checkSelect = el('select');
      for (const value of ['tests', 'lint', 'types']) {
        checkSelect.append(new Option(t(`workflows.check.${value}`), value, false, value === (node.check || 'tests')));
      }
      checkSelect.onchange = () => { node.check = checkSelect.value; state.draft.validatedAt = 0; };
      checkField.append(checkSelect);
    }

    const conditionField = el('label', 'workflows-field');
    conditionField.append(el('span', '', t('workflows.nodeCondition')));
    const conditionSelect = el('select');
    for (const condition of CONDITIONS) conditionSelect.append(new Option(t(`workflows.condition.${condition}`), condition, false, condition === node.condition));
    conditionSelect.onchange = () => { node.condition = conditionSelect.value; state.draft.validatedAt = 0; };
    conditionField.append(conditionSelect);

    const checks = el('div', 'workflows-checks');
    checks.append(el('span', 'workflows-meta', t('workflows.nodeDepends')));
    for (const other of state.draft.nodes.filter((item) => item.id !== node.id)) {
      const label = el('label', 'workflows-inline workflows-check');
      const box = el('input');
      box.type = 'checkbox';
      box.value = other.id;
      box.checked = (node.depends_on || []).includes(other.id);
      box.onchange = () => {
        const deps = new Set(node.depends_on || []);
        if (box.checked) deps.add(other.id); else deps.delete(other.id);
        node.depends_on = [...deps];
        state.draft.validatedAt = 0;
        paintBuilder();
      };
      label.append(box, el('span', '', other.id));
      checks.append(label);
    }

    const budgetBox = el('details', 'workflows-budgets');
    budgetBox.append(el('summary', '', t('workflows.budgets')));
    const grid = el('div', 'workflows-budgetgrid');
    for (const key of Object.keys(BUDGET_DEFAULTS)) {
      const field = el('label', 'workflows-field');
      field.append(el('span', '', t(`workflows.budget.${key}`)));
      const input = el('input');
      input.type = 'number';
      input.min = '1';
      input.max = String(BUDGET_CEILING[key]);
      input.value = String(node.budgets?.[key] ?? BUDGET_DEFAULTS[key]);
      input.onchange = () => {
        node.budgets = {...(node.budgets || {}), [key]: Number(input.value)};
        state.draft.validatedAt = 0;
      };
      field.append(input);
      grid.append(field);
    }
    budgetBox.append(grid);

    form.append(idField, kindField, roleField);
    const hint = t(`workflows.kindHint.${node.kind}`);
    if (hint !== `workflows.kindHint.${node.kind}`) form.append(el('p', 'workflows-help', hint));
    form.append(taskField);
    if (checkField) form.append(checkField);
    form.append(conditionField, checks, budgetBox);
    host.append(form);
    if (state.draft.writes.includes(node.id)) host.append(el('p', 'workflows-help', t('workflows.writeNodeNote')));
  }

  $('wfDraftName').addEventListener('change', () => {
    state.draft.name = $('wfDraftName').value;
    state.draft.validatedAt = 0;
  });

  /* ── validate / save / run ──────────────────────────────────────────── */
  async function validateDraft() {
    setStatus($('wfBuilderStatus'), t('workflows.validating'), '');
    try {
      const body = await gui.api('/api/workflows/validate', {definition: {name: state.draft.name, nodes: state.draft.nodes}})
        .then((payload) => payload.data);
      state.draft.nodes = body.definition.nodes.map((node) => ({...node}));
      state.draft.layout = body.layout;
      state.draft.order = body.order;
      state.draft.writes = body.writes || [];
      state.draft.validatedAt = Date.now() / 1000;
      paintBuilder();
      const out = $('wfValidateOut');
      out.hidden = false;
      out.textContent = t('workflows.validResult', {order: body.order.join(' → '),
        writes: body.writes.length ? body.writes.join(', ') : t('workflows.noWrites')});
      setStatus($('wfBuilderStatus'), t('workflows.validSaved'), 'ok');
      return true;
    } catch (error) {
      $('wfValidateOut').hidden = true;
      setStatus($('wfBuilderStatus'), message(error), 'error');
      return false;
    }
  }

  /* A typed-token form that survives background polling: the poller never
     rebuilds the panel while `.workflows-confirm` exists or an input has focus. */
  function typedConfirm(host, {token, hint, label, action, tone = 'primary', status = null, context = ''}) {
    const existing = host.querySelector('.workflows-confirm');
    if (existing) {
      /* the same request: keep the half-typed token; a different one replaces it */
      if (existing.dataset.token === token) {
        existing.querySelector('.workflows-word')?.focus();
        return;
      }
      existing.remove();
    }
    const box = el('div', 'workflows-confirm');
    box.dataset.token = token;
    const intro = el('label');
    intro.append(document.createTextNode(t('workflows.confirmIntro', {action: hint})), el('code', 'workflows-token', token));
    const input = el('input', 'workflows-word');
    input.type = 'text';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.setAttribute('aria-label', t('workflows.confirmAria'));
    input.placeholder = token;
    const row = el('div', 'workflows-confirmrow');
    const go = el('button', `btn ${tone}`, label);
    go.type = 'button';
    go.disabled = true;
    const submit = () => {
      if (go.disabled) return;
      go.disabled = true;
      Promise.resolve()
        .then(action)
        .then(() => { if (box.isConnected) box.remove(); })
        .catch((error) => {
          go.disabled = false;
          setStatus(status, message(error), 'error');
        });
    };
    go.onclick = submit;
    input.onkeydown = (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      submit();
    };
    const cancel = el('button', 'btn ghost', t('workflows.confirmCancel'));
    cancel.type = 'button';
    cancel.onclick = () => { box.remove(); input.blur(); };
    row.append(go, cancel);
    input.oninput = () => { go.disabled = input.value.trim() !== token; };
    if (context) box.append(el('p', 'workflows-meta', context));
    box.append(intro, input, row);
    host.append(box);
    input.focus();
    box.scrollIntoView({block: 'nearest'});
  }

  $('wfValidate').onclick = () => { validateDraft().catch(() => {}); };
  $('wfLoad').onclick = () => {
    const ident = $('wfLoadPick').value;
    if (!ident) {
      setStatus($('wfBuilderStatus'), t('workflows.pickVersionFirst'), 'warn');
      return;
    }
    openInBuilder(ident).catch(() => {});
  };
  $('wfNew').onclick = () => {
    newDraft();
    state.diff = null;
    $('wfDiffOut').replaceChildren();
    switchTab('builder');
    paintBuilder();
    setStatus($('wfBuilderStatus'), t('workflows.newDraft'), 'ok');
  };

  $('wfSave').onclick = () => {
    const host = $('wfConfirmHost');
    typedConfirm(host, {
      token: state.overview?.tokens?.save || 'SAVE WORKFLOW',
      hint: t('workflows.saveHint'),
      label: t('workflows.saveConfirm'),
      status: $('wfBuilderStatus'),
      action: async () => {
        if (!(await validateDraft())) throw new Error('invalid');
        const payload = {definition: {name: state.draft.name, nodes: state.draft.nodes},
          confirmation: state.overview?.tokens?.save || 'SAVE WORKFLOW'};
        if (state.draft.family) payload.family = state.draft.family;
        const body = await gui.api('/api/workflows/version/save', payload).then((reply) => reply.data);
        const saved = body.saved || {};
        state.draft.family = saved.family || state.draft.family;
        state.draft.sourceId = saved.id || state.draft.sourceId;
        state.draft.sourceVersion = saved.version ?? state.draft.sourceVersion;
        state.draft.name = saved.definition?.name || state.draft.name;
        host.querySelectorAll('.workflows-confirm').forEach((node) => node.remove());
        await Promise.all([loadFamilies(), loadOverview()]).catch(() => {});
        paintBuilder();
        setStatus($('wfBuilderStatus'), t('workflows.savedOk', {version: saved.version ?? '—'}), 'ok');
      },
    });
  };

  $('wfRunDraft').onclick = () => {
    const host = $('wfConfirmHost');
    if (!state.draft.sourceId) {
      setStatus($('wfBuilderStatus'), t('workflows.runNeedsSave'), 'warn');
      return;
    }
    typedConfirm(host, {
      token: state.overview?.tokens?.run || 'RUN REVIEWED WORKFLOW',
      hint: t('workflows.runHint'),
      label: t('workflows.runConfirm'),
      tone: 'primary',
      status: $('wfBuilderStatus'),
      action: async () => {
        const body = await gui.api('/api/workflows/run', {id: state.draft.sourceId,
          confirmation: state.overview?.tokens?.run || 'RUN REVIEWED WORKFLOW'}).then((reply) => reply.data);
        host.querySelectorAll('.workflows-confirm').forEach((node) => node.remove());
        const runId = body.started?.run_id;
        setStatus($('wfBuilderStatus'), t('workflows.runStarted', {id: runId || '—'}), 'ok');
        if (runId) {
          await loadRuns().catch(() => {});
          switchTab('runs');
          await openRun(runId).catch(() => {});
        }
      },
    });
  };

  /* ── version diff ───────────────────────────────────────────────────── */
  function paintVersionDiff(body, host) {
    host.replaceChildren();
    const head = el('p', 'workflows-meta', t('workflows.diffHead', {
      left: `${body.left.name || body.left.id} v${body.left.version ?? '?'}`,
      right: `${body.right.name || body.right.id} v${body.right.version ?? '?'}`}));
    if (!body.same_family) head.append(' · ', el('span', 'workflows-warn', t('workflows.diffOtherFamily')));
    host.append(head);
    if (body.identical) {
      host.append(el('p', 'workflows-help', t('workflows.diffSame')));
      return;
    }
    const rows = el('ul', 'workflows-plain');
    for (const ident of body.added) rows.append(line(t('workflows.diffAdded', {id: ident})));
    for (const ident of body.removed) rows.append(line(t('workflows.diffRemoved', {id: ident})));
    for (const change of body.changed) {
      rows.append(line(t('workflows.diffChanged', {id: change.id, fields: change.fields.join(', ')})));
      for (const field of change.fields) {
        const item = el('li', 'workflows-difffield');
        item.append(el('code', '', field), el('span', 'workflows-before', format(change.before[field])),
          el('span', 'workflows-arrow', '→'), el('span', 'workflows-after', format(change.after[field])));
        rows.append(item);
      }
    }
    host.append(rows);
  }

  const format = (value) => {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'string') return value.length > 160 ? `${value.slice(0, 160)}…` : value;
    return JSON.stringify(value);
  };

  async function runVersionDiff() {
    const left = $('wfDiffA').value;
    const right = $('wfDiffB').value;
    const host = $('wfDiffOut');
    if (!left || !right) {
      host.replaceChildren(el('p', 'workflows-help', t('workflows.pickTwo')));
      return;
    }
    host.replaceChildren(el('p', 'workflows-help', t('common.loading')));
    try {
      const body = await gui.api(`/api/workflows/diff?a=${encodeURIComponent(left)}&b=${encodeURIComponent(right)}`)
        .then((reply) => reply.data);
      state.diff = body;
      paintVersionDiff(body, host);
    } catch (error) {
      host.replaceChildren(el('p', 'workflows-status', message(error)));
    }
  }

  $('wfDiffGo').onclick = () => { runVersionDiff().catch(() => {}); };

  /* ── runs ───────────────────────────────────────────────────────────── */
  async function loadRuns({quiet = false} = {}) {
    const body = await gui.api('/api/workflows/runs?limit=40').then((payload) => payload.data);
    state.inspected = body.items || [];
    paintRuns({force: !quiet});
    paintRunPickers();
    setTotal();
  }

  function paintRuns({force = true} = {}) {
    const host = $('wfRuns');
    if (!force && (host.querySelector('.workflows-confirm') || isTyping())) return;
    host.replaceChildren();
    for (const run of state.inspected) {
      const card = el('article', 'workflows-item');
      if (run.id === state.run?.id) card.dataset.selected = 'true';
      const head = el('div', 'workflows-itemhead');
      head.append(el('h3', 'workflows-itemtitle', run.name || run.workflow_id || run.id));
      const chips = el('div', 'workflows-chips');
      chips.append(chip(statusLabel(run.status), statusTone(run.status)));
      chips.append(chip(t('workflows.progress', {done: run.nodes_done, total: run.nodes_total}), ''));
      head.append(chips);
      card.append(head, el('p', 'workflows-meta', `${stamp(run.created)} · ${run.id}`));
      if (run.error) card.append(el('p', 'workflows-status', run.error));
      const actions = el('div', 'workflows-actions');
      const open = el('button', 'btn', t('workflows.openRun'));
      open.type = 'button';
      open.onclick = () => { openRun(run.id).catch(() => {}); };
      actions.append(open);
      card.append(actions);
      host.append(card);
    }
    $('wfRunsEmpty').hidden = state.inspected.length > 0;
    $('wfRunCount').textContent = String(state.inspected.length || 0);
  }

  function paintRunPickers() {
    for (const select of [$('wfCompareA'), $('wfCompareB')]) {
      const previous = select.value;
      select.replaceChildren(new Option(t('workflows.pick'), ''));
      for (const run of state.inspected) {
        select.append(new Option(`${stamp(run.created)} · ${run.name || run.id} · ${run.status}`, run.id));
      }
      if (state.inspected.some((run) => run.id === previous)) select.value = previous;
    }
  }

  async function openRun(identifier) {
    const host = $('wfRunDetail');
    host.replaceChildren(el('h2', '', t('workflows.runDetailTitle')), el('p', 'workflows-help', t('common.loading')));
    try {
      const body = await gui.api(`/api/workflows/run?id=${encodeURIComponent(identifier)}`).then((reply) => reply.data);
      state.run = body;
      paintRunDetail();
      paintRuns({force: true});
      if (!state.inspected.some((run) => run.id === body.id)) loadRuns().catch(() => {});
      return body;
    } catch (error) {
      host.replaceChildren(el('h2', '', t('workflows.runDetailTitle')), el('p', 'workflows-status', message(error)));
      return null;
    }
  }

  function paintRunDetail() {
    const body = state.run;
    const host = $('wfRunDetail');
    if (!body) return;
    host.replaceChildren();
    host.append(el('h2', '', body.name || body.workflow_id || t('workflows.runDetailTitle')));
    const head = el('div', 'workflows-chips');
    head.append(chip(statusLabel(body.status), statusTone(body.status)), chip(`v${body.version ?? '—'}`, ''));
    head.append(chip(stamp(body.created), ''));
    host.append(head);
    if (body.error) host.append(el('p', 'workflows-status', body.error));
    const wrap = el('div', 'workflows-canvaswrap workflows-canvaswrap-compact');
    const canvas = el('div', 'workflows-canvas');
    wrap.append(canvas);
    host.append(wrap);
    const nodes = new Map((body.nodes || []).map((node) => [node.id, node]));
    paintCanvas(canvas, body.layout, (item) => {
      const node = nodes.get(item.id) || {};
      return {
        kind: t(`workflows.kind.${node.kind}`) || '',
        meta: `${statusLabel(node.status)}${node.duration_s ? ` · ${Math.round(node.duration_s)}s` : ''}`,
        status: node.status,
        onclick: () => $('wfRunDetail').querySelector(`[data-run-node="${item.id}"]`)?.scrollIntoView({block: 'nearest'}),
      };
    });
    const list = el('ol', 'workflows-runNodes');
    for (const node of body.nodes || []) {
      const row = el('li', 'workflows-runNode');
      row.dataset.runNode = node.id;
      row.dataset.status = node.status || '';
      const top = el('div', 'workflows-itemhead');
      const title = el('b', '', node.id);
      const chips = el('div', 'workflows-chips');
      chips.append(chip(t(`workflows.kind.${node.kind}`), 'accent'), chip(statusLabel(node.status), statusTone(node.status)));
      if (node.duration_s) chips.append(chip(`${Math.round(node.duration_s)}s`, ''));
      top.append(title, chips);
      row.append(top);
      row.append(el('p', 'workflows-meta', [node.role, node.task, node.task_id ? `task ${node.task_id}` : '',
        node.evidence_id ? `evidence ${node.evidence_id}` : ''].filter(Boolean).join(' · ')));
      if (node.text) {
        const details = el('details', 'workflows-answer');
        details.append(el('summary', '', t('workflows.nodeAnswer')), el('pre', '', node.text));
        row.append(details);
      }
      if (node.error) row.append(el('p', 'workflows-status', node.error));
      list.append(row);
    }
    host.append(list);
    if (body.status === 'running') {
      const actions = el('div', 'workflows-actions');
      const cancel = el('button', 'btn ghost', t('workflows.cancelRun'));
      cancel.type = 'button';
      cancel.onclick = () => typedConfirm(host, {
        token: `CANCEL RUN ${body.id}`, hint: t('workflows.cancelHint'), label: t('workflows.cancelConfirm'),
        status: host.querySelector('.workflows-status'),
        action: async () => {
          await gui.api('/api/workflows/cancel', {id: body.id, confirmation: `CANCEL RUN ${body.id}`});
          host.querySelectorAll('.workflows-confirm').forEach((node) => node.remove());
          await openRun(body.id).catch(() => {});
        },
      });
      actions.append(cancel);
      host.append(actions);
    }
  }

  $('wfCompareGo').onclick = () => {
    const left = $('wfCompareA').value;
    const right = $('wfCompareB').value;
    const host = $('wfRunDiffOut');
    if (!left || !right) {
      host.replaceChildren(el('p', 'workflows-help', t('workflows.pickTwo')));
      return;
    }
    host.replaceChildren(el('p', 'workflows-help', t('common.loading')));
    gui.api(`/api/workflows/run-diff?a=${encodeURIComponent(left)}&b=${encodeURIComponent(right)}`)
      .then((reply) => reply.data)
      .then((body) => {
        state.runDiff = body;
        host.replaceChildren();
        host.append(el('p', 'workflows-meta', `${body.left.id} → ${body.right.id}`));
        if (body.identical) {
          host.append(el('p', 'workflows-help', t('workflows.diffSame')));
          return;
        }
        const rows = el('ul', 'workflows-plain');
        for (const change of body.changed) {
          rows.append(line(t('workflows.runDiffChanged', {id: change.id,
            before: statusLabel(change.before.status), after: statusLabel(change.after.status)})));
        }
        for (const ident of body.only_left) rows.append(line(t('workflows.runDiffOnlyLeft', {id: ident})));
        for (const ident of body.only_right) rows.append(line(t('workflows.runDiffOnlyRight', {id: ident})));
        host.append(rows);
      })
      .catch((error) => { host.replaceChildren(el('p', 'workflows-status', message(error))); });
  };

  /* ── schedules ──────────────────────────────────────────────────────── */
  async function loadSchedules({quiet = false} = {}) {
    const body = await gui.api('/api/workflows/schedules').then((payload) => payload.data);
    state.schedules = body.schedules || [];
    state.scheduler = body.scheduler || null;
    state.revision = body.revision || '';
    paintSchedules({force: !quiet});
    paintScheduler();
    if (!state.families.length) await loadFamilies().catch(() => {});
    paintSchedulePickers();
    setBanner(body.error ? t('workflows.storeDamaged', {error: body.error}) : '');
    if (!state.overview) await loadOverview().catch(() => {});
  }

  function paintScheduler() {
    const scheduler = state.scheduler || {};
    const host = $('wfScheduler');
    host.replaceChildren();
    const cards = [
      [t('workflows.schedulerRunning'), scheduler.running ? t('workflows.status.running') : t('workflows.stopped'),
        scheduler.running ? 'ok' : 'warn'],
      [t('workflows.schedulerPasses'), String(scheduler.passes ?? '—'), ''],
      [t('workflows.schedulerInterval'), scheduler.interval_s ? `${scheduler.interval_s}s` : '—', ''],
      [t('workflows.schedulerLast'), scheduler.last_pass ? stamp(scheduler.last_pass.observed_at) : '—', ''],
      [t('workflows.schedulerEnabled'), `${scheduler.enabled ?? 0}/${scheduler.schedules ?? 0}`, ''],
    ];
    for (const [label, value, tone] of cards) {
      const card = el('div', 'workflows-count');
      if (tone) card.dataset.tone = tone;
      card.append(el('b', '', value), el('span', '', label));
      host.append(card);
    }
    facts($('wfSchedulerFacts'), [
      [t('workflows.schedulerStore'), t('workflows.storeLocal')],
      [t('workflows.schedulerLastTick'), stamp(scheduler.last_tick_at)],
      [t('workflows.schedulerError'), scheduler.error || '—'],
      [t('workflows.schedulerRevision'), state.revision || '—'],
    ]);
  }

  function paintSchedules({force = true} = {}) {
    const host = $('wfSchedules');
    if (!force && (host.querySelector('.workflows-confirm') || isTyping())) return;
    host.replaceChildren();
    for (const row of state.schedules) {
      const card = el('article', 'workflows-item');
      card.dataset.schedule = row.id;
      const head = el('div', 'workflows-itemhead');
      head.append(el('h3', 'workflows-itemtitle', row.name || row.workflow_id));
      const chips = el('div', 'workflows-chips');
      chips.append(chip(row.enabled ? t('workflows.enabled') : t('workflows.disabled'), row.enabled ? 'ok' : 'warn'));
      chips.append(chip(describeTrigger(row.trigger, row.described), 'accent'));
      if (row.missed) chips.append(chip(t('workflows.missedCount', {count: row.missed}), 'warn'));
      if (!row.version_present) chips.append(chip(t('workflows.versionGone'), 'error'));
      head.append(chips);
      card.append(head);
      card.append(el('p', 'workflows-meta', t('workflows.scheduleMeta', {
        id: row.id, version: row.version ?? '—', updated: stamp(row.updated)})));
      card.append(el('p', 'workflows-meta', row.enabled
        ? t('workflows.nextFire', {time: stamp(row.next_run_at), rel: relative(row.next_run_at)})
        : t('workflows.nextFireOff')));
      if (row.last_skip?.reason) {
        card.append(el('p', 'workflows-status', t('workflows.lastSkip', {reason: row.last_skip.reason})));
      }
      if (row.last?.run_id) {
        const last = el('button', 'workflows-version', t('workflows.lastRun', {id: row.last.run_id, time: stamp(row.last.at)}));
        last.type = 'button';
        last.onclick = () => { switchTab('runs'); openRun(row.last.run_id).catch(() => {}); };
        card.append(last);
      }
      const actions = el('div', 'workflows-actions');
      const toggle = el('button', 'btn', row.enabled ? t('workflows.disable') : t('workflows.enable'));
      toggle.type = 'button';
      if (row.enabled === false && row.trigger?.kind === 'manual') toggle.disabled = true;
      toggle.onclick = () => typedConfirm($('wfScheduleConfirm'), {
        context: `${row.name || row.workflow_id} · ${row.id}`,
        token: `${row.enabled ? 'DISABLE' : 'ENABLE'} SCHEDULE ${row.id}`,
        hint: t('workflows.toggleHint'), label: row.enabled ? t('workflows.disableConfirm') : t('workflows.enableConfirm'),
        status: $('wfSchStatus'),
        action: async () => {
          await gui.api('/api/workflows/schedule/toggle', {id: row.id, enabled: !row.enabled, revision: state.revision,
            confirmation: `${row.enabled ? 'DISABLE' : 'ENABLE'} SCHEDULE ${row.id}`});
          await loadSchedules();
          setStatus($('wfSchStatus'), t(row.enabled ? 'workflows.disabledOk' : 'workflows.enabledOk'), 'ok');
        },
      });
      const runNow = el('button', 'btn ghost', t('workflows.runNow'));
      runNow.type = 'button';
      runNow.onclick = () => typedConfirm($('wfScheduleConfirm'), {
        context: `${row.name || row.workflow_id} · ${row.id}`,
        token: `RUN SCHEDULE ${row.id}`, hint: t('workflows.runNowHint'), label: t('workflows.runNowConfirm'),
        status: $('wfSchStatus'),
        action: async () => {
          try {
            const body = await gui.api('/api/workflows/schedule/run', {id: row.id,
              confirmation: `RUN SCHEDULE ${row.id}`}).then((reply) => reply.data);
            setStatus($('wfSchStatus'), t('workflows.runStarted', {id: body.run_id || '—'}), 'ok');
            await loadSchedules();
            if (body.run_id) { switchTab('runs'); await openRun(body.run_id).catch(() => {}); }
          } catch (error) {
            setStatus($('wfSchStatus'), message(error), 'error');
            throw error;
          }
        },
      });
      const remove = el('button', 'btn ghost', t('workflows.remove'));
      remove.type = 'button';
      remove.onclick = () => typedConfirm($('wfScheduleConfirm'), {
        context: `${row.name || row.workflow_id} · ${row.id}`,
        token: `REMOVE SCHEDULE ${row.id}`, hint: t('workflows.removeHint'), label: t('workflows.removeConfirm'),
        status: $('wfSchStatus'),
        action: async () => {
          await gui.api('/api/workflows/schedule/remove', {id: row.id, confirmation: `REMOVE SCHEDULE ${row.id}`});
          await loadSchedules();
          setStatus($('wfSchStatus'), t('workflows.removedOk'), 'ok');
        },
      });
      actions.append(toggle, runNow, remove);
      card.append(actions);
      host.append(card);
    }
    $('wfSchedulesEmpty').hidden = state.schedules.length > 0;
    $('wfScheduleCount').textContent = String(state.schedules.length || 0);
  }

  function paintSchedulePickers() {
    const select = $('wfSchWorkflow');
    if (!select) return;
    const previous = select.value;
    select.replaceChildren(new Option(t('workflows.pick'), ''));
    for (const family of state.families) {
      for (const version of family.versions) {
        select.append(new Option(`${family.name || family.family} · v${version.version ?? '?'}`, version.id));
      }
    }
    if (previous && [...select.options].some((option) => option.value === previous)) select.value = previous;
    else if (state.draft.sourceId) select.value = state.draft.sourceId;
    else select.value = select.options[1]?.value || '';   /* newest saved version, never invented */
  }

  function triggerPayload() {
    const kind = $('wfSchKind').value;
    if (kind === 'manual') return {kind: 'manual'};
    if (kind === 'cron') return {kind: 'cron', cron: $('wfSchCron').value.trim()};
    return {kind: 'interval', minutes: Number($('wfSchMinutes').value)};
  }

  function paintTriggerFields() {
    const kind = $('wfSchKind').value;
    $('wfSchMinutesField').hidden = kind !== 'interval';
    $('wfSchCronField').hidden = kind !== 'cron';
    $('wfSchEnabled').disabled = kind === 'manual';
    if (kind === 'manual') $('wfSchEnabled').checked = false;
  }

  async function previewTrigger() {
    const host = $('wfSchPreview');
    host.replaceChildren(el('li', '', t('common.loading')));
    try {
      const body = await gui.api('/api/workflows/trigger/preview', {trigger: triggerPayload()}).then((reply) => reply.data);
      state.preview = body.times || [];
      if (!state.preview.length) {
        host.replaceChildren(el('li', '', t('workflows.previewManual')));
        return;
      }
      host.replaceChildren(...state.preview.map((time) => el('li', '', `${stamp(time)} · ${relative(time)}`)));
    } catch (error) {
      host.replaceChildren(el('li', 'workflows-warn', message(error)));
    }
  }

  $('wfSchKind').onchange = () => { paintTriggerFields(); previewTrigger().catch(() => {}); };
  $('wfSchPreviewBtn').onclick = () => { previewTrigger().catch(() => {}); };
  $('wfScheduleForm').onsubmit = (event) => {
    event.preventDefault();
    const host = $('wfScheduleForm');
    const workflowId = $('wfSchWorkflow').value;
    if (!workflowId) {
      setStatus($('wfSchStatus'), t('workflows.pickVersionFirst'), 'warn');
      return;
    }
    const trigger = triggerPayload();
    typedConfirm($('wfSchConfirm'), {
      token: state.overview?.tokens?.schedule || 'SCHEDULE WORKFLOW',
      hint: t('workflows.scheduleHint'), label: t('workflows.scheduleConfirm'),
      status: $('wfSchStatus'),
      action: async () => {
        await gui.api('/api/workflows/schedule/save', {workflow_id: workflowId, trigger,
          enabled: $('wfSchEnabled').checked, revision: state.revision,
          confirmation: state.overview?.tokens?.schedule || 'SCHEDULE WORKFLOW'});
        host.querySelectorAll('.workflows-confirm').forEach((node) => node.remove());
        await loadSchedules();
        setStatus($('wfSchStatus'), t('workflows.scheduleSaved'), 'ok');
        previewTrigger().catch(() => {});
      },
    });
  };

  /* ── polling: never under a focused field or an open confirmation ───── */
  let timer = null;
  function schedule() {
    if (timer) window.clearInterval(timer);
    timer = null;
    if (gui.view !== 'workflows') return;
    timer = window.setInterval(() => {
      if (document.hidden || state.busy) return;
      if (root.querySelector('.workflows-confirm') || root.contains(document.activeElement)) return;
      if (state.tab === 'workflows') loadFamilies().catch(() => {});
      if (state.tab === 'schedules') loadSchedules({quiet: true}).catch(() => {});
      if (state.tab === 'runs') {
        loadRuns({quiet: true}).catch(() => {});
        if (state.run) openRun(state.run.id).catch(() => {});
      }
    }, 8000);
  }

  /* ── wiring ─────────────────────────────────────────────────────────── */
  $('wfRefresh').onclick = () => {
    loadOverview().catch(() => {});
    loadFamilies().catch(() => {});
    if (state.tab === 'runs') loadRuns().catch(() => {});
    if (state.tab === 'schedules') loadSchedules().catch(() => {});
    if (state.run) openRun(state.run.id).catch(() => {});
  };
  $('wfStudio').onclick = () => {
    $('studioOpen')?.click();
    window.setTimeout(() => {
      const button = [...document.querySelectorAll('#stNav button')].find((node) => node.textContent.trim() === 'Workflows');
      button?.click();
    }, 80);
  };

  window.addEventListener('nexus:view', (event) => {
    if (event.detail === 'workflows') {
      if (!state.overview) loadOverview().catch(() => {});
      if (!state.families.length) loadFamilies().catch(() => {});
      if (state.tab === 'runs' && !state.inspected.length) loadRuns().catch(() => {});
      if (state.tab === 'schedules') loadSchedules().catch(() => {});
      if (state.tab === 'builder' && !state.draft.nodes.length) { newDraft(); paintBuilder(); }
    }
    schedule();
  });
  document.addEventListener('visibilitychange', schedule);
  i18n.onChange(() => {
    i18n.apply(root);
    paintOverview();
    paintFamilies();
    paintRuns({force: true});
    paintSchedules({force: true});
    paintScheduler();
    paintBuilder();
    if (state.lastRead) $('wfUpdated').textContent = t('workflows.updated', {time: clock(state.lastRead)});
    if (state.diff) paintVersionDiff(state.diff, $('wfDiffOut'));
  });

  paintTriggerFields();
  if (gui.view === 'workflows') {
    loadOverview().catch(() => {});
    loadFamilies().catch(() => {});
  }
  schedule();

  window.nexusWorkflows = {
    root, tab: switchTab, load: async () => {
      await Promise.all([loadOverview(), loadFamilies()]);
    },
    state: () => ({tab: state.tab, draftNodes: state.draft.nodes.length, run: state.run?.id || null,
      schedules: state.schedules?.length ?? null}),
  };
})();
