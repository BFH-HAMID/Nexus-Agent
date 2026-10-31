/* Approval Center: every request that is really waiting, in one reviewed place.

   Three existing gates raise requests — a live task paused inside its approval
   gate, a background Jarvis action, and a queued task waiting for admission.
   This page only *projects* them and asks for the decision the gate already
   expects; it starts nothing, grants nothing on its own, and never shows a
   request whose own record has gone.

   Refusing is one click. Granting needs the exact token that request published.
   Expired or consumed requests are reported as gone, never revived. */
(() => {
  'use strict';

  const gui = window.nexusGui;
  const i18n = window.nexusI18n;
  if (!gui || !i18n) return;
  const t = i18n.t;
  const $ = (id) => document.getElementById(id);

  const root = document.createElement('section');
  root.id = 'view-approvals';
  root.className = 'gui-modules approvals-page';
  root.dataset.guiView = 'approvals';
  root.hidden = gui.view !== 'approvals';
  root.innerHTML = `
    <header class="approvals-head">
      <div>
        <p class="approvals-eyebrow" data-i18n="approvals.eyebrow">Approval Center</p>
        <h1 data-i18n="approvals.title">What is waiting for you</h1>
        <p class="approvals-sub" data-i18n="approvals.subtitle"></p>
      </div>
      <div class="approvals-quick" role="group" aria-label="Approval actions">
        <button class="btn" type="button" id="apRefresh"><span class="btn-label" data-i18n="approvals.refresh"></span></button>
        <button class="btn ghost" type="button" id="apOpenOps"><span class="btn-label" data-i18n="approvals.openOperations"></span></button>
        <span class="approvals-stamp" id="apUpdated" data-i18n="approvals.never"></span>
      </div>
    </header>

    <div class="approvals-toolbar">
      <div class="approvals-tabs" role="tablist" aria-label="Approval Center sections">
        <button type="button" role="tab" id="apTabInbox" data-ap-tab="inbox" aria-selected="true" aria-controls="apPanelInbox" data-i18n="approvals.tabInbox"></button>
        <button type="button" role="tab" id="apTabPolicy" data-ap-tab="policy" aria-selected="false" aria-controls="apPanelPolicy" data-i18n="approvals.tabPolicy"></button>
        <button type="button" role="tab" id="apTabHistory" data-ap-tab="history" aria-selected="false" aria-controls="apPanelHistory" data-i18n="approvals.tabHistory"></button>
        <button type="button" role="tab" id="apTabDiag" data-ap-tab="diagnostics" aria-selected="false" aria-controls="apPanelDiagnostics" data-i18n="approvals.tabDiagnostics"></button>
      </div>
      <span class="approvals-chip" id="apTotal" data-tone="warn" role="status" aria-live="polite">—</span>
    </div>

    <div class="approvals-body">
      <section class="approvals-panel" id="apPanelInbox" role="tabpanel" aria-labelledby="apTabInbox" data-ap-panel="inbox">
        <div class="approvals-summary" id="apCounts"></div>
        <div class="approvals-filters" id="apFilters" role="group" aria-label="Filter by source"></div>
        <p class="approvals-help" data-i18n="approvals.inboxLead"></p>
        <div class="approvals-list" id="apList"></div>
        <p class="approvals-empty" id="apEmpty" hidden></p>
      </section>

      <section class="approvals-panel" id="apPanelPolicy" role="tabpanel" aria-labelledby="apTabPolicy" data-ap-panel="policy" hidden>
        <div class="approvals-grid">
          <article class="approvals-card">
            <div class="approvals-cardhead"><h2 data-i18n="approvals.policyPermissions"></h2><span class="approvals-chip" id="apPolicyStamp">—</span></div>
            <dl class="approvals-facts" id="apPermissions"></dl>
            <div class="approvals-toolmodes" id="apToolModes"></div>
          </article>
          <article class="approvals-card">
            <h2 data-i18n="approvals.policyAutonomy"></h2>
            <dl class="approvals-facts" id="apAutonomy"></dl>
            <p class="approvals-help" data-i18n="approvals.autonomyNote"></p>
          </article>
          <article class="approvals-card">
            <h2 data-i18n="approvals.policyStops"></h2>
            <dl class="approvals-facts" id="apStops"></dl>
            <p class="approvals-help" data-i18n="approvals.stopsNote"></p>
          </article>
          <article class="approvals-card">
            <h2 data-i18n="approvals.alwaysReview"></h2>
            <ul class="approvals-plain" id="apAlwaysReview"></ul>
            <p class="approvals-help" data-i18n="approvals.alwaysNote"></p>
          </article>
          <article class="approvals-card">
            <h2 data-i18n="approvals.notChecked"></h2>
            <ul class="approvals-plain" id="apNotChecked"></ul>
          </article>
        </div>
      </section>

      <section class="approvals-panel" id="apPanelHistory" role="tabpanel" aria-labelledby="apTabHistory" data-ap-panel="history" hidden>
        <div class="approvals-card">
          <div class="approvals-cardhead"><h2 data-i18n="approvals.historyTitle"></h2><span class="approvals-chip" id="apHistoryCount">—</span></div>
          <p class="approvals-help" data-i18n="approvals.historyLead"></p>
          <div class="approvals-history" id="apHistory"></div>
          <p class="approvals-empty" id="apHistoryEmpty" hidden data-i18n="approvals.historyEmpty"></p>
        </div>
      </section>

      <section class="approvals-panel" id="apPanelDiagnostics" role="tabpanel" aria-labelledby="apTabDiag" data-ap-panel="diagnostics" hidden>
        <div class="approvals-grid">
          <article class="approvals-card">
            <h2 data-i18n="approvals.diagnosticsTitle"></h2>
            <dl class="approvals-facts" id="apDiag"></dl>
          </article>
          <article class="approvals-card">
            <h2 data-i18n="approvals.sourceErrors"></h2>
            <ul class="approvals-plain" id="apDiagErrors"></ul>
          </article>
          <article class="approvals-card">
            <h2 data-i18n="approvals.historySources"></h2>
            <ul class="approvals-plain" id="apDiagSources"></ul>
          </article>
          <article class="approvals-card">
            <h2 data-i18n="approvals.limitsTitle"></h2>
            <ul class="approvals-plain" id="apLimits"></ul>
          </article>
        </div>
      </section>
    </div>
  `;
  (document.querySelector('main.deck') || document.body).after(root);

  const state = {
    tab: 'inbox', filter: 'all', inbox: null, policy: null, history: null,
    busy: false, lastRead: 0,
  };

  const clock = (value) => {
    if (!value) return '—';
    const date = new Date(value * 1000);
    return Number.isNaN(date.getTime()) ? '—'
      : date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  };
  const stamp = (value) => (value ? new Date(value * 1000).toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : '—');
  const message = (error) => error?.payload?.error?.message || error?.message || String(error);
  const set = (node, text, tone) => {
    if (!node) return;
    node.textContent = text;
    if (tone !== undefined) node.dataset.tone = tone;
  };
  function flash(node, text, tone = '') {
    set(node, text, tone);
    if (text) window.setTimeout(() => { if (node.textContent === text) set(node, '', ''); }, 8000);
  }
  function chip(text, tone) {
    const span = document.createElement('span');
    span.className = 'approvals-chip';
    span.textContent = text;
    if (tone) span.dataset.tone = tone;
    return span;
  }
  const sourceLabel = (source) => t(`approvals.source.${source}`);
  const riskLabel = (risk) => (risk && i18n.t(`approvals.risk.${risk}`) !== `approvals.risk.${risk}`
    ? t(`approvals.risk.${risk}`) : String(risk || '—'));
  const decisionLabel = (decision) => t(`approvals.action.${decision}`);

  function paintCounts() {
    const counts = state.inbox?.counts || {live: 0, action: 0, queue: 0, total: 0};
    const host = $('apCounts');
    host.replaceChildren();
    for (const source of ['live', 'action', 'queue']) {
      const card = document.createElement('div');
      card.className = 'approvals-count';
      card.dataset.source = source;
      const value = document.createElement('b');
      value.textContent = counts[source] < 0 ? '—' : String(counts[source]);
      const label = document.createElement('span');
      label.textContent = sourceLabel(source);
      card.append(value, label);
      host.append(card);
    }
    const total = counts.total || 0;
    set($('apTotal'), total ? t('approvals.waiting', {count: total}) : t('approvals.nothingWaiting'),
      total ? 'warn' : 'ok');
  }

  function paintFilters() {
    const host = $('apFilters');
    host.replaceChildren();
    for (const [value, key] of [['all', 'approvals.filterAll'], ['live', 'approvals.source.live'],
      ['action', 'approvals.source.action'], ['queue', 'approvals.source.queue']]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'approvals-filter';
      button.dataset.apFilter = value;
      button.setAttribute('aria-pressed', String(state.filter === value));
      button.textContent = t(key);
      button.onclick = () => { state.filter = value; paintFilters(); paintList(); };
      host.append(button);
    }
  }

  function detailRow(item) {
    const meta = document.createElement('p');
    meta.className = 'approvals-meta';
    const bits = [item.tool || '—', item.detail || '', t('approvals.requested', {time: clock(item.requested)})];
    meta.textContent = bits.filter(Boolean).join(' · ');
    return meta;
  }

  function renderItem(item) {
    const article = document.createElement('article');
    article.className = 'approvals-item';
    article.dataset.source = item.source;
    article.dataset.ref = item.reference;
    article.id = `apItem-${item.source}-${item.reference}`;

    const head = document.createElement('div');
    head.className = 'approvals-itemhead';
    const title = document.createElement('h3');
    title.className = 'approvals-title';
    title.textContent = item.title || item.tool || t('approvals.untitled');
    const chips = document.createElement('div');
    chips.className = 'approvals-chips';
    chips.append(chip(sourceLabel(item.source), 'accent'));
    if (item.risk) chips.append(chip(riskLabel(item.risk), item.risk === 'read' ? 'ok' : 'warn'));
    if (typeof item.seconds_left === 'number') {
      chips.append(chip(t('approvals.expiresIn', {time: formatLeft(item.seconds_left)}),
        item.seconds_left < 120 ? 'error' : ''));
    }
    head.append(title, chips);
    article.append(head, detailRow(item));

    if (item.preview) {
      const preview = document.createElement('pre');
      preview.className = 'approvals-preview';
      preview.textContent = item.preview;
      article.append(preview);
    }
    if (item.args && Object.keys(item.args).length) {
      const details = document.createElement('details');
      details.className = 'approvals-args';
      const summary = document.createElement('summary');
      summary.textContent = t('approvals.arguments');
      const body = document.createElement('pre');
      body.textContent = JSON.stringify(item.args, null, 2);
      details.append(summary, body);
      article.append(details);
    }

    const status = document.createElement('p');
    status.className = 'approvals-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');

    const actions = document.createElement('div');
    actions.className = 'approvals-actions';
    for (const decision of item.decisions || []) {
      const button = document.createElement('button');
      button.type = 'button';
      const grant = decision === 'approve' || decision === 'allow';
      button.className = grant ? 'btn primary' : 'btn ghost';
      button.dataset.apAct = decision;
      button.textContent = decisionLabel(decision);
      button.onclick = () => {
        if (grant) openConfirm(article, item, decision, status);
        else submit(item, decision, '', null, status, actions);
      };
      actions.append(button);
    }
    article.append(actions, status);
    return article;
  }

  function formatLeft(seconds) {
    if (seconds >= 3600) return t('approvals.hours', {count: Math.floor(seconds / 3600)});
    if (seconds >= 60) return t('approvals.minutes', {count: Math.floor(seconds / 60)});
    return t('approvals.seconds', {count: seconds});
  }

  function openConfirm(article, item, decision, status) {
    if (article.querySelector('.approvals-confirm')) {
      article.querySelector('.approvals-word')?.focus();
      return;
    }
    const box = document.createElement('form');
    box.className = 'approvals-confirm';
    box.noValidate = true;
    const label = document.createElement('label');
    label.textContent = t('approvals.confirmIntro', {token: item.token});
    const token = document.createElement('code');
    token.className = 'approvals-token';
    token.textContent = item.token;
    label.append(' ', token);
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'approvals-word';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.setAttribute('aria-label', t('approvals.confirmAria'));
    input.placeholder = item.token;
    const scope = document.createElement('label');
    scope.className = 'approvals-scope';
    if ((item.scopes || []).includes('session')) {
      const select = document.createElement('select');
      select.className = 'approvals-scope-select';
      for (const value of item.scopes) {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = t(`approvals.scope.${value}`);
        select.append(option);
      }
      scope.append(document.createTextNode(t('approvals.scopeLabel')), select);
      scope.hidden = false;
    } else {
      scope.hidden = true;
    }
    const row = document.createElement('div');
    row.className = 'approvals-confirmrow';
    const go = document.createElement('button');
    go.type = 'submit';
    go.className = 'btn primary';
    go.disabled = true;
    go.textContent = t('approvals.confirmGo', {decision: decisionLabel(decision)});
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn ghost';
    cancel.textContent = t('approvals.confirmCancel');
    cancel.onclick = () => { box.remove(); input.focus(); };
    row.append(go, cancel);
    input.oninput = () => { go.disabled = input.value.trim() !== item.token; };
    box.append(label, input, scope, row);
    box.onsubmit = (event) => {
      event.preventDefault();
      if (go.disabled) return;
      const chosen = box.querySelector('.approvals-scope-select')?.value || 'once';
      submit(item, decision, input.value.trim(), chosen, status, box, go);
    };
    article.append(box);
    input.focus();
  }

  async function submit(item, decision, confirmation, scope, status, region, go) {
    if (state.busy) return;
    state.busy = true;
    for (const button of region.querySelectorAll('button')) button.disabled = true;
    if (go) go.disabled = true;
    const body = {source: item.source, id: item.reference, decision};
    if (item.approval_id) body.approval_id = item.approval_id;
    if (confirmation) body.confirmation = confirmation;
    if (scope && (item.scopes || []).includes('session')) body.scope = scope;
    try {
      await gui.api('/api/approvals/decide', body);
      flash(status, t(`approvals.done.${decision}`), 'ok');
      state.history = null;
      if (state.tab === 'history') await loadHistory().catch(() => {});
      root.querySelectorAll('.approvals-confirm').forEach((node) => node.remove());
      await load({quiet: true});
      paintList({force: true});
    } catch (error) {
      flash(status, message(error), 'error');
      region.querySelectorAll('button').forEach((button) => { button.disabled = false; });
      if (go) go.disabled = false;
      await load({quiet: true}).catch(() => {});
    } finally {
      state.busy = false;
    }
  }

  function paintList({force = false} = {}) {
    const host = $('apList');
    /* Never rebuild the list under someone who is typing a grant token: the
       inline confirmation and its focus must survive a background poll. */
    if (!force && host.querySelector('.approvals-confirm')) return;
    host.replaceChildren();
    const items = (state.inbox?.items || []).filter((item) => state.filter === 'all' || item.source === state.filter);
    for (const item of items) host.append(renderItem(item));
    const empty = $('apEmpty');
    const filteredOut = (state.inbox?.items || []).length > 0 && items.length === 0;
    empty.hidden = items.length > 0;
    empty.textContent = filteredOut ? t('approvals.emptyFiltered') : t('approvals.emptyInbox');
  }

  async function load({quiet = false} = {}) {
    if (!quiet) root.dataset.busy = 'true';
    try {
      state.inbox = await gui.api('/api/approvals/inbox').then((body) => body.data);
      state.lastRead = state.inbox.observed_at;
      paintCounts();
      paintFilters();
      paintList({force: !quiet});
      set($('apUpdated'), t('approvals.updated', {time: clock(state.inbox.observed_at)}));
      if (state.tab === 'diagnostics') paintDiagnostics();
    } catch (error) {
      set($('apUpdated'), message(error), 'error');
      set($('apTotal'), t('approvals.unavailable'), 'error');
      if (!state.inbox) {
        $('apEmpty').hidden = false;
        $('apEmpty').textContent = t('approvals.unavailableLead');
      }
    } finally {
      root.dataset.busy = 'false';
    }
  }

  async function loadPolicy() {
    try {
      state.policy = await gui.api('/api/approvals/policy').then((body) => body.data);
      paintPolicy();
    } catch (error) {
      set($('apPolicyStamp'), message(error), 'error');
    }
  }

  async function loadHistory() {
    try {
      state.history = await gui.api('/api/approvals/history?limit=40').then((body) => body.data);
      paintHistory();
      if (state.tab === 'diagnostics') paintDiagnostics();
    } catch (error) {
      set($('apHistoryCount'), message(error), 'error');
    }
  }

  function facts(host, rows) {
    host.replaceChildren();
    for (const [key, value] of rows) {
      const dt = document.createElement('dt');
      dt.textContent = key;
      const dd = document.createElement('dd');
      dd.textContent = value === null || value === undefined || value === '' ? '—' : String(value);
      host.append(dt, dd);
    }
  }

  function paintPolicy() {
    const policy = state.policy;
    if (!policy) return;
    set($('apPolicyStamp'), clock(policy.observed_at));
    const permissions = policy.permissions || {};
    facts($('apPermissions'), permissions.readable ? [
      [t('approvals.defaultMode'), permissions.default],
      [t('approvals.toolOverrides'), permissions.tools?.count ?? 0],
      [t('approvals.adapterBrowser'), onOff(permissions.computer?.browser)],
      [t('approvals.adapterOffice'), onOff(permissions.computer?.office)],
      [t('approvals.adapterEditor'), onOff(permissions.computer?.editor)],
      [t('approvals.adapterShell'), permissions.computer?.shell_backend || '—'],
    ] : [[t('approvals.unreadable'), permissions.error || '—']]);
    const modes = $('apToolModes');
    modes.replaceChildren();
    for (const row of permissions.tools?.rows || []) {
      const line = document.createElement('p');
      line.className = 'approvals-toolmode';
      line.textContent = `${row.tool}: ${row.mode}`;
      modes.append(line);
    }
    const autonomy = policy.autonomy || {};
    facts($('apAutonomy'), autonomy.readable ? [
      [t('approvals.autonomyLevel'), autonomy.level],
      [t('approvals.runtime'), onOff(autonomy.runtime_enabled)],
      [t('approvals.localModel'), autonomy.local_model || '—'],
      [t('approvals.maxQueue'), autonomy.max_queue],
      [t('approvals.ttl'), t('approvals.seconds', {count: autonomy.ttl_seconds})],
    ] : [[t('approvals.unreadable'), autonomy.error || '—']]);
    facts($('apStops'), [
      [t('approvals.latch'), policy.latch?.stopped ? t('approvals.latched') : t('approvals.clear')],
      [t('approvals.queue'), policy.queue_paused ? t('approvals.paused') : t('approvals.running')],
      [t('approvals.dnd'), onOff(policy.delivery?.quiet)],
      [t('approvals.delivery'), policy.delivery?.transport || 'local'],
    ]);
    $('apAlwaysReview').replaceChildren(...(policy.always_review || []).map((name) => line(name)));
    $('apNotChecked').replaceChildren(...(policy.not_checked || []).map((row) => line(localised(row))));
    if (!(policy.not_checked || []).length) $('apNotChecked').append(line(t('approvals.none')));
    paintDiagnostics();
  }

  /* Server rows carry a stable id plus English fallback; translate when we can. */
  function localised(row) {
    if (typeof row === 'string') return row;
    const key = `approvals.text.${row.id}`;
    const value = t(key);
    return value === key ? String(row.text || row.id) : value;
  }

  function line(text) {
    const li = document.createElement('li');
    li.textContent = text;
    return li;
  }
  const onOff = (value) => (value ? t('approvals.on') : t('approvals.off'));

  function paintHistory() {
    const history = state.history;
    if (!history) return;
    const host = $('apHistory');
    host.replaceChildren();
    const items = history.items || [];
    for (const row of items) {
      const item = document.createElement('article');
      item.className = 'approvals-historyrow';
      item.dataset.source = row.source || '';
      const head = document.createElement('div');
      head.className = 'approvals-historyhead';
      const time = document.createElement('time');
      time.textContent = stamp(row.at);
      const title = document.createElement('b');
      title.textContent = `${row.decision || '—'} · ${row.tool || row.reference || '—'}`;
      head.append(time, title);
      const meta = document.createElement('p');
      meta.className = 'approvals-meta';
      meta.textContent = [
        sourceLabel(row.source || 'action'),
        row.reference ? `${row.reference}` : '',
        row.scope ? t(`approvals.scope.${row.scope}`) : '',
        row.risk || '', row.recorded ? t('approvals.recordedIn', {where: row.recorded}) : '',
      ].filter(Boolean).join(' · ');
      item.append(head, meta);
      host.append(item);
    }
    $('apHistoryEmpty').hidden = items.length > 0;
    set($('apHistoryCount'), items.length ? t('approvals.recorded', {count: items.length}) : t('approvals.none'));
  }

  function paintDiagnostics() {
    const inbox = state.inbox;
    if (!inbox) return;
    facts($('apDiag'), [
      [t('approvals.observedAt'), stamp(inbox.observed_at)],
      [t('approvals.waitingTotal'), inbox.counts?.total ?? '—'],
      [t('approvals.itemBudget'), inbox.limits?.items ?? '—'],
      [t('approvals.textBudget'), inbox.limits?.text ?? '—'],
      [t('approvals.historyKept'), state.history?.total ?? '—'],
    ]);
    const errors = $('apDiagErrors');
    errors.replaceChildren(...(inbox.errors || []).map((text) => line(text)));
    if (!(inbox.errors || []).length) errors.append(line(t('approvals.noErrors')));
    const sources = $('apDiagSources');
    sources.replaceChildren(...(state.history?.sources || []).map((row) => line(`${row.name}: ${row.path} — ${row.note}`)));
    if (!(state.history?.sources || []).length) sources.append(line(t('approvals.historyPending')));
    const limits = $('apLimits');
    limits.replaceChildren(...(inbox.notes || []).map((row) => line(localised(row))));
  }

  function switchTab(name) {
    state.tab = name;
    root.querySelectorAll('[data-ap-tab]').forEach((node) => {
      node.setAttribute('aria-selected', String(node.dataset.apTab === name));
    });
    root.querySelectorAll('[data-ap-panel]').forEach((node) => { node.hidden = node.dataset.apPanel !== name; });
    if (name === 'policy' && !state.policy) loadPolicy().catch(() => {});
    /* a decision may have been answered since the last read, so History always
       re-reads when its tab opens instead of trusting a stale cache */
    if (name === 'history') loadHistory().catch(() => {});
    if (name === 'diagnostics') {
      if (!state.history) loadHistory().catch(() => {});
      paintDiagnostics();
    }
  }

  root.querySelectorAll('[data-ap-tab]').forEach((node) => { node.onclick = () => switchTab(node.dataset.apTab); });
  root.querySelector('[role="tablist"]').addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...root.querySelectorAll('[data-ap-tab]')];
    const index = tabs.findIndex((tab) => tab.dataset.apTab === state.tab);
    const next = event.key === 'Home' ? tabs[0]
      : event.key === 'End' ? tabs[tabs.length - 1]
        : tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
    next.focus();
    switchTab(next.dataset.apTab);
  });
  $('apRefresh').onclick = () => {
    load().catch(() => {});
    if (state.tab === 'policy') loadPolicy().catch(() => {});
    if (state.tab === 'history' || state.tab === 'diagnostics') loadHistory().catch(() => {});
  };
  $('apOpenOps').onclick = () => gui.show('operations');

  let timer = null;
  function schedule() {
    if (timer) window.clearInterval(timer);
    timer = null;
    if (gui.view !== 'approvals') return;
    timer = window.setInterval(() => {
      if (document.hidden || state.busy) return;
      if (state.tab === 'inbox') load({quiet: true}).catch(() => {});
    }, 8000);
  }
  window.addEventListener('nexus:view', (event) => {
    if (event.detail === 'approvals') {
      if (!state.inbox) load().catch(() => {});
      if (!state.policy) loadPolicy().catch(() => {});
      if (!state.history) loadHistory().catch(() => {});
    }
    schedule();
  });
  document.addEventListener('visibilitychange', schedule);
  i18n.onChange(() => {
    i18n.apply(root);
    if (state.inbox) { paintCounts(); paintFilters(); paintList({force: true}); paintDiagnostics(); }
    if (state.policy) paintPolicy();
    if (state.history) paintHistory();
    if (state.lastRead) set($('apUpdated'), t('approvals.updated', {time: clock(state.lastRead)}));
  });
  if (gui.view === 'approvals') load().catch(() => {});
  schedule();

  window.nexusApprovals = {
    load, loadPolicy, loadHistory, root, tab: switchTab,
    state: () => ({...state, inbox: state.inbox?.counts || null}),
  };
})();
