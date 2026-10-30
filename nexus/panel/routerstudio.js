/* Router Studio: inspect the keyword stage, review rules, read the fallback
   ladders, score a batch of tasks offline and see what evidence exists.

   Every value comes from a read surface (`/api/router/overview`, `/rules`,
   `/chains`, `/models`, `/accuracy`, `/lab`) or the offline dry run
   (`/api/router/simulate`). The only writes are reviewed: a prepared rule plus
   a typed `SAVE RULE <id>`, a typed `SAVE WEIGHTS`, and explicit confirmations
   for enable/remove. Nothing here starts a task or calls a model. */
(() => {
  'use strict';

  const gui = window.nexusGui;
  const i18n = window.nexusI18n;
  if (!gui || !i18n) return;
  const t = i18n.t;
  const $ = (id) => document.getElementById(id);

  const state = {
    tab: 'policy',
    overview: null, policy: null, settings: null, roster: null, chainSummary: null, chains: null,
    models: null, accuracy: null, lab: null, roleNames: [], rules: [],
    weightsDirty: false, pending: null, editing: null,
    observed_at: 0, busy: false, error: '',
  };

  const root = document.createElement('section');
  root.id = 'view-router';
  root.className = 'gui-modules router-page';
  root.dataset.guiView = 'router';
  root.hidden = gui.view !== 'router';
  root.dataset.busy = 'false';
  root.innerHTML = `
    <header class="router-head">
      <div>
        <p class="router-eyebrow" data-i18n="router.eyebrow">Router studio</p>
        <h1 data-i18n="router.title">How a task becomes a role</h1>
        <p class="router-sub" data-i18n="router.subtitle"></p>
      </div>
      <div class="router-quick" role="group" data-i18n-aria="router.actionsAria">
        <button class="btn" type="button" id="rtRefresh"><span class="btn-label" data-i18n="router.refresh"></span></button>
        <button class="btn ghost" type="button" id="rtRoles"><span class="btn-label" data-i18n="router.roles"></span></button>
        <button class="btn ghost" type="button" id="rtOpenModels"><span class="btn-label" data-i18n="router.workbench"></span></button>
      </div>
    </header>

    <div class="router-toolbar">
      <div class="router-tabs" role="tablist" data-i18n-aria="router.tabsAria">
        <button type="button" role="tab" id="rtTabPolicy" data-rt-tab="policy" aria-selected="true" data-i18n="router.tabPolicy"></button>
        <button type="button" role="tab" id="rtTabRules" data-rt-tab="rules" aria-selected="false" data-i18n="router.tabRules"></button>
        <button type="button" role="tab" id="rtTabChains" data-rt-tab="chains" aria-selected="false" data-i18n="router.tabChains"></button>
        <button type="button" role="tab" id="rtTabLab" data-rt-tab="lab" aria-selected="false" data-i18n="router.tabLab"></button>
        <button type="button" role="tab" id="rtTabAccuracy" data-rt-tab="accuracy" aria-selected="false" data-i18n="router.tabAccuracy"></button>
      </div>
      <span class="router-stamp" id="rtUpdated" data-i18n="router.never"></span>
    </div>

    <div class="router-body">
      <section class="router-panel" data-rt-panel="policy" id="rtPanelPolicy">
        <div class="router-grid">
          <article class="router-card">
            <h2 data-i18n="router.settings"></h2>
            <p class="router-hint" data-i18n="router.settingsHint"></p>
            <dl class="router-facts" id="rtFacts"></dl>
          </article>
          <article class="router-card">
            <h2 data-i18n="router.weights"></h2>
            <p class="router-hint" data-i18n="router.weightsHint"></p>
            <div class="router-weights" id="rtWeights">
              <label><span data-i18n="router.weight.cost"></span>
                <input type="range" id="rtWeightCost" min="0" max="100" step="5" value="40"><output id="rtWeightCostOut">40</output></label>
              <label><span data-i18n="router.weight.speed"></span>
                <input type="range" id="rtWeightSpeed" min="0" max="100" step="5" value="40"><output id="rtWeightSpeedOut">40</output></label>
              <label><span data-i18n="router.weight.quality"></span>
                <input type="range" id="rtWeightQuality" min="0" max="100" step="5" value="20"><output id="rtWeightQualityOut">20</output></label>
            </div>
            <p class="router-hint" id="rtWeightSum"></p>
            <div class="router-actions">
              <button class="btn" type="button" id="rtSaveWeights"><span class="btn-label" data-i18n="router.saveWeights"></span></button>
              <span class="router-status" id="rtWeightsStatus" role="status"></span>
            </div>
          </article>
        </div>

        <article class="router-card">
          <div class="router-cardhead">
            <h2 data-i18n="router.customRules"></h2>
            <span class="router-chip" id="rtRulesChip">—</span>
          </div>
          <p class="router-hint" id="rtRulesSummary"></p>
          <div class="router-actions">
            <button class="btn" type="button" id="rtEnableRules"><span class="btn-label" data-i18n="router.enableRules"></span></button>
            <button class="btn ghost" type="button" id="rtOpenRules"><span class="btn-label" data-i18n="router.openRules"></span></button>
            <span class="router-status" id="rtRulesStatus" role="status"></span>
          </div>
        </article>

        <article class="router-card">
          <div class="router-cardhead">
            <h2 data-i18n="router.models"></h2>
            <span class="router-chip" id="rtCurrentModel">—</span>
          </div>
          <p class="router-hint" data-i18n="router.modelsHint"></p>
          <div class="router-tablewrap">
            <table class="router-table">
              <thead><tr>
                <th data-i18n="router.modelAgent"></th>
                <th data-i18n="router.modelProvider"></th>
                <th data-i18n="router.modelCost"></th>
                <th data-i18n="router.modelSpeed"></th>
                <th data-i18n="router.modelQuality"></th>
                <th data-i18n="router.modelCoverage"></th>
                <th data-i18n="router.modelScore"></th>
              </tr></thead>
              <tbody id="rtModelBody"></tbody>
            </table>
          </div>
          <p class="router-hint" id="rtModelMeta"></p>
        </article>
      </section>

      <section class="router-panel" data-rt-panel="rules" id="rtPanelRules" hidden>
        <div class="router-grid router-grid-rules">
          <article class="router-card">
            <div class="router-cardhead">
              <h2 data-i18n="router.ruleList"></h2>
              <span class="router-chip" id="rtRuleCount">—</span>
            </div>
            <div class="router-list" id="rtRuleList" role="list"></div>
            <p class="router-hint" id="rtRuleEmpty" data-i18n="router.ruleEmpty" hidden></p>
          </article>

          <article class="router-card">
            <div class="router-cardhead">
              <h2 id="rtRuleTitle" data-i18n="router.ruleTitleNew"></h2>
              <button class="btn ghost" type="button" id="rtRuleClear"><span class="btn-label" data-i18n="router.newRule"></span></button>
            </div>
            <p class="router-hint" data-i18n="router.rulesHint"></p>
            <div class="router-form">
              <label><span data-i18n="router.rulePhrase"></span>
                <input id="rtRulePhrase" maxlength="160" autocomplete="off" spellcheck="false"></label>
              <div class="router-formrow">
                <label><span data-i18n="router.ruleMode"></span>
                  <select id="rtRuleMode">
                    <option value="contains" data-i18n="router.ruleModeContains"></option>
                    <option value="words" data-i18n="router.ruleModeWords"></option>
                  </select></label>
                <label><span data-i18n="router.ruleWeight"></span>
                  <input id="rtRuleWeight" type="number" min="0.5" max="10" step="0.5" value="2"></label>
                <label><span data-i18n="router.rulePriority"></span>
                  <input id="rtRulePriority" type="number" min="1" max="9" step="1" value="5"></label>
              </div>
              <label><span data-i18n="router.ruleRole"></span>
                <input id="rtRuleRole" list="rtRoleNames" autocomplete="off" spellcheck="false" maxlength="80">
                <datalist id="rtRoleNames"></datalist></label>
              <label><span data-i18n="router.ruleNote"></span>
                <input id="rtRuleNote" maxlength="200" autocomplete="off"></label>
              <label class="router-check"><input type="checkbox" id="rtRuleEnabled" checked><span data-i18n="router.ruleEnabled"></span></label>
              <div class="router-actions">
                <button class="btn primary" type="button" id="rtRuleSave"><span class="btn-label" data-i18n="router.ruleSave"></span></button>
                <span class="router-status" id="rtRuleStatus" role="status"></span>
              </div>
              <div class="router-confirm" id="rtRuleConfirmRow" hidden>
                <p class="router-hint" id="rtRuleConfirmLabel"></p>
                <div class="router-actions">
                  <label class="router-confirmfield"><span data-i18n="router.ruleConfirm"></span>
                    <input id="rtRuleConfirm" autocomplete="off" spellcheck="false"></label>
                  <button class="btn primary" type="button" id="rtRuleConfirmGo"><span class="btn-label" data-i18n="router.ruleConfirmGo"></span></button>
                  <button class="btn ghost" type="button" id="rtRuleCancel"><span class="btn-label" data-i18n="router.ruleCancel"></span></button>
                </div>
              </div>
            </div>
          </article>
        </div>

        <article class="router-card">
          <div class="router-cardhead">
            <h2 data-i18n="router.rulePreview"></h2>
            <span class="router-chip" id="rtPreviewChip">—</span>
          </div>
          <p class="router-hint" data-i18n="router.rulePreviewHint"></p>
          <div class="router-formrow">
            <label class="router-wide"><span class="sr-only" data-i18n="router.rulePreviewTask"></span>
              <input id="rtPreviewTask" maxlength="4000" autocomplete="off" data-i18n-ph="router.rulePreviewPlaceholder"></label>
            <button class="btn" type="button" id="rtPreviewGo"><span class="btn-label" data-i18n="router.rulePreviewGo"></span></button>
          </div>
          <div class="router-out" id="rtPreviewOut"></div>
        </article>
      </section>

      <section class="router-panel" data-rt-panel="chains" id="rtPanelChains" hidden>
        <article class="router-card">
          <h2 data-i18n="router.chainsTitle"></h2>
          <p class="router-hint" data-i18n="router.chainsHint"></p>
          <div class="router-tiles" id="rtChainTiles"></div>
          <p class="router-hint" data-i18n="router.chainNote"></p>
          <div class="router-list" id="rtChains"></div>
        </article>
      </section>

      <section class="router-panel" data-rt-panel="lab" id="rtPanelLab" hidden>
        <article class="router-card">
          <h2 data-i18n="router.labTitle"></h2>
          <p class="router-hint" data-i18n="router.labHint"></p>
          <label class="router-wide"><span class="sr-only" data-i18n="router.labTitle"></span>
            <textarea id="rtLabCases" rows="6" spellcheck="false" data-i18n-ph="router.labPlaceholder"></textarea></label>
          <div class="router-actions">
            <button class="btn primary" type="button" id="rtLabRun"><span class="btn-label" data-i18n="router.labRun"></span></button>
            <span class="router-status" id="rtLabStatus" role="status"></span>
          </div>
          <p class="router-hint" id="rtLabSummary"></p>
          <div class="router-tablewrap">
            <table class="router-table">
              <thead><tr>
                <th data-i18n="router.labCase"></th>
                <th data-i18n="router.labExpected"></th>
                <th data-i18n="router.labPredicted"></th>
                <th data-i18n="router.labStage"></th>
                <th data-i18n="router.labResult"></th>
              </tr></thead>
              <tbody id="rtLabBody"></tbody>
            </table>
          </div>
          <h3 data-i18n="router.labHistory"></h3>
          <ol class="router-history" id="rtLabHistory"></ol>
          <p class="router-hint" id="rtLabEmpty" data-i18n="router.labEmpty" hidden></p>
        </article>
      </section>

      <section class="router-panel" data-rt-panel="accuracy" id="rtPanelAccuracy" hidden>
        <article class="router-card">
          <h2 data-i18n="router.accuracyTitle"></h2>
          <p class="router-hint" data-i18n="router.accuracyHint"></p>
          <div class="router-tiles" id="rtAccTiles"></div>
          <div class="router-grid">
            <div>
              <h3 data-i18n="router.accRoles"></h3>
              <div class="router-tablewrap">
                <table class="router-table">
                  <thead><tr>
                    <th data-i18n="router.ruleRole"></th>
                    <th data-i18n="router.accRuns"></th>
                    <th data-i18n="router.accOk"></th>
                    <th data-i18n="router.accApproved"></th>
                  </tr></thead>
                  <tbody id="rtAccBody"></tbody>
                </table>
              </div>
              <p class="router-hint" id="rtAccRunsNote"></p>
            </div>
            <div>
              <h3 data-i18n="router.accChecked"></h3>
              <ul class="router-list" id="rtAccChecked"></ul>
              <h3 data-i18n="router.accUnknown"></h3>
              <ul class="router-list" id="rtAccUnknown"></ul>
            </div>
          </div>
          <p class="router-hint" id="rtAccSource"></p>
        </article>
      </section>
    </div>

    <footer class="router-foot">
      <span id="rtStats">—</span>
      <span id="rtSource"></span>
    </footer>
  `;
  (document.querySelector('main.deck') || document.body).after(root);

  /* ── helpers ─────────────────────────────────────────────────────────── */
  const set = (node, text) => { if (node && node.textContent !== text) node.textContent = text; };
  const clock = (stamp) => stamp ? new Date(stamp * 1000).toLocaleTimeString() : t('router.unavailable');
  const flash = (node, text, tone = '') => {
    if (!node) return;
    node.textContent = text;
    node.dataset.tone = tone;
  };
  const message = (error) => String(error?.message || error || t('router.unavailable'));
  const chip = (text, tone) => {
    const node = document.createElement('span');
    node.className = 'router-chip';
    node.dataset.tone = tone || 'muted';
    node.textContent = text;
    return node;
  };
  const percent = (value) => value === null || value === undefined ? t('router.unavailable') : `${Math.round(value * 100)}%`;
  const seconds = (value) => value === null || value === undefined ? t('router.unavailable') : `${value.toFixed(1)} s`;
  const money = (rate) => rate === null || rate === undefined ? t('router.unavailable')
    : `$${rate.per_million.toFixed(2)} / M`;
  const cell = (row, text, className) => {
    const node = document.createElement('td');
    if (className) node.className = className;
    node.textContent = text;
    row.append(node);
    return node;
  };

  function fillSelect(node, values, selected) {
    if (!node) return;
    node.replaceChildren(...values.map((value) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = value;
      if (value === selected) option.selected = true;
      return option;
    }));
  }

  /* ── policy tab ──────────────────────────────────────────────────────── */
  function fact(labelKey, value, tone) {
    const dt = document.createElement('dt');
    dt.textContent = t(labelKey);
    const dd = document.createElement('dd');
    if (tone) dd.dataset.tone = tone;
    dd.textContent = value;
    return [dt, dd];
  }

  function paintFacts() {
    const node = $('rtFacts');
    if (!node || !state.settings) return;
    const s = state.settings;
    const yes = (value) => value === null ? t('router.unavailable') : value ? t('router.on') : t('router.off');
    const rows = [
      fact('router.autoRoute', yes(s.auto_route)),
      fact('router.minScore', String(s.router_min_score)),
      fact('router.llmFallback', yes(s.router_llm_fallback)),
      fact('router.classifierAgent', s.classifier_agent || t('router.unavailable')),
      fact('router.defaultAgent', s.default_agent || t('router.unavailable')),
      fact('router.preferLocal', yes(s.prefer_local)),
      fact('router.localOnly', yes(s.local_only)),
    ];
    node.replaceChildren(...rows.flat());
  }

  function paintWeights() {
    if (!state.policy) return;
    const weights = state.weightsDirty ? readWeights() : state.policy.weights;
    for (const key of ['cost', 'speed', 'quality']) {
      const input = $(`rtWeight${key[0].toUpperCase()}${key.slice(1)}`);
      const out = $(`rtWeight${key[0].toUpperCase()}${key.slice(1)}Out`);
      if (input && !state.weightsDirty) input.value = String(weights[key] ?? 0);
      if (out && input) out.textContent = input.value;
    }
    const total = ['cost', 'speed', 'quality'].reduce((sum, key) => sum + Number($( `rtWeight${key[0].toUpperCase()}${key.slice(1)}`)?.value || 0), 0);
    set($('rtWeightSum'), t('router.weightSum', {sum: total}));
  }

  function readWeights() {
    return {
      cost: Number($('rtWeightCost')?.value || 0),
      speed: Number($('rtWeightSpeed')?.value || 0),
      quality: Number($('rtWeightQuality')?.value || 0),
    };
  }

  function paintRulesSummary() {
    if (!state.policy) return;
    const count = state.policy.rules.length;
    set($('rtRulesChip'), t('router.ruleCount', {count, max: state.policy.limits.rules}));
    $('rtRulesChip').dataset.tone = state.policy.enabled ? 'accent' : 'muted';
    set($('rtRulesSummary'), state.policy.enabled
      ? t('router.rulesStatusOn', {count})
      : t('router.rulesStatusOff', {count}));
    gui.label($('rtEnableRules'), state.policy.enabled ? t('router.disableRules') : t('router.enableRules'));
  }

  function paintModels() {
    if (!state.models) return;
    const body = $('rtModelBody');
    if (!body) return;
    body.replaceChildren();
    for (const row of state.models.agents) {
      const tr = document.createElement('tr');
      if (row.agent === state.models.current) tr.dataset.current = 'true';
      if (state.models.recommendation?.agent === row.agent) tr.dataset.recommended = 'true';
      cell(tr, row.agent, 'router-mono');
      cell(tr, row.model || row.provider, 'router-mono');
      cell(tr, money(row.price));
      cell(tr, seconds(row.avg_duration_s));
      cell(tr, percent(row.reported_ok_rate));
      cell(tr, `${row.coverage}/3`);
      cell(tr, row.score === null ? t('router.unavailable') : String(row.score), 'router-score');
      body.append(tr);
    }
    const current = state.models.current;
    set($('rtCurrentModel'), t('router.currentRouter', {agent: current}));
    const recommendation = state.models.recommendation;
    const bits = [];
    if (recommendation) bits.push(t('router.recommended'), recommendation.agent, `${recommendation.score}%`, `(${recommendation.dimensions.join(', ')})`);
    else bits.push(t('router.noRecommendation'));
    if (!state.models.pricing_configured) bits.push(t('router.pricingMissing'));
    if (!state.models.agents.some((row) => row.runs_observed)) bits.push(t('router.runsMissing'));
    set($('rtModelMeta'), bits.join(' · '));
    $('rtModelMeta').title = state.models.basis;
  }

  function paintPolicy() {
    paintFacts();
    paintWeights();
    paintRulesSummary();
    paintModels();
  }

  /* ── rules tab ───────────────────────────────────────────────────────── */
  function paintRuleList() {
    const list = $('rtRuleList');
    if (!list) return;
    list.replaceChildren();
    const rules = state.policy?.rules || [];
    set($('rtRuleCount'), t('router.ruleCount', {count: rules.length, max: state.policy?.limits.rules ?? 60}));
    $('rtRuleEmpty').hidden = rules.length > 0;
    for (const rule of rules) {
      const row = document.createElement('div');
      row.className = 'router-rule';
      row.setAttribute('role', 'listitem');
      row.dataset.enabled = String(rule.enabled);
      const main = document.createElement('div');
      main.className = 'router-rulemain';
      const phrase = document.createElement('b');
      phrase.textContent = rule.phrase;
      const meta = document.createElement('span');
      meta.textContent = `→ ${rule.role} · ${rule.mode} · ×${rule.weight} · P${rule.priority}`;
      main.append(phrase, meta);

      const actions = document.createElement('div');
      actions.className = 'router-ruleactions';
      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'btn ghost';
      toggle.textContent = rule.enabled ? t('router.disable') : t('router.enable');
      toggle.onclick = () => toggleRule(rule, !rule.enabled);
      const edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'btn ghost';
      edit.textContent = t('router.edit');
      edit.onclick = () => editRule(rule);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'btn danger';
      remove.textContent = t('router.remove');
      remove.onclick = () => removeRule(rule);
      actions.append(toggle, edit, remove);
      row.append(main, actions);
      list.append(row);
    }
    const datalist = $('rtRoleNames');
    if (datalist && datalist.children.length !== state.roleNames.length) {
      datalist.replaceChildren(...state.roleNames.map((name) => {
        const option = document.createElement('option');
        option.value = name;
        return option;
      }));
    }
  }

  function readRuleForm() {
    return {
      ...(state.editing ? {id: state.editing.id} : {}),
      phrase: $('rtRulePhrase').value.trim(),
      role: $('rtRuleRole').value.trim(),
      mode: $('rtRuleMode').value,
      weight: Number($('rtRuleWeight').value || 2),
      priority: Number($('rtRulePriority').value || 5),
      note: $('rtRuleNote').value.trim(),
      enabled: $('rtRuleEnabled').checked,
    };
  }

  function editRule(rule) {
    state.editing = rule;
    $('rtRuleTitle').textContent = t('router.ruleTitleEdit', {id: rule.id});
    $('rtRulePhrase').value = rule.phrase;
    $('rtRuleRole').value = rule.role;
    $('rtRuleMode').value = rule.mode;
    $('rtRuleWeight').value = String(rule.weight);
    $('rtRulePriority').value = String(rule.priority);
    $('rtRuleNote').value = rule.note || '';
    $('rtRuleEnabled').checked = rule.enabled;
    $('rtRuleConfirmRow').hidden = true;
    $('rtRulePhrase').focus();
  }

  function clearRuleForm() {
    state.editing = null;
    state.pending = null;
    $('rtRuleTitle').textContent = t('router.ruleTitleNew');
    $('rtRulePhrase').value = '';
    $('rtRuleRole').value = '';
    $('rtRuleMode').value = 'contains';
    $('rtRuleWeight').value = '2';
    $('rtRulePriority').value = '5';
    $('rtRuleNote').value = '';
    $('rtRuleEnabled').checked = true;
    $('rtRuleConfirmRow').hidden = true;
    flash($('rtRuleStatus'), '');
    flash($('rtPreviewChip'), '—');
    $('rtPreviewOut').replaceChildren();
  }

  async function prepareRule() {
    const rule = readRuleForm();
    if (!rule.phrase) return flash($('rtRuleStatus'), t('router.ruleNeedPhrase'), 'warn');
    if (!rule.role) return flash($('rtRuleStatus'), t('router.ruleNeedRole'), 'warn');
    try {
      const body = (await gui.api('/api/router/rule', {action: 'prepare', rule})).data;
      state.pending = body;
      $('rtRuleConfirmRow').hidden = false;
      set($('rtRuleConfirmLabel'), t('router.rulePrepared', {token: body.confirmation}));
      $('rtRuleConfirm').value = '';
      $('rtRuleConfirm').placeholder = body.confirmation;
      $('rtRuleConfirm').focus();
      flash($('rtRuleStatus'), t('router.rulePreparedShort'), '');
      return body;
    } catch (error) {
      flash($('rtRuleStatus'), message(error), 'error');
      return null;
    }
  }

  async function writeRule() {
    if (!state.pending) return;
    const token = $('rtRuleConfirm').value.trim();
    try {
      const body = (await gui.api('/api/router/rule', {
        action: 'save', rule: state.pending.rule, revision: state.pending.revision, confirmation: token,
      })).data;
      $('rtRuleConfirmRow').hidden = true;
      state.pending = null;
      state.policy = body.policy;
      await refreshPolicy();
      await loadRules();
      paintRuleList();
      if (body.saved === 'created') clearRuleForm();
      flash($('rtRuleStatus'), t('router.ruleSaved', {id: body.rule.id, action: body.saved}), 'ok');
    } catch (error) {
      flash($('rtRuleStatus'), message(error), 'error');
    }
  }

  async function toggleRule(rule, enabled) {
    const token = `${enabled ? 'ENABLE' : 'DISABLE'} RULE ${rule.id}`;
    const typed = window.prompt(`${t('router.confirmPrompt')} ${token}`, '');
    if (typed === null) return;
    if (typed.trim() !== token) return flash($('rtRuleStatus'), t('router.cancelled'), 'warn');
    try {
      const body = (await gui.api('/api/router/rule', {
        action: enabled ? 'enable' : 'disable', id: rule.id, revision: state.policy.revision, confirmation: token,
      })).data;
      state.policy = body.policy;
      await refreshPolicy();
      await loadRules();
      paintRuleList();
      flash($('rtRuleStatus'), t('router.ruleToggleDone', {id: rule.id, state: enabled ? t('router.enabled') : t('router.disabled')}), 'ok');
    } catch (error) {
      flash($('rtRuleStatus'), message(error), 'error');
    }
  }

  async function removeRule(rule) {
    const token = `REMOVE RULE ${rule.id}`;
    const typed = window.prompt(`${t('router.confirmPrompt')} ${token}`, '');
    if (typed === null) return;
    if (typed.trim() !== token) return flash($('rtRuleStatus'), t('router.cancelled'), 'warn');
    try {
      const body = (await gui.api('/api/router/rule', {
        action: 'remove', id: rule.id, revision: state.policy.revision, confirmation: token,
      })).data;
      state.policy = body.policy;
      if (state.editing?.id === rule.id) clearRuleForm();
      await refreshPolicy();
      await loadRules();
      paintRuleList();
      flash($('rtRuleStatus'), t('router.ruleRemoved', {id: rule.id}), 'ok');
    } catch (error) {
      flash($('rtRuleStatus'), message(error), 'error');
    }
  }

  async function previewDraft() {
    const task = $('rtPreviewTask').value.trim();
    if (!task) return flash($('rtPreviewChip'), t('router.previewEmpty'), 'warn');
    try {
      const draft = readRuleForm();
      delete draft.id;
      const body = (await gui.api('/api/router/simulate',
        draft.phrase && draft.role ? {task, draft} : {task})).data;
      paintPreview($('rtPreviewOut'), body);
      const hit = body.rules_matched.length > 0;
      $('rtPreviewChip').textContent = hit ? t('router.ruleMatch') : t('router.ruleNoMatch');
      $('rtPreviewChip').dataset.tone = hit ? 'accent' : 'muted';
    } catch (error) {
      flash($('rtPreviewChip'), message(error), 'error');
    }
  }

  function paintPreview(out, data) {
    out.replaceChildren();
    const head = document.createElement('p');
    head.className = 'router-previewhead';
    if (data.decision) {
      head.textContent = t('router.previewPick', {role: data.decision.role, method: data.decision.method, score: data.decision.score});
    } else {
      head.textContent = t('router.previewDeferred', {threshold: data.threshold, stage: data.deferred?.stage || 'classifier'});
    }
    out.append(head);
    if (data.rules_matched.length) {
      const rules = document.createElement('p');
      rules.className = 'router-hint';
      rules.textContent = t('router.previewRules', {rules: data.rules_matched.map((rule) => `${rule.id} → ${rule.role}`).join(', ')});
      out.append(rules);
    }
    if (data.deferred?.reason) {
      const reason = document.createElement('p');
      reason.className = 'router-hint';
      reason.textContent = data.deferred.reason;
      out.append(reason);
    }
    out.append(chip(`${data.provider_calls} provider calls · ${data.elapsed_ms} ms`, 'muted'));
  }

  /* ── chains tab ──────────────────────────────────────────────────────── */
  function paintChains() {
    if (!state.chains) return;
    const summary = state.chains.summary;
    const tiles = $('rtChainTiles');
    tiles.replaceChildren();
    for (const [labelKey, value, tone] of [
      ['router.chainAgents', summary.agents, ''],
      ['router.chainWithFallback', summary.with_fallback, ''],
      ['router.chainBroken', summary.broken, summary.broken ? 'warn' : 'ok'],
      ['router.chainLoops', summary.loops, ''],
      ['router.chainMissing', summary.missing_keys, summary.missing_keys ? 'warn' : 'ok'],
    ]) {
      const tile = document.createElement('div');
      tile.className = 'router-tile';
      tile.dataset.tone = tone;
      const b = document.createElement('b');
      b.textContent = String(value);
      const span = document.createElement('span');
      span.textContent = t(labelKey);
      tile.append(b, span);
      tiles.append(tile);
    }
    const list = $('rtChains');
    list.replaceChildren();
    for (const chain of state.chains.agents) {
      const row = document.createElement('div');
      row.className = 'router-chain';
      const head = document.createElement('div');
      head.className = 'router-chainhead';
      const name = document.createElement('b');
      name.textContent = chain.agent;
      head.append(name);
      for (const issue of chain.issues) {
        if (issue.code === 'loop') continue;
        const tag = chip(t(issue.code === 'missing_key' ? 'router.chainIssueKey' : 'router.chainIssueUnknown'), issue.code === 'missing_key' ? 'muted' : 'warn');
        tag.title = issue.message;
        head.append(tag);
      }
      const ladder = document.createElement('div');
      ladder.className = 'router-ladder';
      chain.nodes.forEach((node, index) => {
        if (index) {
          const arrow = document.createElement('span');
          arrow.className = 'router-arrow';
          arrow.textContent = '→';
          ladder.append(arrow);
        }
        const item = document.createElement('span');
        item.className = 'router-node';
        item.dataset.known = String(node.known);
        if (node.is_router) item.dataset.role = 'router';
        if (node.is_default) item.dataset.role = 'default';
        item.textContent = node.known ? node.name : `${node.name} ?`;
        if (node.known) item.title = `${node.provider} · ${node.model}${node.local ? ' · local' : ''}`;
        ladder.append(item);
      });
      if (chain.issues.some((issue) => issue.code === 'loop')) {
        const loop = document.createElement('span');
        loop.className = 'router-chip';
        loop.dataset.tone = 'muted';
        loop.textContent = t('router.chainLoop');
        ladder.append(loop);
      }
      row.append(head, ladder);
      list.append(row);
    }
  }

  /* ── lab tab ─────────────────────────────────────────────────────────── */
  function parseCases(text) {
    return text.split('\n').map((line) => line.trim()).filter(Boolean).map((line) => {
      const [task, expected] = line.split('=>').map((part) => (part || '').trim());
      return expected ? {task, expected} : {task};
    });
  }

  async function runLab() {
    const cases = parseCases($('rtLabCases').value);
    if (!cases.length) return flash($('rtLabStatus'), t('router.labNeedCases'), 'warn');
    $('rtLabRun').disabled = true;
    flash($('rtLabStatus'), t('router.labRunning'), '');
    try {
      const body = (await gui.api('/api/router/lab', {cases})).data;
      state.lab = {runs: [body.run]};
      paintLabRun(body.run);
      await loadLab();
      paintLabHistory();
      flash($('rtLabStatus'), t('router.labSaved', {id: body.run.id, time: clock(body.run.at)}), 'ok');
    } catch (error) {
      flash($('rtLabStatus'), message(error), 'error');
    } finally {
      $('rtLabRun').disabled = false;
    }
  }

  function paintLabRun(run) {
    const body = $('rtLabBody');
    body.replaceChildren();
    for (const item of run.cases) {
      const tr = document.createElement('tr');
      if (item.pass === true) tr.dataset.result = 'pass';
      if (item.pass === false) tr.dataset.result = 'fail';
      cell(tr, item.task, 'router-task');
      cell(tr, item.expected || t('router.labUnlabelled'));
      cell(tr, item.predicted || (item.top ? `${item.top} (${t('router.belowThreshold')})` : t('router.unavailable')));
      cell(tr, item.stage);
      cell(tr, item.pass === null ? t('router.labUnlabelled') : item.pass ? t('router.labPass') : t('router.labFail'),
        item.pass === null ? '' : item.pass ? 'router-pass' : 'router-fail');
      body.append(tr);
    }
    const bits = [];
    if (run.accuracy === null) bits.push(t('router.labNoExpected'));
    else bits.push(t('router.labAccuracy', {accuracy: percent(run.accuracy), correct: run.correct, expected: run.with_expected}));
    if (run.unresolved) bits.push(t('router.labUnresolved', {count: run.unresolved}));
    bits.push(`${run.provider_calls} provider calls · ${run.elapsed_ms} ms`);
    set($('rtLabSummary'), bits.join(' · '));
  }

  function paintLabHistory() {
    const list = $('rtLabHistory');
    if (!list) return;
    const runs = state.lab?.runs || [];
    list.replaceChildren();
    $('rtLabEmpty').hidden = runs.length > 0;
    for (const run of runs) {
      const row = document.createElement('li');
      const b = document.createElement('b');
      b.textContent = clock(run.at);
      const span = document.createElement('span');
      span.textContent = `${run.total} case(s) · ${run.accuracy === null ? t('router.labNoExpected') : percent(run.accuracy)} · ${run.id}`;
      row.append(b, span);
      list.append(row);
    }
  }

  /* ── accuracy tab ────────────────────────────────────────────────────── */
  function paintAccuracy() {
    if (!state.accuracy) return;
    const data = state.accuracy;
    const tiles = $('rtAccTiles');
    tiles.replaceChildren();
    const feedback = data.feedback;
    for (const [label, value] of [
      [t('router.accObservations'), String(feedback.observations)],
      [t('router.accApproved'), String(feedback.approved)],
      [t('router.accRate'), feedback.approval_rate === null ? t('router.unavailable') : percent(feedback.approval_rate)],
      [t('router.accRuns'), String(data.totals.runs)],
      [t('router.accOk'), data.totals.runs ? percent(data.totals.reported_ok / data.totals.runs) : t('router.unavailable')],
    ]) {
      const tile = document.createElement('div');
      tile.className = 'router-tile';
      const b = document.createElement('b');
      b.textContent = value;
      const span = document.createElement('span');
      span.textContent = label;
      tile.append(b, span);
      tiles.append(tile);
    }
    const approvedByRole = new Map(feedback.roles.map((row) => [row.role, row]));
    const body = $('rtAccBody');
    body.replaceChildren();
    for (const row of data.roles) {
      const tr = document.createElement('tr');
      cell(tr, row.role, 'router-mono');
      cell(tr, String(row.runs));
      cell(tr, percent(row.reported_ok_rate));
      const explicit = approvedByRole.get(row.role);
      cell(tr, explicit ? `${explicit.approved}/${explicit.observations}` : t('router.unavailable'));
      body.append(tr);
    }
    set($('rtAccRunsNote'), data.roles.length ? t('router.accWindow', {days: data.window_days}) : t('router.accNoRuns'));
    const label = (item) => {
      const key = `router.check.${item.code}`;
      return i18n.keys().includes(key) ? t(key) : item.text;
    };
    const checked = $('rtAccChecked');
    checked.replaceChildren(...data.checked.map((item) => {
      const li = document.createElement('li');
      li.textContent = label(item);
      return li;
    }));
    const unknown = $('rtAccUnknown');
    unknown.replaceChildren(...data.not_checked.map((item) => {
      const li = document.createElement('li');
      li.textContent = label(item);
      return li;
    }));
    set($('rtAccSource'), t('router.accSource', {source: data.source}));
  }

  /* ── loading ─────────────────────────────────────────────────────────── */
  async function refreshPolicy() {
    const body = (await gui.api('/api/router/overview')).data;
    state.overview = body;
    state.policy = body.policy;
    state.settings = body.settings;
    state.roster = body.roster;
    state.chainSummary = body.chains;
    state.observed_at = body.observed_at;
    state.weightsDirty = false;
    paintPolicy();
  }

  async function loadRules() {
    const body = (await gui.api('/api/router/rules')).data;
    state.policy = {
      ...(state.policy || {}), enabled: body.enabled, rules: body.rules, revision: body.revision,
      limits: state.policy?.limits || {rules: body.limits.rules},
    };
    state.roleNames = body.role_names;
    state.observed_at = body.observed_at;
  }

  async function loadModels() {
    state.models = (await gui.api('/api/router/models')).data;
    paintModels();
  }

  async function loadChains() {
    state.chains = (await gui.api('/api/router/chains')).data;
    paintChains();
  }

  async function loadLab() {
    state.lab = (await gui.api('/api/router/lab')).data;
    paintLabHistory();
    if (state.lab.last) paintLabRun(state.lab.last);
  }

  async function loadAccuracy() {
    state.accuracy = (await gui.api('/api/router/accuracy')).data;
    paintAccuracy();
  }

  async function load() {
    root.dataset.busy = 'true';
    state.busy = true;
    try {
      await refreshPolicy();
      await loadRules();
      paintRuleList();
      await loadModels();
      set($('rtUpdated'), t('router.updated', {time: clock(state.observed_at)}));
      set($('rtStats'), t('router.footer', {
        roles: state.roster.roles, agents: state.roster.agents, rules: state.policy.rules.length,
      }));
      set($('rtSource'), state.policy.enabled ? t('router.footerOn') : t('router.footerOff'));
      flash($('rtWeightsStatus'), '');
    } catch (error) {
      state.error = message(error);
      set($('rtUpdated'), state.error);
      flash($('rtWeightsStatus'), state.error, 'error');
    } finally {
      state.busy = false;
      root.dataset.busy = 'false';
    }
  }

  async function switchTab(tab) {
    state.tab = tab;
    root.querySelectorAll('[data-rt-tab]').forEach((node) => node.setAttribute('aria-selected', String(node.dataset.rtTab === tab)));
    root.querySelectorAll('[data-rt-panel]').forEach((panel) => { panel.hidden = panel.dataset.rtPanel !== tab; });
    if (tab === 'chains' && !state.chains?.agents) loadChains().catch((error) => flash($('rtChainTiles'), message(error), 'error'));
    if (tab === 'lab') loadLab().catch((error) => flash($('rtLabStatus'), message(error), 'error'));
    if (tab === 'accuracy') loadAccuracy().catch((error) => set($('rtAccSource'), message(error)));
  }

  /* ── wiring: policy ──────────────────────────────────────────────────── */
  $('rtRefresh').onclick = () => load();
  $('rtRoles').onclick = () => gui.show('roles');
  $('rtOpenModels').onclick = () => {
    if (typeof window.nexusWorkbenchShow === 'function') window.nexusWorkbenchShow('models');
    else $('guiSettings')?.click();
  };
  $('rtOpenRules').onclick = () => switchTab('rules');
  for (const key of ['Cost', 'Speed', 'Quality']) {
    $(`rtWeight${key}`).addEventListener('input', () => {
      state.weightsDirty = true;
      $(`rtWeight${key}Out`).textContent = $(`rtWeight${key}`).value;
      const total = ['Cost', 'Speed', 'Quality'].reduce((sum, suffix) => sum + Number($(`rtWeight${suffix}`).value || 0), 0);
      set($('rtWeightSum'), t('router.weightSum', {sum: total}));
    });
  }
  $('rtSaveWeights').onclick = async () => {
    const typed = window.prompt(t('router.weightsConfirm'), '');
    if (typed === null) return;
    try {
      const body = (await gui.api('/api/router/weights', {
        weights: readWeights(), revision: state.policy.revision, confirmation: typed.trim(),
      })).data;
      state.policy = body.policy;
      state.models = body.ranking;
      state.weightsDirty = false;
      paintPolicy();
      flash($('rtWeightsStatus'), t('router.weightsSaved', {revision: body.revision}), 'ok');
    } catch (error) {
      flash($('rtWeightsStatus'), message(error), 'error');
    }
  };
  $('rtEnableRules').onclick = async () => {
    const enable = !state.policy?.enabled;
    const token = enable ? 'ENABLE RULES' : 'DISABLE RULES';
    const typed = window.prompt(t('router.enableConfirm', {token}), '');
    if (typed === null) return;
    try {
      const body = (await gui.api('/api/router/enable', {
        enabled: enable, revision: state.policy.revision, confirmation: typed.trim(),
      })).data;
      state.policy = body.policy;
      paintPolicy();
      paintRuleList();
      flash($('rtRulesStatus'), enable ? t('router.enableDone') : t('router.disableDone'), 'ok');
    } catch (error) {
      flash($('rtRulesStatus'), message(error), 'error');
    }
  };

  /* ── wiring: rules ───────────────────────────────────────────────────── */
  $('rtRuleClear').onclick = clearRuleForm;
  $('rtRuleSave').onclick = prepareRule;
  $('rtRuleConfirmGo').onclick = writeRule;
  $('rtRuleCancel').onclick = () => { state.pending = null; $('rtRuleConfirmRow').hidden = true; flash($('rtRuleStatus'), ''); };
  $('rtPreviewGo').onclick = previewDraft;

  /* ── wiring: lab + tabs ──────────────────────────────────────────────── */
  $('rtLabRun').onclick = runLab;
  root.querySelectorAll('[data-rt-tab]').forEach((node) => { node.onclick = () => switchTab(node.dataset.rtTab); });
  root.querySelector('[role="tablist"]').addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...root.querySelectorAll('[data-rt-tab]')];
    const index = tabs.findIndex((tab) => tab.dataset.rtTab === state.tab);
    const next = event.key === 'Home' ? tabs[0]
      : event.key === 'End' ? tabs[tabs.length - 1]
        : tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
    next.focus();
    switchTab(next.dataset.rtTab);
  });

  /* ── composer: an offline route preview next to the role override ─────── */
  const previewBtn = $('routePreviewBtn');
  const previewOut = $('routePreview');
  async function previewComposer() {
    if (!previewBtn || !previewOut) return;
    const forced = $('roleSelect')?.value || '';
    if (forced) {
      previewOut.textContent = t('router.previewForced', {role: forced});
      previewOut.dataset.tone = 'accent';
      return;
    }
    const task = ($('taskInput')?.value || '').trim();
    if (!task) {
      previewOut.textContent = t('router.previewEmpty');
      previewOut.dataset.tone = '';
      return;
    }
    previewBtn.disabled = true;
    try {
      const body = (await gui.api('/api/router/simulate', {task})).data;
      if (body.decision) {
        previewOut.textContent = t('router.previewPick', {role: body.decision.role, method: body.decision.method, score: body.decision.score});
        previewOut.dataset.tone = 'accent';
      } else if (body.deferred) {
        previewOut.textContent = t('router.previewDeferred', {threshold: body.threshold, stage: body.deferred.stage});
        previewOut.dataset.tone = 'warn';
      }
      if (body.rules_matched.length) previewOut.textContent += ` · ${t('router.previewRules', {rules: body.rules_matched.map((rule) => rule.id).join(', ')})}`;
    } catch (error) {
      previewOut.textContent = message(error);
      previewOut.dataset.tone = 'error';
    } finally {
      previewBtn.disabled = false;
    }
  }
  if (previewBtn) previewBtn.addEventListener('click', previewComposer);
  if (previewOut) {
    previewOut.addEventListener('click', () => {
      const task = ($('taskInput')?.value || '').trim();
      gui.show('router');
      switchTab('lab');
      if (task) $('rtLabCases').value = task;
    });
  }

  /* ── lifecycle ───────────────────────────────────────────────────────── */
  let loaded = false;
  async function boot() {
    if (loaded) return;
    loaded = true;
    await load();
  }
  window.addEventListener('nexus:view', (event) => {
    if (event.detail === 'router') boot().catch((error) => set($('rtUpdated'), message(error)));
  });
  i18n.onChange(() => {
    i18n.apply(root);
    clearRuleFormLabels();
    paintPolicy();
    paintRuleList();
    if (state.chains) paintChains();
    if (state.accuracy) paintAccuracy();
    if (state.lab?.last) paintLabRun(state.lab.last);
    paintLabHistory();
    if (state.observed_at) set($('rtUpdated'), t('router.updated', {time: clock(state.observed_at)}));
    set($('rtSource'), state.policy?.enabled ? t('router.footerOn') : t('router.footerOff'));
  });
  function clearRuleFormLabels() {
    $('rtRuleTitle').textContent = state.editing ? t('router.ruleTitleEdit', {id: state.editing.id}) : t('router.ruleTitleNew');
  }
  if (gui.view === 'router') boot().catch(() => {});

  window.nexusRouter = {
    load, root,
    state: () => ({...state, roleNames: state.roleNames.length, rules: state.policy?.rules?.length || 0}),
    tab: switchTab,
    simulate: (task, draft) => gui.api('/api/router/simulate', draft ? {task, draft} : {task}),
    preview: previewComposer,
  };
})();
