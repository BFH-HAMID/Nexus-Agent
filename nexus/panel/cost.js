/* Cost, models & performance: what the machine actually spent and how slow it was.

   Every number here is imported from recorded traces (see nexus/costcenter.py):
   tokens and cost from the per-call records the agent writes, latency measured
   between the wire and the response of the same call, resources from real
   psutil / nvidia-smi samples taken while this page is open.

   Nothing is estimated: a call without a user-provided price is shown as
   *unpriced*, never as free. A budget is only enforced when it is switched on,
   and it stops a run before the call that would cross it. */
(() => {
  'use strict';

  const gui = window.nexusGui;
  const i18n = window.nexusI18n;
  if (!gui || !i18n) return;
  const t = i18n.t;
  const $ = (id) => document.getElementById(id);

  const TABS = ['overview', 'latency', 'breakdown', 'budget', 'compare', 'resources'];
  const TOKENS = {save: 'SAVE COST POLICY', compare: 'START COMPARISON', clear: 'CLEAR COST HISTORY'};
  const GROUPS = ['model', 'agent', 'role', 'day'];

  const state = {
    tab: 'overview', overview: null, report: null, providers: null, resources: null,
    budget: null, latencyGroup: 'model', breakdownGroup: 'model', days: 7,
    selected: new Set(), job: null, jobPoll: 0, comparisons: [], visible: false,
    // A background refresh must never overwrite a field the user is editing.
    budgetDirty: false, resourcesDirty: false,
  };

  const root = document.createElement('section');
  root.id = 'view-cost';
  root.className = 'gui-modules security-page cost-page';
  root.dataset.guiView = 'cost';
  root.hidden = gui.view !== 'cost';
  root.innerHTML = `
    <header class="security-head">
      <div>
        <p class="security-eyebrow" data-i18n="cost.eyebrow"></p>
        <h1 data-i18n="cost.title"></h1>
        <p class="security-sub" data-i18n="cost.subtitle"></p>
      </div>
      <div class="security-quick" role="group" aria-label="Cost actions">
        <button class="btn" type="button" id="costRefresh"><span class="btn-label" data-i18n="cost.refresh"></span></button>
        <button class="btn ghost" type="button" id="costOpenWorkbench"><span class="btn-label" data-i18n="cost.openWorkbench"></span></button>
        <span class="security-stamp" id="costUpdated"></span>
      </div>
    </header>

    <p class="security-banner" id="costError" role="alert" hidden></p>

    <div class="security-toolbar">
      <div class="security-tabs" role="tablist" aria-label="Cost sections">
        <button type="button" role="tab" id="costTabOverview" data-cost-tab="overview" aria-selected="true" aria-controls="costPanelOverview" data-i18n="cost.tabOverview"></button>
        <button type="button" role="tab" id="costTabLatency" data-cost-tab="latency" aria-selected="false" aria-controls="costPanelLatency" data-i18n="cost.tabLatency"></button>
        <button type="button" role="tab" id="costTabBreakdown" data-cost-tab="breakdown" aria-selected="false" aria-controls="costPanelBreakdown" data-i18n="cost.tabBreakdown"></button>
        <button type="button" role="tab" id="costTabBudget" data-cost-tab="budget" aria-selected="false" aria-controls="costPanelBudget" data-i18n="cost.tabBudget"></button>
        <button type="button" role="tab" id="costTabCompare" data-cost-tab="compare" aria-selected="false" aria-controls="costPanelCompare" data-i18n="cost.tabCompare"></button>
        <button type="button" role="tab" id="costTabResources" data-cost-tab="resources" aria-selected="false" aria-controls="costPanelResources" data-i18n="cost.tabResources"></button>
      </div>
      <span class="security-chip" id="costChip" role="status" aria-live="polite">—</span>
    </div>

    <div class="security-panels">
      <section class="security-panel" id="costPanelOverview" role="tabpanel" aria-labelledby="costTabOverview" data-cost-panel="overview">
        <div class="security-kpis" id="costKpis"></div>

        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="cost.spend.title"></h2>
            <span class="security-stamp" id="costStoreLine"></span>
          </div>
          <p class="security-note" data-i18n="cost.spend.intro"></p>
          <div class="cost-bars" id="costBars"></div>
        </section>

        <section class="security-card">
          <div class="security-cardhead"><h2 data-i18n="cost.sources.title"></h2></div>
          <p class="security-note" data-i18n="cost.sources.intro"></p>
          <div class="memory-facts" id="costFacts"></div>
          <ul class="security-notes" id="costNotes"></ul>
        </section>
      </section>

      <section class="security-panel" id="costPanelLatency" role="tabpanel" aria-labelledby="costTabLatency" data-cost-panel="latency" hidden>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="cost.latency.title"></h2>
            <span class="security-stamp" id="costLatencyMeta"></span>
          </div>
          <p class="security-note" data-i18n="cost.latency.intro"></p>
          <div class="security-row">
            <label class="security-field"><span data-i18n="cost.range"></span>
              <select id="costDays"><option value="1">1</option><option value="7" selected>7</option>
                <option value="30">30</option><option value="90">90</option></select></label>
            <label class="security-field"><span data-i18n="cost.groupBy"></span>
              <select id="costLatencyGroup"></select></label>
          </div>
          <div class="cost-percentiles" id="costPercentiles"></div>
          <div class="security-tablewrap">
            <table class="security-table">
              <thead><tr>
                <th data-i18n="cost.colName"></th>
                <th data-i18n="cost.colCalls"></th>
                <th>p50</th><th>p90</th><th>p99</th>
                <th data-i18n="cost.colMin"></th>
                <th data-i18n="cost.colMax"></th>
              </tr></thead>
              <tbody id="costLatencyRows"></tbody>
            </table>
          </div>
        </section>
      </section>

      <section class="security-panel" id="costPanelBreakdown" role="tabpanel" aria-labelledby="costTabBreakdown" data-cost-panel="breakdown" hidden>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="cost.breakdown.title"></h2>
            <span class="security-stamp" id="costBreakdownMeta"></span>
          </div>
          <p class="security-note" data-i18n="cost.breakdown.intro"></p>
          <div class="security-row">
            <label class="security-field"><span data-i18n="cost.groupBy"></span>
              <select id="costBreakdownGroup"></select></label>
          </div>
          <div class="security-tablewrap">
            <table class="security-table">
              <thead><tr>
                <th data-i18n="cost.colName"></th>
                <th data-i18n="cost.colCalls"></th>
                <th data-i18n="cost.colInput"></th>
                <th data-i18n="cost.colOutput"></th>
                <th data-i18n="cost.colCost"></th>
                <th data-i18n="cost.colUnpriced"></th>
                <th data-i18n="cost.colLast"></th>
              </tr></thead>
              <tbody id="costBreakdownRows"></tbody>
            </table>
          </div>
        </section>
      </section>

      <section class="security-panel" id="costPanelBudget" role="tabpanel" aria-labelledby="costTabBudget" data-cost-panel="budget" hidden>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="cost.budget.title"></h2>
            <span class="security-stamp" id="costBudgetState"></span>
          </div>
          <p class="security-note" data-i18n="cost.budget.intro"></p>
          <div class="cost-bars" id="costBudgetBars"></div>
          <div class="memory-switches">
            <label class="security-switch"><input type="checkbox" id="costBudgetEnabled"><span data-i18n="cost.budget.enabled"></span></label>
            <label class="security-switch"><input type="checkbox" id="costBudgetStop"><span data-i18n="cost.budget.stop"></span></label>
            <label class="security-switch"><input type="checkbox" id="costBudgetUnknown"><span data-i18n="cost.budget.unknown"></span></label>
          </div>
          <div class="security-row">
            <label class="security-field"><span data-i18n="cost.budget.daily"></span>
              <input id="costDaily" type="number" min="0" step="0.01" inputmode="decimal"></label>
            <label class="security-field"><span data-i18n="cost.budget.monthly"></span>
              <input id="costMonthly" type="number" min="0" step="0.01" inputmode="decimal"></label>
            <button class="btn" type="button" id="costBudgetSave"><span class="btn-label" data-i18n="cost.budget.save"></span></button>
          </div>
          <ul class="security-notes" id="costBudgetNotes"></ul>
        </section>
        <section class="security-card">
          <div class="security-cardhead"><h2 data-i18n="cost.budget.howTitle"></h2></div>
          <p class="security-note" data-i18n="cost.budget.howIntro"></p>
          <div class="memory-facts" id="costBudgetFacts"></div>
        </section>
      </section>

      <section class="security-panel" id="costPanelCompare" role="tabpanel" aria-labelledby="costTabCompare" data-cost-panel="compare" hidden>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="cost.compare.title"></h2>
            <span class="security-stamp" id="costProvidersMeta"></span>
          </div>
          <p class="security-note" data-i18n="cost.compare.intro"></p>
          <p class="security-note cost-note" id="costCompareWarn" role="status" hidden></p>
          <fieldset class="cost-models">
            <legend data-i18n="cost.compare.models"></legend>
            <div id="costModelList" class="cost-modellist"></div>
          </fieldset>
          <label class="security-field"><span data-i18n="cost.compare.prompt"></span>
            <textarea id="costPrompt" rows="3" maxlength="2000" data-i18n-ph="cost.compare.promptHint"></textarea></label>
          <div class="security-row">
            <label class="security-field"><span data-i18n="cost.compare.maxTokens"></span>
              <input id="costMaxTokens" type="number" min="16" max="512" step="16" value="128"></label>
            <button class="btn" type="button" id="costCompareRun"><span class="btn-label" data-i18n="cost.compare.run"></span></button>
            <span class="security-stamp" id="costCompareState"></span>
          </div>
        </section>
        <section class="security-card" id="costResultCard" hidden>
          <div class="security-cardhead">
            <h2 data-i18n="cost.compare.resultTitle"></h2>
            <span class="security-stamp" id="costResultMeta"></span>
          </div>
          <div class="security-tablewrap">
            <table class="security-table">
              <thead><tr>
                <th data-i18n="cost.compare.colAgent"></th>
                <th data-i18n="cost.compare.colModel"></th>
                <th data-i18n="cost.colLatency"></th>
                <th data-i18n="cost.colInput"></th>
                <th data-i18n="cost.colOutput"></th>
                <th data-i18n="cost.colCost"></th>
                <th data-i18n="cost.compare.colAnswer"></th>
              </tr></thead>
              <tbody id="costResultRows"></tbody>
            </table>
          </div>
          <p class="security-note" data-i18n="cost.compare.resultNote"></p>
        </section>
        <section class="security-card">
          <div class="security-cardhead"><h2 data-i18n="cost.compare.historyTitle"></h2></div>
          <p class="security-note" data-i18n="cost.compare.historyIntro"></p>
          <ol class="cost-history" id="costHistory"></ol>
        </section>
      </section>

      <section class="security-panel" id="costPanelResources" role="tabpanel" aria-labelledby="costTabResources" data-cost-panel="resources" hidden>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="cost.resources.title"></h2>
            <span class="security-stamp" id="costResourceMeta"></span>
          </div>
          <p class="security-note" data-i18n="cost.resources.intro"></p>
          <div class="memory-switches">
            <label class="security-switch"><input type="checkbox" id="costResourceEnabled"><span data-i18n="cost.resources.enabled"></span></label>
          </div>
          <div class="security-row">
            <label class="security-field"><span data-i18n="cost.resources.interval"></span>
              <input id="costResourceInterval" type="number" min="5" max="300" step="5"></label>
            <label class="security-field"><span data-i18n="cost.resources.keep"></span>
              <input id="costResourceKeep" type="number" min="1" max="720" step="1"></label>
            <button class="btn" type="button" id="costResourceSave"><span class="btn-label" data-i18n="cost.resources.save"></span></button>
          </div>
          <div class="security-kpis cost-resource-kpis" id="costResourceKpis"></div>
          <p class="security-note" id="costResourceNote"></p>
        </section>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="cost.resources.historyTitle"></h2>
            <span class="security-stamp" id="costResourceCount"></span>
          </div>
          <div class="security-tablewrap">
            <table class="security-table">
              <thead><tr>
                <th data-i18n="cost.resources.colTime"></th>
                <th data-i18n="cost.resources.colCpu"></th>
                <th data-i18n="cost.resources.colRam"></th>
                <th data-i18n="cost.resources.colGpu"></th>
              </tr></thead>
              <tbody id="costResourceRows"></tbody>
            </table>
          </div>
        </section>
        <section class="security-card">
          <div class="security-cardhead"><h2 data-i18n="cost.clearTitle"></h2></div>
          <p class="security-note" data-i18n="cost.clearIntro"></p>
          <div class="security-row">
            <button class="btn ghost" type="button" id="costClear"><span class="btn-label" data-i18n="cost.clear"></span></button>
          </div>
        </section>
      </section>
    </div>`;
  (document.querySelector('main.deck') || document.body).after(root);

  const host = document.createElement('div');
  host.id = 'costActionHost';
  host.hidden = true;
  root.querySelector('.security-toolbar').after(host);

  /* ── helpers ────────────────────────────────────────────────────────── */
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  const message = (error) => error?.payload?.error?.message || error?.message || String(error);
  const usd = (value) => {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    if (n === 0) return '$0';
    if (Math.abs(n) < 0.01) return `$${n.toFixed(4)}`;
    if (Math.abs(n) < 1) return `$${n.toFixed(3)}`;
    return `$${n.toFixed(2)}`;
  };
  const ms = (value) => (value === null || value === undefined ? '—' : `${Number(value).toFixed(0)} ms`);
  const bytesOf = (size) => {
    const n = Number(size) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1048576).toFixed(1)} MB`;
    return `${(n / 1073741824).toFixed(2)} GB`;
  };
  const stampOf = (value) => {
    if (!value) return '—';
    const date = new Date(value * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    const options = {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'};
    if (date.getFullYear() !== new Date().getFullYear()) options.year = 'numeric';
    return date.toLocaleString([], options);
  };
  const number = (value) => (value === null || value === undefined ? '—' : Number(value).toLocaleString());
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  function showError(error) {
    const banner = $('costError');
    banner.textContent = error ? message(error) : '';
    banner.hidden = !error;
  }
  async function guard(work) {
    try {
      showError(null);
      await work();
    } catch (error) {
      showError(error);
    }
  }
  /* A shared read limit is a "come back in N seconds", not a broken page: wait
     the advertised second once and try again before showing the error. */
  const RETRY = /retry in (\d+)\s*s/i;
  const api = async (path, body, attempt = 0) => {
    try {
      const reply = await gui.api(path, body);
      return reply.data;
    } catch (error) {
      const wait = RETRY.exec(message(error));
      if (!wait || attempt > 0) throw error;
      await new Promise((done) => setTimeout(done, Math.min(Number(wait[1]) || 5, 10) * 1000));
      return api(path, body, attempt + 1);
    }
  };

  /* ── confirm sheet ──────────────────────────────────────────────────── */
  function closeAction() {
    host.hidden = true;
    host.replaceChildren();
  }
  function openAction({title, token, detail, onConfirm, expected}) {
    host.hidden = false;
    host.innerHTML = `
      <section class="security-card security-confirm" role="group" aria-labelledby="costActionTitle">
        <div class="security-cardhead"><h2 id="costActionTitle">${esc(title)}</h2></div>
        <p class="security-note">${esc(detail || '')}</p>
        <p class="security-code" id="costActionExpected">${esc(expected || token)}</p>
        <label class="security-field"><span data-i18n="cost.confirm.tokenLabel"></span>
          <input id="costActionToken" type="text" autocomplete="off" spellcheck="false" autocapitalize="characters"></label>
        <div class="security-confirmrow">
          <button class="btn" type="button" id="costActionGo" disabled><span class="btn-label" data-i18n="cost.confirm.go"></span></button>
          <button class="btn ghost" type="button" id="costActionCancel"><span class="btn-label" data-i18n="cost.confirm.cancel"></span></button>
        </div>
        <p class="security-note" id="costActionError" role="alert"></p>
      </section>`;
    i18n.apply(host);
    const input = $('costActionToken');
    const go = $('costActionGo');
    const want = expected || token;
    input.focus();
    input.oninput = () => { go.disabled = input.value.trim() !== want; };
    go.onclick = async () => {
      if (go.disabled) return;
      go.disabled = true;
      $('costActionError').textContent = '';
      try {
        await onConfirm({confirmation: want});
        closeAction();
      } catch (error) {
        $('costActionError').textContent = message(error);
        go.disabled = false;
      }
    };
    $('costActionCancel').onclick = closeAction;
    host.scrollIntoView({block: 'nearest'});
  }

  /* ── tabs ───────────────────────────────────────────────────────────── */
  function switchTab(tab) {
    if (!TABS.includes(tab)) return;
    state.tab = tab;
    for (const name of TABS) {
      const panel = $(`costPanel${name[0].toUpperCase()}${name.slice(1)}`);
      const button = $(`costTab${name[0].toUpperCase()}${name.slice(1)}`);
      const on = name === tab;
      if (panel) panel.hidden = !on;
      if (button) button.setAttribute('aria-selected', String(on));
    }
    closeAction();
    if (!state.visible) return;  // nothing is fetched for a page nobody is looking at
    if (tab === 'overview') loadOverview().catch(showError);
    if (tab === 'latency') loadLatency().catch(showError);
    if (tab === 'breakdown') loadBreakdown().catch(showError);
    if (tab === 'budget') loadBudget().catch(showError);
    if (tab === 'compare') loadCompare().catch(showError);
    if (tab === 'resources') loadResources().catch(showError);
  }

  /* ── overview ───────────────────────────────────────────────────────── */
  function kpi(label, value, tone) {
    const node = el('div', 'security-count');
    if (tone) node.dataset.tone = tone;
    node.append(el('b', '', value), el('span', '', label));
    return node;
  }
  function bar(label, used, budget, detail) {
    const row = el('div', 'cost-bar');
    const head = el('div', 'cost-barhead');
    head.append(el('span', '', label), el('span', 'security-mono', detail));
    const track = el('div', 'memory-budget');
    const fill = el('span');
    const ratio = budget > 0 ? Math.min(1, used / budget) : (used > 0 ? 1 : 0);
    fill.style.width = `${Math.round(ratio * 100)}%`;
    fill.dataset.tone = ratio >= 0.9 ? 'warn' : 'ok';
    track.append(fill);
    row.append(head, track);
    return row;
  }

  function paintOverview() {
    const data = state.overview;
    if (!data) return;
    const budget = data.budget || {};
    const today = budget.today || {};
    const month = budget.month || {};
    const totals = data.totals || {};
    const latency = data.latency || {};
    $('costKpis').replaceChildren(
      kpi(t('cost.kpi.today'), usd(today.usd || 0), budget.enabled ? 'ok' : ''),
      kpi(t('cost.kpi.month'), usd(month.usd || 0), budget.enabled ? 'ok' : ''),
      kpi(t('cost.kpi.callsToday'), number(today.calls || 0), ''),
      kpi(t('cost.kpi.unpriced'), number(today.unpriced || 0), (today.unpriced || 0) ? 'warn' : ''),
      kpi(t('cost.kpi.p50'), ms(latency.overall?.p50), ''),
      kpi(t('cost.kpi.models'), number(totals.models_30d || 0), ''),
    );
    $('costBars').replaceChildren(
      bar(t('cost.spend.today'), Number(today.usd || 0), Number(budget.daily_usd || 0),
        t('cost.spend.of', {used: usd(today.usd || 0), budget: usd(budget.daily_usd || 0)})),
      bar(t('cost.spend.month'), Number(month.usd || 0), Number(budget.monthly_usd || 0),
        t('cost.spend.of', {used: usd(month.usd || 0), budget: usd(budget.monthly_usd || 0)})),
    );
    const facts = $('costFacts');
    facts.replaceChildren();
    for (const [term, value] of [
      [t('cost.fact.store'), data.store?.path || '—'],
      [t('cost.fact.size'), bytesOf(data.store?.bytes || 0)],
      [t('cost.fact.first'), data.totals?.first_sample_at ? stampOf(data.totals.first_sample_at) : '—'],
      [t('cost.fact.runs'), number(totals.runs_30d || 0)],
      [t('cost.fact.samples'), number(latency.samples || 0)],
      [t('cost.fact.policy'), data.file || '—'],
    ]) {
      facts.append(el('span', 'memory-fact-term', term), el('span', 'memory-fact-value', String(value)));
    }
    const notes = $('costNotes');
    notes.replaceChildren();
    for (const key of ['traces_only', 'unknown_is_not_free', 'latency_wire_to_response', 'resources_while_open']) {
      notes.append(el('li', '', t(`cost.notes.${key}`)));
    }
    if (data.policy_error) notes.append(el('li', '', data.policy_error));
    $('costStoreLine').textContent = data.store?.exists
      ? t('cost.storeLine', {path: data.store.path, size: bytesOf(data.store.bytes)})
      : t('cost.storeMissing');
    $('costUpdated').textContent = t('cost.updated', {when: stampOf(data.observed_at)});
    $('costChip').textContent = t('cost.chip', {
      budget: `${usd(budget.daily_usd)} ${budget.enabled ? t('cost.on') : t('cost.off')}`,
      stop: budget.stop ? t('cost.on') : t('cost.off'),
      spend: usd(today.usd || 0)});
  }

  async function loadOverview() {
    state.overview = await api('/api/cost/overview');
    state.report = state.overview.latency ? {latency: state.overview.latency} : null;
    paintOverview();
  }

  /* ── latency ────────────────────────────────────────────────────────── */
  function paintLatency() {
    const data = state.report?.latency;
    if (!data) return;
    const percentiles = $('costPercentiles');
    percentiles.replaceChildren(
      el('div', 'cost-percentile', `${t('cost.samples')}: ${number(data.samples || 0)}`),
      el('div', 'cost-percentile', `p50 · ${ms(data.overall?.p50)}`),
      el('div', 'cost-percentile', `p90 · ${ms(data.overall?.p90)}`),
      el('div', 'cost-percentile', `p99 · ${ms(data.overall?.p99)}`),
    );
    $('costLatencyMeta').textContent = t('cost.latency.meta', {
      days: state.days, calls: number(data.overall?.calls || 0)});
    const tbody = $('costLatencyRows');
    tbody.replaceChildren();
    if (!data.rows?.length) {
      const tr = el('tr');
      const td = el('td', 'security-empty', t('cost.latency.empty'));
      td.colSpan = 7;
      tr.append(td);
      tbody.append(tr);
      return;
    }
    for (const row of data.rows) {
      const tr = el('tr');
      tr.append(el('td', '', row.name), el('td', 'security-mono', number(row.calls)),
        el('td', 'security-mono', ms(row.p50)), el('td', 'security-mono', ms(row.p90)),
        el('td', 'security-mono', ms(row.p99)), el('td', 'security-mono', ms(row.min)),
        el('td', 'security-mono', ms(row.max)));
      tbody.append(tr);
    }
  }

  async function loadLatency() {
    state.report = await api(`/api/cost/report?days=${state.days}`);
    paintLatency();
  }

  /* ── breakdown ──────────────────────────────────────────────────────── */
  function paintBreakdown() {
    const data = state.report;
    if (!data) return;
    const key = `by_${state.breakdownGroup}`;
    const rows = data[key] || [];
    const totals = data.totals?.[state.breakdownGroup] || {};
    // A window where nothing carries a price is not "$0 spent": it is unknown.
    const spread = (count, unpriced) => (Number(count) > 0 && Number(unpriced) >= Number(count) ? '—' : null);
    $('costBreakdownMeta').textContent = t('cost.breakdown.meta', {
      days: state.days, calls: number(totals.calls || 0),
      cost: spread(totals.calls, totals.unpriced) || usd(totals.usd || 0)});
    const tbody = $('costBreakdownRows');
    tbody.replaceChildren();
    if (!rows.length) {
      const tr = el('tr');
      const td = el('td', 'security-empty', t('cost.breakdown.empty'));
      td.colSpan = 7;
      tr.append(td);
      tbody.append(tr);
      return;
    }
    for (const row of rows) {
      const tr = el('tr');
      const unknown = spread(row.calls, row.unpriced);
      const cost = el('td', 'security-mono', unknown || usd(row.usd));
      if (unknown) cost.dataset.tone = 'muted';
      if (row.unpriced) cost.title = t('cost.breakdown.unpricedHint', {count: row.unpriced});
      tr.append(el('td', '', row.name), el('td', 'security-mono', number(row.calls)),
        el('td', 'security-mono', number(row.input)), el('td', 'security-mono', number(row.output)),
        cost, el('td', 'security-mono', number(row.unpriced)),
        el('td', 'security-mono', stampOf(row.last_at)));
      tbody.append(tr);
    }
  }

  async function loadBreakdown() {
    state.report = await api(`/api/cost/report?days=${state.days}`);
    paintBreakdown();
  }

  /* ── budget ─────────────────────────────────────────────────────────── */
  function paintBudget() {
    const data = state.budget;
    const overview = state.overview;
    const policy = overview?.policy?.budget || {};
    if (!data) return;
    if (!state.budgetDirty) {
      $('costBudgetEnabled').checked = Boolean(data.enabled);
      $('costBudgetStop').checked = Boolean(data.stop);
      $('costBudgetUnknown').checked = Boolean(data.unknown_price_blocks);
      $('costDaily').value = String(data.daily_usd ?? 5);
      $('costMonthly').value = String(data.monthly_usd ?? 50);
    }
    $('costBudgetState').textContent = data.enforced
      ? t('cost.budget.enforced')
      : (data.enabled ? t('cost.budget.notStopping') : t('cost.budget.off'));
    $('costBudgetState').dataset.tone = data.enforced ? 'ok' : '';
    $('costBudgetBars').replaceChildren(
      bar(t('cost.spend.today'), Number(data.today?.usd || 0), Number(data.daily_usd || 0),
        t('cost.budget.remaining', {left: usd(data.today?.remaining || 0)})),
      bar(t('cost.spend.month'), Number(data.month?.usd || 0), Number(data.monthly_usd || 0),
        t('cost.budget.remaining', {left: usd(data.month?.remaining || 0)})),
    );
    const notes = $('costBudgetNotes');
    notes.replaceChildren();
    for (const key of ['hardStop', 'unknown', 'noPrice']) notes.append(el('li', '', t(`cost.budget.notes.${key}`)));
    if (data.error) notes.append(el('li', '', data.error));
    const facts = $('costBudgetFacts');
    facts.replaceChildren();
    for (const [term, value] of [
      [t('cost.budget.todayPriced'), number(data.today?.priced || 0)],
      [t('cost.budget.todayUnpriced'), number(data.today?.unpriced || 0)],
      [t('cost.budget.monthCalls'), number(data.month?.calls || 0)],
      [t('cost.budget.policyFile'), overview?.file || '—'],
      [t('cost.budget.token'), t('cost.budget.tokenHint')],
    ]) {
      facts.append(el('span', 'memory-fact-term', term), el('span', 'memory-fact-value', String(value)));
    }
  }

  async function loadBudget() {
    if (!state.overview) await loadOverview();
    state.budget = await api('/api/cost/budget');
    paintBudget();
  }

  function budgetPayload() {
    const body = {
      enabled: $('costBudgetEnabled').checked,
      stop: $('costBudgetStop').checked,
      unknown_price_blocks: $('costBudgetUnknown').checked,
      daily_usd: Number($('costDaily').value),
      monthly_usd: Number($('costMonthly').value),
    };
    if (!Number.isFinite(body.daily_usd) || !Number.isFinite(body.monthly_usd)) {
      throw Error(t('cost.budget.badNumber'));
    }
    return body;
  }

  function askSaveBudget() {
    let budget;
    try { budget = budgetPayload(); } catch (error) { showError(error); return; }
    openAction({
      title: t('cost.budget.saveTitle'),
      token: TOKENS.save,
      detail: t('cost.budget.saveDetail', {daily: usd(budget.daily_usd), monthly: usd(budget.monthly_usd)}),
      onConfirm: async (payload) => {
        const saved = await api('/api/cost/policy/save', {
          ...payload, budget, resources: state.overview?.policy?.resources,
          revision: state.overview?.revision || ''});
        state.budgetDirty = false;
        state.budget = await api('/api/cost/budget');
        await loadOverview();
        paintBudget();
        window.nexusToast?.(t('cost.budget.saved'), {tone: 'done'});
        return saved;
      },
    });
  }

  function askSaveResources() {
    const resources = {
      enabled: $('costResourceEnabled').checked,
      interval_s: Number($('costResourceInterval').value),
      keep_hours: Number($('costResourceKeep').value),
    };
    openAction({
      title: t('cost.resources.saveTitle'),
      token: TOKENS.save,
      detail: t('cost.resources.saveDetail'),
      onConfirm: async (payload) => {
        await api('/api/cost/policy/save', {
          ...payload, resources, budget: state.overview?.policy?.budget,
          revision: state.overview?.revision || ''});
        state.resourcesDirty = false;
        await loadOverview();
        await loadResources();
        window.nexusToast?.(t('cost.resources.saved'), {tone: 'done'});
      },
    });
  }

  /* ── compare ────────────────────────────────────────────────────────── */
  function paintProviders() {
    const data = state.providers;
    if (!data) return;
    const list = $('costModelList');
    list.replaceChildren();
    for (const agent of data.agents || []) {
      const label = el('label', 'cost-model');
      const box = el('input');
      box.type = 'checkbox';
      box.value = agent.name;
      box.checked = state.selected.has(agent.name);
      box.onchange = () => {
        if (box.checked) state.selected.add(agent.name);
        else state.selected.delete(agent.name);
        paintCompareState();
      };
      const text = el('span', 'cost-modeltext');
      text.append(el('b', '', agent.label), el('small', '', `${agent.provider} · ${agent.model}`));
      label.append(box, text);
      const chips = el('span', 'cost-chiprow');
      if (agent.local) chips.append(el('span', 'memory-chip', t('cost.compare.local')));
      if (!agent.key_present && !agent.local) {
        const missing = el('span', 'memory-chip', t('cost.compare.noKey'));
        missing.dataset.tone = 'warn';
        chips.append(missing);
      }
      if (agent.priced) chips.append(el('span', 'memory-chip', t('cost.compare.priced')));
      label.append(chips);
      list.append(label);
    }
    $('costProvidersMeta').textContent = data.local_only
      ? t('cost.compare.localOnly')
      : t('cost.compare.count', {count: (data.agents || []).length});
    const warn = $('costCompareWarn');
    const priced = (data.agents || []).filter((agent) => agent.priced).length;
    warn.hidden = !(data.agents || []).length;
    warn.textContent = (data.agents || []).length
      ? t('cost.compare.warn', {priced, total: (data.agents || []).length})
      : t('cost.compare.empty');
    paintCompareState();
  }

  function paintCompareState() {
    const chosen = state.selected.size;
    // Kept clickable on purpose: a dead button explains nothing, the guard names the rule.
    $('costCompareRun').disabled = state.job?.status === 'running';
    $('costCompareState').textContent = t('cost.compare.chosen', {count: chosen});
  }

  function paintResults(job) {
    const card = $('costResultCard');
    const result = job?.result;
    if (!result) { card.hidden = true; return; }
    card.hidden = false;
    $('costResultMeta').textContent = t('cost.compare.resultMeta', {
      when: stampOf(result.created), tokens: result.max_tokens});
    const tbody = $('costResultRows');
    tbody.replaceChildren();
    for (const row of result.results || []) {
      const tr = el('tr');
      if (!row.ok) tr.dataset.state = 'error';
      const answer = el('td', 'security-target');
      answer.textContent = row.ok ? (row.text || '—') : (row.error || t('cost.compare.failed'));
      answer.title = answer.textContent;
      tr.append(el('td', '', row.agent), el('td', 'security-mono', row.model || '—'),
        el('td', 'security-mono', ms(row.latency_ms)),
        el('td', 'security-mono', number(row.input_tokens)),
        el('td', 'security-mono', number(row.output_tokens)),
        el('td', 'security-mono', row.cost_usd === null ? t('cost.unpriced') : usd(row.cost_usd)),
        answer);
      tbody.append(tr);
    }
  }

  async function pollJob() {
    if (!state.job?.id) return;
    try {
      const found = await api(`/api/cost/compare?id=${encodeURIComponent(state.job.id)}`);
      state.job = {...state.job, ...found};
      $('costCompareState').textContent = found.status === 'running'
        ? t('cost.compare.running')
        : (found.status === 'error' ? t('cost.compare.jobError') : t('cost.compare.done'));
      if (found.status !== 'running') {
        paintResults(found);
        paintCompareState();
        await loadComparisons();
        if (found.error) showError(found.error);
        return;
      }
    } catch (error) {
      // A 400 for an unknown id just means the job has not stored a row yet.
      if (!/No stored comparison/.test(message(error))) showError(error);
    }
    state.jobPoll = window.setTimeout(() => pollJob(), 1500);
  }

  function askCompare() {
    const names = [...state.selected];
    if (names.length < 2 || names.length > 4) { showError(t('cost.compare.choose')); return; }
    const prompt = $('costPrompt').value.trim();
    if (!prompt) { showError(t('cost.compare.emptyPrompt')); return; }
    const maxTokens = Number($('costMaxTokens').value) || 128;
    openAction({
      title: t('cost.compare.confirmTitle'),
      token: TOKENS.compare,
      detail: t('cost.compare.confirmDetail', {models: names.join(', '), tokens: maxTokens}),
      onConfirm: async (payload) => {
        const started = await api('/api/cost/compare', {...payload, agents: names, prompt, max_tokens: maxTokens});
        state.job = started.job || {id: '', status: 'running'};
        $('costCompareState').textContent = t('cost.compare.running');
        paintCompareState();
        pollJob();
      },
    });
  }

  async function loadComparisons() {
    state.comparisons = await api('/api/cost/comparisons?limit=20');
    const list = $('costHistory');
    list.replaceChildren();
    if (!state.comparisons.length) {
      list.append(el('li', 'security-empty', t('cost.compare.noHistory')));
      return;
    }
    for (const item of state.comparisons) {
      const row = el('li', 'cost-history-row');
      const button = el('button', 'btn ghost security-small', t('cost.compare.open'));
      button.type = 'button';
      button.onclick = () => guard(async () => {
        const found = await api(`/api/cost/compare?id=${encodeURIComponent(item.id)}`);
        paintResults(found);
        $('costResultCard').scrollIntoView({block: 'nearest'});
      });
      row.append(el('span', 'security-mono', stampOf(item.created)),
        el('span', 'security-target', item.prompt || '—'),
        el('span', 'security-mono', (item.models || []).join(' · ')), button);
      list.append(row);
    }
  }

  async function loadCompare() {
    state.providers = await api('/api/cost/providers');
    paintProviders();
    await loadComparisons();
    if (state.job?.result) paintResults(state.job);
  }

  /* ── resources ──────────────────────────────────────────────────────── */
  function paintResources() {
    const data = state.resources;
    if (!data) return;
    const policy = data.policy || {};
    if (!state.resourcesDirty) {
      $('costResourceEnabled').checked = Boolean(policy.enabled);
      $('costResourceInterval').value = String(policy.interval_s ?? 15);
      $('costResourceKeep').value = String(policy.keep_hours ?? 48);
    }
    const sample = data.sample;
    const latest = sample || (data.history || [])[data.history.length - 1];
    const ram = latest && latest.ram_total ? Math.round((latest.ram_used / latest.ram_total) * 100) : null;
    $('costResourceKpis').replaceChildren(
      kpi(t('cost.resources.cpu'), latest?.cpu === null || latest?.cpu === undefined ? '—' : `${Number(latest.cpu).toFixed(1)}%`),
      kpi(t('cost.resources.ram'), ram === null ? '—' : `${ram}%`),
      kpi(t('cost.resources.gpu'), latest?.gpu_util === null || latest?.gpu_util === undefined ? t('cost.resources.none')
        : `${Number(latest.gpu_util).toFixed(0)}%`),
      kpi(t('cost.resources.samples'), number((data.history || []).length)),
    );
    $('costResourceMeta').textContent = data.error || (policy.enabled ? t('cost.resources.on') : t('cost.resources.off'));
    $('costResourceNote').textContent = sample?.error || data.note || '';
    $('costResourceCount').textContent = t('cost.resources.count', {count: (data.history || []).length});
    const tbody = $('costResourceRows');
    tbody.replaceChildren();
    const rows = (data.history || []).slice(-40).reverse();
    if (!rows.length) {
      const tr = el('tr');
      const td = el('td', 'security-empty', t('cost.resources.empty'));
      td.colSpan = 4;
      tr.append(td);
      tbody.append(tr);
      return;
    }
    for (const row of rows) {
      const tr = el('tr');
      const used = row.ram_total ? `${(row.ram_used / 1073741824).toFixed(1)} / ${(row.ram_total / 1073741824).toFixed(1)} GB` : '—';
      const gpu = row.gpu_util === null || row.gpu_util === undefined
        ? '—' : `${Number(row.gpu_util).toFixed(0)}% · ${number(row.gpu_mem_used)} MiB`;
      tr.append(el('td', 'security-mono', stampOf(row.at)),
        el('td', 'security-mono', row.cpu === null ? '—' : `${Number(row.cpu).toFixed(1)}%`),
        el('td', 'security-mono', used), el('td', 'security-mono', gpu));
      tbody.append(tr);
    }
  }

  async function loadResources() {
    if (!state.overview) await loadOverview();
    state.resources = await api('/api/cost/resources');
    paintResources();
  }

  /* ── wiring ─────────────────────────────────────────────────────────── */
  for (const group of [['costLatencyGroup', 'latencyGroup'], ['costBreakdownGroup', 'breakdownGroup']]) {
    const select = $(group[0]);
    for (const name of GROUPS) {
      const option = document.createElement('option');
      option.value = name;
      option.textContent = t(`cost.group.${name}`);
      select.append(option);
    }
    select.value = state[group[1]];
    select.onchange = () => {
      state[group[1]] = select.value;
      if (group[1] === 'latencyGroup') loadLatency().catch(showError);
      else loadBreakdown().catch(showError);
    };
  }
  $('costDays').onchange = () => {
    state.days = Number($('costDays').value) || 7;
    if (state.tab === 'latency') loadLatency().catch(showError);
    else loadBreakdown().catch(showError);
  };
  for (const tab of TABS) {
    const button = $(`costTab${tab[0].toUpperCase()}${tab.slice(1)}`);
    if (button) button.onclick = () => switchTab(tab);
  }
  $('costRefresh').onclick = () => guard(async () => { await loadOverview(); switchTab(state.tab); });
  $('costOpenWorkbench').onclick = () => {
    if (typeof window.nexusWorkbenchShow === 'function') window.nexusWorkbenchShow('models');
    else $('guiSettings')?.click();
  };
  for (const id of ['costBudgetEnabled', 'costBudgetStop', 'costBudgetUnknown', 'costDaily', 'costMonthly']) {
    $(id).addEventListener('input', () => { state.budgetDirty = true; });
  }
  for (const id of ['costResourceEnabled', 'costResourceInterval', 'costResourceKeep']) {
    $(id).addEventListener('input', () => { state.resourcesDirty = true; });
  }
  $('costBudgetSave').onclick = () => guard(askSaveBudget);
  $('costResourceSave').onclick = () => guard(askSaveResources);
  $('costCompareRun').onclick = () => guard(askCompare);
  $('costClear').onclick = () => openAction({
    title: t('cost.clearTitle'),
    token: TOKENS.clear,
    detail: t('cost.clearDetail'),
    onConfirm: async (payload) => {
      await api('/api/cost/clear', payload);
      state.overview = null;
      await loadOverview();
      await loadResources().catch(() => {});
      window.nexusToast?.(t('cost.cleared'), {tone: 'done'});
    },
  });

  i18n.onChange(() => {
    i18n.apply(root);
    i18n.apply(host);
    paintOverview();
    paintLatency();
    paintBreakdown();
    paintBudget();
    paintProviders();
    paintResources();
    if (state.job?.result) paintResults(state.job);
  });

  window.addEventListener('nexus:view', (event) => {
    if (event.detail !== 'cost') { state.visible = false; return; }
    state.visible = true;
    switchTab(state.tab);
  });

  const initial = new URLSearchParams(location.search).get('tab');
  switchTab(TABS.includes(initial) ? initial : 'overview');
  // A finished run changes the ledger; refresh the page the user is looking at.
  window.addEventListener('nexus:event', ({ detail }) => {
    if (!state.visible || !['done', 'error'].includes(detail?.type)) return;
    switchTab(state.tab);
  });

  window.nexusCost = {
    root,
    tab: switchTab,
    load: loadOverview,
    state: () => ({
      tab: state.tab, enabled: state.overview?.budget?.enabled ?? null,
      stop: state.overview?.budget?.stop ?? null,
      spendToday: state.overview?.budget?.today?.usd ?? null,
      calls: state.overview?.budget?.today?.calls ?? null,
      unpriced: state.overview?.budget?.today?.unpriced ?? null,
      p50: state.overview?.latency?.overall?.p50 ?? null,
      models: state.providers?.agents?.length ?? null,
      job: state.job?.status ?? null,
    }),
  };
})();
