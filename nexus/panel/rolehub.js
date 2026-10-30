/* Role Hub: browse 1000 roles, read one prompt, review an edit, undo a version.

   Data comes from four read surfaces (`/api/roles/index`, `/detail`, `/history`,
   `/diff`) and three reviewed actions (`/api/roles/route`, `/export`,
   `/restore`). Editing is not a new write path: it posts to the existing
   `/api/workbench/configuration` with its revision check and typed
   `SAVE ROLE <name>` confirmation, so the same server-side validation and
   atomic rollback apply. Nothing here runs a task or calls a model. */
(() => {
  'use strict';

  const gui = window.nexusGui;
  const i18n = window.nexusI18n;
  if (!gui || !i18n) return;
  const t = i18n.t;
  const $ = (id) => document.getElementById(id);
  const ROW = 46;
  const OVERSCAN = 6;

  const state = {
    rows: [], total: 0, matched: 0, page: 1, pages: 1, size: 50,
    q: '', category: '', agent: '', sort: 'name', dir: 'asc',
    stats: null, filters: {categories: [], agents: [], sources: []},
    active: 0, name: null, detail: null, tab: 'role',
    history: [], diff: null, quality: null, route: null, agents: {},
    observed_at: 0, busy: false, error: '', keep: null,
  };

  const root = document.createElement('section');
  root.id = 'view-roles';
  root.className = 'gui-modules roles-page';
  root.dataset.guiView = 'roles';
  root.hidden = gui.view !== 'roles';
  root.dataset.busy = 'false';
  root.innerHTML = `
    <header class="roles-head">
      <div>
        <p class="roles-eyebrow" data-i18n="roles.eyebrow">Role hub</p>
        <h1 data-i18n="roles.title">The whole roster, one page</h1>
        <p class="roles-sub" data-i18n="roles.subtitle"></p>
      </div>
      <div class="roles-quick" role="group" aria-label="Role actions" data-i18n-aria="roles.actionsAria">
        <button class="btn primary" type="button" id="rhNew"><span class="btn-label" data-i18n="roles.new"></span></button>
        <button class="btn" type="button" id="rhExport"><span class="btn-label" data-i18n="roles.export"></span></button>
        <button class="btn ghost" type="button" id="rhWorkbench"><span class="btn-label" data-i18n="roles.workbench"></span></button>
      </div>
    </header>

    <div class="roles-toolbar">
      <label class="roles-search">
        <span class="sr-only" data-i18n="roles.searchLabel">Search roles</span>
        <input id="rhSearch" type="search" autocomplete="off" spellcheck="false" data-i18n-ph="roles.search">
      </label>
      <label class="roles-pick"><span data-i18n="roles.category">Category</span><select id="rhCategory"></select></label>
      <label class="roles-pick"><span data-i18n="roles.agent">Agent</span><select id="rhAgent"></select></label>
      <label class="roles-pick"><span data-i18n="roles.sort">Sort</span>
        <select id="rhSort">
          <option value="name" data-i18n="roles.sortName">Name</option>
          <option value="persona" data-i18n="roles.sortPrompt">Prompt size</option>
          <option value="keywords" data-i18n="roles.sortKeywords">Keywords</option>
          <option value="category" data-i18n="roles.sortCategory">Category</option>
          <option value="updated" data-i18n="roles.sortUpdated">Edited</option>
        </select>
      </label>
      <div class="roles-tabs" role="tablist" aria-label="Role panels" data-i18n-aria="roles.panelsAria">
        <button role="tab" type="button" id="rhTabRole" data-tab="role" aria-selected="true" data-i18n="roles.tabRole">Role</button>
        <button role="tab" type="button" id="rhTabHistory" data-tab="history" aria-selected="false" data-i18n="roles.tabHistory">History</button>
        <button role="tab" type="button" id="rhTabQuality" data-tab="quality" aria-selected="false" data-i18n="roles.tabQuality">Quality</button>
        <button role="tab" type="button" id="rhTabRoute" data-tab="route" aria-selected="false" data-i18n="roles.tabRoute">Route preview</button>
      </div>
    </div>

    <div class="roles-body">
      <div class="roles-listwrap">
        <div class="roles-list" id="rhList" role="listbox" tabindex="0"
             aria-label="Roles" data-i18n-aria="roles.listAria"></div>
        <p class="roles-listfoot"><span id="rhMeta">—</span></p>
      </div>

      <aside class="roles-side" id="rhSide" aria-live="polite">
        <section class="roles-panel" data-panel="role" id="rhPanelRole">
          <div id="rhEmpty" class="roles-placeholder" data-i18n="roles.emptyHint"></div>
          <form id="rhForm" class="roles-form" hidden>
            <div class="roles-formhead">
              <div>
                <h2 id="rhTitle">—</h2>
                <p class="roles-id" id="rhName"></p>
              </div>
              <div class="roles-badges" id="rhBadges"></div>
            </div>
            <div class="roles-grid">
              <label><span data-i18n="roles.fieldTitle">Title</span><input id="rhFTitle" maxlength="300"></label>
              <label><span data-i18n="roles.fieldCategory">Category</span><input id="rhFCategory" maxlength="80"></label>
              <label><span data-i18n="roles.fieldAgent">Agent</span><select id="rhFAgent"></select></label>
              <label><span data-i18n="roles.fieldModel">Model override</span><input id="rhFModel" maxlength="200" data-i18n-ph="roles.optional"></label>
              <label><span data-i18n="roles.fieldTemperature">Temperature</span><input id="rhFTemperature" type="number" step="0.1" min="0" max="2"></label>
              <label><span data-i18n="roles.fieldTools">Tools (comma, empty = inherit)</span><input id="rhFTools" maxlength="1200"></label>
            </div>
            <label class="roles-wide"><span data-i18n="roles.fieldKeywords">Keywords (word:weight, comma separated)</span>
              <textarea id="rhFKeywords" rows="2" spellcheck="false"></textarea></label>
            <label class="roles-wide"><span data-i18n="roles.fieldAliases">Aliases (comma separated)</span>
              <input id="rhFAliases" maxlength="1200" spellcheck="false"></label>
            <label class="roles-wide"><span data-i18n="roles.fieldPersona">Persona / system prompt</span>
              <textarea id="rhFPersona" rows="9" spellcheck="false"></textarea></label>
            <label class="roles-wide"><span data-i18n="roles.fieldInstructions">Extra instructions</span>
              <textarea id="rhFInstructions" rows="4" spellcheck="false"></textarea></label>
            <p class="roles-hint" id="rhHint"></p>
            <div class="roles-formactions">
              <button class="btn primary" type="button" id="rhSave"><span class="btn-label" data-i18n="roles.save"></span></button>
              <button class="btn danger" type="button" id="rhRemove"><span class="btn-label" data-i18n="roles.remove"></span></button>
              <button class="btn ghost" type="button" id="rhExportOne"><span class="btn-label" data-i18n="roles.exportOne"></span></button>
              <span class="roles-status" id="rhStatus" role="status"></span>
            </div>
          </form>
        </section>

        <section class="roles-panel" data-panel="history" id="rhPanelHistory" hidden>
          <h2 data-i18n="roles.historyTitle">Recorded versions</h2>
          <p class="roles-hint" data-i18n="roles.historyHint"></p>
          <ol class="roles-versions" id="rhVersions"></ol>
          <pre class="roles-diff" id="rhDiff" hidden></pre>
          <div class="roles-formactions" id="rhRestoreRow" hidden>
            <button class="btn danger" type="button" id="rhRestore"><span class="btn-label" data-i18n="roles.restore"></span></button>
            <span class="roles-status" id="rhRestoreStatus" role="status"></span>
          </div>
        </section>

        <section class="roles-panel" data-panel="quality" id="rhPanelQuality" hidden>
          <h2 data-i18n="roles.qualityTitle">Roster checks</h2>
          <p class="roles-hint" id="rhQualityMeta"></p>
          <ul class="roles-issues" id="rhIssues"></ul>
          <p class="roles-hint" id="rhQualityScope"></p>
        </section>

        <section class="roles-panel" data-panel="route" id="rhPanelRoute" hidden>
          <h2 data-i18n="roles.routeTitle">Which role would take this task?</h2>
          <p class="roles-hint" data-i18n="roles.routeHint"></p>
          <label class="roles-wide"><span class="sr-only" data-i18n="roles.routeLabel">Task text</span>
            <textarea id="rhRouteTask" rows="3" spellcheck="false" data-i18n-ph="roles.routePlaceholder"></textarea></label>
          <div class="roles-formactions">
            <button class="btn" type="button" id="rhRouteGo"><span class="btn-label" data-i18n="roles.routeGo"></span></button>
            <span class="roles-status" id="rhRouteStatus" role="status"></span>
          </div>
          <div id="rhRouteOut" class="roles-routeout"></div>
        </section>
      </aside>
    </div>

    <footer class="roles-foot">
      <span id="rhStats">—</span>
      <span id="rhUpdated"></span>
    </footer>
  `;
  (document.querySelector('main.deck') || document.body).after(root);

  /* ── rendering helpers ───────────────────────────────────────────────── */
  const set = (node, text) => { if (node && node.textContent !== text) node.textContent = text; };
  const clock = (stamp) => stamp ? new Date(stamp * 1000).toLocaleTimeString() : '—';
  const ago = (stamp) => {
    if (!stamp) return '';
    const seconds = Math.max(0, Math.round((Date.now() / 1000) - stamp));
    if (seconds < 90) return `${seconds}s`;
    if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
    return `${Math.round(seconds / 3600)}h`;
  };
  const flash = (node, text, tone = '') => {
    if (!node) return;
    node.textContent = text;
    node.dataset.tone = tone;
  };

  function stateChip(label, tone) {
    const span = document.createElement('span');
    span.className = 'roles-chip';
    span.dataset.tone = tone || 'muted';
    span.textContent = label;
    return span;
  }

  /* ── the virtualised list ────────────────────────────────────────────── */
  const viewport = {scroll: null};

  function rowLabel(row) {
    return `${row.name} · ${row.title || row.category} · ${row.agent || t('roles.defaultAgent')}`;
  }

  function paintRows() {
    const list = $('rhList');
    if (!list) return;
    const height = list.clientHeight || 420;
    const first = Math.max(0, Math.floor(list.scrollTop / ROW) - OVERSCAN);
    const count = Math.ceil(height / ROW) + OVERSCAN * 2;
    const slice = state.rows.slice(first, first + count);
    list.replaceChildren();
    list.style.setProperty('--rh-total', String(state.rows.length * ROW));
    const spacer = document.createElement('div');
    spacer.className = 'roles-spacer';
    spacer.style.height = `${state.rows.length * ROW}px`;
    const window_ = document.createElement('div');
    window_.className = 'roles-window';
    window_.style.transform = `translateY(${first * ROW}px)`;
    slice.forEach((row, offset) => {
      const index = first + offset;
      const item = document.createElement('div');
      item.className = 'roles-row';
      item.id = 'rhRow-' + row.name;
      item.dataset.index = String(index);
      item.dataset.name = row.name;
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', String(state.name === row.name));
      item.tabIndex = -1;
      const title = document.createElement('b');
      title.textContent = row.name;
      const meta = document.createElement('span');
      meta.textContent = `${row.title || row.category} · ${row.agent || t('roles.defaultAgent')}`;
      const tail = document.createElement('span');
      tail.className = 'roles-rowtail';
      tail.textContent = row.override ? t('roles.edited') : `${row.keywords} kw`;
      item.dataset.override = String(row.override);
      item.append(title, meta, tail);
      item.onclick = () => select(index, true);
      window_.append(item);
    });
    list.append(spacer, window_);
    const active = list.querySelector(`#rhRow-${CSS.escape(state.name || '')}`);
    if (active) list.setAttribute('aria-activedescendant', active.id);
    else list.removeAttribute('aria-activedescendant');
  }

  function select(index, open) {
    const row = state.rows[index];
    if (!row) return;
    state.active = index;
    state.name = row.name;
    paintRows();
    const node = document.getElementById('rhRow-' + row.name);
    node?.scrollIntoView({block: 'nearest'});
    if (open) openRole(row.name);
  }

  function fillSelect(select, items, value, emptyLabel) {
    const previous = value ?? select.value;
    select.replaceChildren();
    if (emptyLabel !== undefined) select.add(new Option(emptyLabel, ''));
    items.forEach((item) => select.add(new Option(item, item)));
    if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  }

  /* ── the index ───────────────────────────────────────────────────────── */
  let searchTimer = null;

  async function load(patch = {}, {saveScroll = false} = {}) {
    Object.assign(state, patch);
    if (state.busy) return;
    state.busy = true;
    try {
      const query = new URLSearchParams({
        q: state.q, category: state.category, agent: state.agent,
        sort: state.sort, dir: state.dir, page: String(state.page), size: String(state.size),
      });
      const body = await gui.api('/api/roles/index?' + query.toString());
      const data = body.data;
      state.rows = data.roles;
      state.total = data.total;
      state.matched = data.matched;
      state.page = data.page;
      state.pages = data.pages;
      state.stats = data.stats;
      state.filters = data.filters;
      state.observed_at = data.observed_at;
      state.error = '';
      const visible = state.rows.some((row) => row.name === state.name);
      if (visible) {
        state.active = state.rows.findIndex((row) => row.name === state.name);
      } else if (state.keep && state.keep === state.name) {
        /* A just-saved role often sits on another page (names are alphabetical):
           keep its form open instead of jumping to an unrelated first row. */
        state.active = -1;
      } else {
        state.name = state.rows[0]?.name || null;
        state.active = 0;
      }
      fillSelect($('rhCategory'), state.filters.categories, state.category, t('roles.allCategories'));
      fillSelect($('rhAgent'), state.filters.agents, state.agent, t('roles.allAgents'));
      paintRows();
      paintStats();
      if (state.name && state.name !== state.keep && (!state.detail || state.detail.name !== state.name)) {
        await openRole(state.name, {quiet: true});
      }
    } catch (error) {
      state.error = String(error.message || error);
      set($('rhMeta'), state.error);
      set($('rhStats'), t('roles.unavailable'));
      $('rhList')?.replaceChildren();
    } finally {
      state.busy = false;
    }
  }

  /* Stats, the read stamp and the search hint all derive from the last answer,
     so a language switch repaints them without another request. */
  function paintStats() {
    if (!state.stats) return;
    $('rhSearch').placeholder = t('roles.search', {total: state.total});
    set($('rhStats'), t('roles.stats', {
      total: state.total,
      edited: state.stats.overridden,
      noKeywords: state.stats.without_keywords,
      teams: state.stats.teams,
    }));
    set($('rhUpdated'), state.observed_at
      ? t('roles.readAt', {time: new Date(state.observed_at * 1000).toLocaleTimeString()})
      : t('roles.unavailable'));
    paintMeta();
  }

  function paintMeta() {
    const first = state.matched ? (state.page - 1) * state.size + 1 : 0;
    const last = Math.min(state.matched, state.page * state.size);
    set($('rhMeta'), t('roles.range', {first, last, matched: state.matched, total: state.total}));
  }

  /* ── one role ────────────────────────────────────────────────────────── */
  /* A slower detail response must never overwrite a form the user has since
     opened, cleared or replaced with the New role form. */
  let openToken = 0;

  async function openRole(name, {quiet = false} = {}) {
    if (!name) return;
    const token = ++openToken;
    if (!quiet) set($('rhStatus'), t('roles.loading'));
    try {
      const body = await gui.api('/api/roles/detail?name=' + encodeURIComponent(name));
      if (token !== openToken) return;
      const data = body.data;
      state.detail = data;
      state.name = data.name;
      state.revision = data.revision || state.revision;
      paintDetail(data);
      if (!quiet) set($('rhStatus'), '');
    } catch (error) {
      if (token !== openToken) return;
      state.detail = null;
      $('rhForm').hidden = true;
      set($('rhEmpty'), t('roles.detailUnavailable', {error: String(error.message || error)}));
      $('rhEmpty').hidden = false;
    }
  }

  function paintDetail(data) {
    $('rhEmpty').hidden = true;
    $('rhForm').hidden = false;
    set($('rhTitle'), data.title || data.name);
    set($('rhName'), data.name);
    const badges = $('rhBadges');
    badges.replaceChildren(
      stateChip(data.category || 'general', 'muted'),
      stateChip(data.agent || t('roles.defaultAgent'), 'accent'),
      stateChip(data.override ? t('roles.edited') : t('roles.builtin'), data.override ? 'warn' : 'muted'),
      stateChip(t('roles.promptSize', {size: data.persona.length}), 'muted'),
    );
    $('rhFTitle').value = data.title || '';
    $('rhFCategory').value = data.category || '';
    fillSelect($('rhFAgent'), Object.keys(state.agents), data.agent || state.stats?.default_agent || '', undefined);
    $('rhFModel').value = data.model || '';
    $('rhFTemperature').value = data.temperature == null ? '' : String(data.temperature);
    $('rhFTools').value = Array.isArray(data.tools) ? data.tools.join(', ') : '';
    $('rhFKeywords').value = (data.keywords || []).map((k) => (k.weight === 1 ? k.text : `${k.text}:${k.weight}`)).join(', ');
    $('rhFAliases').value = (data.aliases || []).join(', ');
    $('rhFPersona').value = data.persona || '';
    $('rhFInstructions').value = data.instructions || '';
    $('rhHint').textContent = [
      t('roles.sourceHint', {source: data.source}),
      data.agent ? '' : t('roles.agentInherit', {agent: state.stats?.default_agent || ''}),
      data.removable ? '' : t('roles.builtinHint'),
      data.used_by?.teams?.length ? t('roles.usedBy', {teams: data.used_by.teams.join(', ')}) : '',
      data.editable ? '' : t('roles.demoHint'),
    ].filter(Boolean).join(' ');
    $('rhRemove').disabled = !data.removable || !data.editable;
    $('rhSave').disabled = !data.editable;
    $('rhExportOne').disabled = false;
    paintRows();
  }

  function parseCsv(value) {
    return String(value || '').split(',').map((item) => item.trim()).filter(Boolean);
  }

  function keywordList(value) {
    return parseCsv(value).map((item) => {
      const [text, weight] = item.split(':');
      const parsed = Number(weight);
      return Number.isFinite(parsed) ? {text: text.trim(), weight: parsed} : {text: item.trim()};
    });
  }

  function entryFromForm() {
    const entry = {
      title: $('rhFTitle').value.trim(),
      category: $('rhFCategory').value.trim() || 'general',
      agent: $('rhFAgent').value,
      persona: $('rhFPersona').value,
      instructions: $('rhFInstructions').value,
      keywords: keywordList($('rhFKeywords').value),
      aliases: parseCsv($('rhFAliases').value),
    };
    const model = $('rhFModel').value.trim();
    if (model) entry.model = model;
    const temperature = $('rhFTemperature').value.trim();
    if (temperature !== '') entry.temperature = Number(temperature);
    const tools = parseCsv($('rhFTools').value);
    if ($('rhFTools').value.trim()) entry.tools = tools;
    else if (state.detail?.tools !== null) entry.tools = null;
    return entry;
  }

  function typed(expected) {
    const answer = window.prompt(t('roles.confirmPrompt', {word: expected}) + '\n' + expected);
    if (answer !== expected) throw new Error(t('roles.cancelled'));
    return expected;
  }

  async function saveRole({name, action}) {
    const revision = state.revision || state.detail?.revision;
    if (!revision) throw new Error(t('roles.noRevision'));
    const confirmed = typed(`${action.toUpperCase()} ROLE ${name}`);
    const payload = {kind: 'role', name, action, revision, confirmation: confirmed};
    if (action === 'save') payload.entry = entryFromForm();
    root.dataset.busy = 'true';
    let body;
    try {
      body = await gui.api('/api/workbench/configuration', payload);
      flash($('rhStatus'), t('roles.saved', {name}), 'ok');
      state.detail = null;
      state.keep = name;
      await load({});
      await openRole(name, {quiet: true});
    } finally {
      state.keep = null;
      root.dataset.busy = 'false';
    }
    return body;
  }

  /* ── history and diff ────────────────────────────────────────────────── */
  async function loadHistory() {
    const body = await gui.api('/api/roles/history');
    state.history = body.data.versions;
    paintHistory();
  }

  function paintHistory() {
    const list = $('rhVersions');
    list.replaceChildren();
    if (!state.history.length) {
      const li = document.createElement('li');
      li.className = 'roles-empty';
      li.textContent = t('roles.noHistory');
      list.append(li);
      $('rhRestoreRow').hidden = true;
      return;
    }
    state.history.forEach((row) => {
      const li = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'roles-version';
      button.dataset.id = row.id;
      const title = document.createElement('b');
      title.textContent = row.reason || t('roles.edit');
      const meta = document.createElement('span');
      meta.textContent = `${clock(row.at)} · ${Math.round(row.bytes / 1024)} KB · ${row.id.slice(-6)}`;
      button.append(title, meta);
      button.onclick = () => showDiff(row.id);
      li.append(button);
      list.append(li);
    });
    $('rhRestoreRow').hidden = true;
  }

  async function showDiff(version) {
    const name = state.name || '';
    const body = await gui.api(`/api/roles/diff?version=${encodeURIComponent(version)}&name=${encodeURIComponent(name)}`);
    state.diff = body.data;
    const pre = $('rhDiff');
    pre.hidden = false;
    pre.textContent = state.diff.lines.length
      ? state.diff.lines.join('\n')
      : t('roles.noDiff', {name: name || t('roles.layer')});
    $('rhRestoreRow').hidden = false;
    $('rhRestore').dataset.version = version;
    state.history.forEach((row) => {
      const button = $('rhVersions').querySelector(`[data-id="${CSS.escape(row.id)}"]`);
      if (button) button.setAttribute('aria-current', String(row.id === version));
    });
    flash($('rhRestoreStatus'), t('roles.diffOf', {version: version.slice(-6), name: name || t('roles.layer')}));
  }

  async function restore() {
    const version = $('rhRestore').dataset.version;
    if (!version) return;
    const confirmed = typed(`RESTORE ROLES ${version}`);
    root.dataset.busy = 'true';
    let body;
    try {
      body = await gui.api('/api/roles/restore', {
        version, revision: state.revision || state.detail?.revision, confirmation: confirmed});
      flash($('rhRestoreStatus'), t('roles.restored', {version: version.slice(-6)}), 'ok');
      const name = state.name;
      state.detail = null;
      state.keep = name;
      await loadHistory();
      await load({});
      if (name) await openRole(name, {quiet: true});
    } finally {
      state.keep = null;
      root.dataset.busy = 'false';
    }
    return body;
  }

  /* ── quality ─────────────────────────────────────────────────────────── */
  async function loadQuality() {
    const body = await gui.api('/api/roles/quality');
    state.quality = body.data;
    paintQuality();
  }

  function paintQuality() {
    const data = state.quality;
    if (!data) return;
    set($('rhQualityMeta'), t('roles.qualityMeta', {
      total: data.total_roles, high: data.counts.high, low: data.counts.low, info: data.counts.info}));
    const list = $('rhIssues');
    list.replaceChildren();
    if (!data.issues.length) {
      const li = document.createElement('li');
      li.className = 'roles-empty';
      li.textContent = t('roles.clean');
      list.append(li);
    }
    data.issues.slice(0, 120).forEach((row) => {
      const li = document.createElement('li');
      li.dataset.severity = row.severity;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'roles-issue';
      const title = document.createElement('b');
      title.textContent = row.name;
      const meta = document.createElement('span');
      meta.textContent = `${row.code} · ${row.message}`;
      button.append(title, meta);
      button.onclick = () => { switchTab('role'); openRole(row.name); };
      li.append(button);
      list.append(li);
    });
    set($('rhQualityScope'), t('roles.qualityScope', {checked: data.checked.join(', '), unchecked: data.not_checked.join(', ')}));
  }

  /* ── route preview ───────────────────────────────────────────────────── */
  async function previewRoute() {
    const task = $('rhRouteTask').value.trim();
    if (!task) { flash($('rhRouteStatus'), t('roles.routeNeedTask'), 'warn'); return; }
    $('rhRouteGo').disabled = true;
    try {
      const body = await gui.api('/api/roles/route', {task});
      state.route = body.data;
      paintRoute();
      flash($('rhRouteStatus'), t('roles.routeDone', {calls: body.data.provider_calls}), 'ok');
    } catch (error) {
      flash($('rhRouteStatus'), String(error.message || error), 'error');
      $('rhRouteOut').replaceChildren();
    } finally {
      $('rhRouteGo').disabled = false;
    }
  }

  function paintRoute() {
    const data = state.route;
    const out = $('rhRouteOut');
    out.replaceChildren();
    if (!data) return;
    const head = document.createElement('p');
    head.className = 'roles-routehead';
    head.textContent = data.decision
      ? t('roles.routePick', {role: data.decision.role, method: data.decision.method, score: data.decision.score})
      : t('roles.routeNone', {threshold: data.threshold});
    out.append(head);
    if (data.decision?.hits?.length) {
      const hits = document.createElement('p');
      hits.className = 'roles-hint';
      hits.textContent = t('roles.routeHits', {hits: data.decision.hits.join(', ')});
      out.append(hits);
    }
    if (data.candidates.length) {
      const table = document.createElement('ol');
      table.className = 'roles-candidates';
      data.candidates.forEach((row) => {
        const li = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'roles-candidate';
        button.textContent = `${row.role} · ${row.score}`;
        button.onclick = () => { switchTab('role'); openRole(row.role); };
        li.append(button);
        table.append(li);
      });
      out.append(table);
    }
    const note = document.createElement('p');
    note.className = 'roles-hint';
    note.textContent = data.note;
    out.append(note);
  }

  /* ── tabs ────────────────────────────────────────────────────────────── */
  function switchTab(tab) {
    state.tab = tab;
    root.querySelectorAll('[data-tab]').forEach((button) => button.setAttribute('aria-selected', String(button.dataset.tab === tab)));
    root.querySelectorAll('.roles-panel').forEach((panel) => { panel.hidden = panel.dataset.panel !== tab; });
    if (tab === 'history') loadHistory().catch((error) => flash($('rhRestoreStatus'), String(error.message || error), 'error'));
    if (tab === 'quality') loadQuality().catch((error) => set($('rhQualityMeta'), String(error.message || error)));
  }

  /* ── export ──────────────────────────────────────────────────────────── */
  async function exportRoles(names, statusNode) {
    if (!names.length) { flash(statusNode, t('roles.exportNeed'), 'warn'); return; }
    const body = await gui.api('/api/roles/export', {names});
    const blob = new Blob([body.data.yaml], {type: 'text/yaml;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = body.data.filename;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    flash(statusNode, t('roles.exported', {count: body.data.count, kb: Math.max(1, Math.round(body.data.bytes / 1024))}), 'ok');
  }

  /* ── wiring ──────────────────────────────────────────────────────────── */
  const search = $('rhSearch');
  search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => load({q: search.value.trim(), page: 1}), 180);
  });
  search.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown') { event.preventDefault(); $('rhList').focus(); }
  });
  $('rhCategory').addEventListener('change', (event) => load({category: event.target.value, page: 1}));
  $('rhAgent').addEventListener('change', (event) => load({agent: event.target.value, page: 1}));
  $('rhSort').addEventListener('change', (event) => {
    const value = event.target.value;
    load({sort: value, dir: value === 'persona' || value === 'keywords' || value === 'updated' ? 'desc' : 'asc', page: 1});
  });
  $('rhList').addEventListener('scroll', () => paintRows(), {passive: true});
  $('rhList').addEventListener('keydown', (event) => {
    const keys = ['ArrowDown', 'ArrowUp', 'Home', 'End', 'PageDown', 'PageUp', 'Enter'];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'Enter') { if (state.name) openRole(state.name); return; }
    const delta = event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1
      : event.key === 'PageDown' ? 12 : event.key === 'PageUp' ? -12 : 0;
    let next = event.key === 'Home' ? 0 : event.key === 'End' ? state.rows.length - 1 : state.active + delta;
    next = Math.max(0, Math.min(state.rows.length - 1, next));
    if (next !== state.active) select(next, event.key !== 'ArrowUp' && event.key !== 'ArrowDown');
  });
  $('rhList').addEventListener('click', (event) => {
    const row = event.target.closest('.roles-row');
    if (row) select(Number(row.dataset.index), true);
  });
  root.querySelectorAll('[data-tab]').forEach((button) => { button.onclick = () => switchTab(button.dataset.tab); });
  root.querySelector('[role="tablist"]').addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...root.querySelectorAll('[data-tab]')];
    const index = tabs.findIndex((tab) => tab.dataset.tab === state.tab);
    const next = tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
    next.focus();
    switchTab(next.dataset.tab);
  });

  $('rhSave').onclick = async () => {
    if (!state.name) return;
    $('rhSave').disabled = true;
    try { await saveRole({name: state.name, action: 'save'}); }
    catch (error) { flash($('rhStatus'), String(error.message || error), 'error'); }
    finally { $('rhSave').disabled = false; }
  };
  $('rhRemove').onclick = async () => {
    if (!state.name) return;
    $('rhRemove').disabled = true;
    try {
      await saveRole({name: state.name, action: 'remove'});
      state.name = null;
    } catch (error) { flash($('rhStatus'), String(error.message || error), 'error'); }
    finally { $('rhRemove').disabled = false; }
  };
  $('rhExportOne').onclick = () => exportRoles(state.name ? [state.name] : [], $('rhStatus'))
    .catch((error) => flash($('rhStatus'), String(error.message || error), 'error'));
  $('rhExport').onclick = () => {
    const names = state.rows.filter((_, index) => index < 50).map((row) => row.name);
    exportRoles(names, null).catch((error) => set($('rhMeta'), String(error.message || error)));
  };
  $('rhRestore').onclick = async () => {
    $('rhRestore').disabled = true;
    try { await restore(); }
    catch (error) { flash($('rhRestoreStatus'), String(error.message || error), 'error'); }
    finally { $('rhRestore').disabled = false; }
  };
  $('rhRouteGo').onclick = previewRoute;
  $('rhNew').onclick = () => {
    openToken += 1;
    switchTab('role');
    state.name = null;
    state.detail = null;
    $('rhEmpty').hidden = true;
    $('rhForm').hidden = false;
    $('rhTitle').textContent = t('roles.newTitle');
    $('rhName').textContent = t('roles.newHint');
    $('rhBadges').replaceChildren(stateChip(t('roles.new'), 'accent'));
    ['rhFTitle', 'rhFCategory', 'rhFModel', 'rhFTemperature', 'rhFTools', 'rhFKeywords', 'rhFAliases',
      'rhFPersona', 'rhFInstructions'].forEach((id) => { $(id).value = ''; });
    fillSelect($('rhFAgent'), Object.keys(state.agents), state.stats?.default_agent || '', undefined);
    $('rhHint').textContent = t('roles.newFormHint');
    $('rhRemove').disabled = true;
    $('rhSave').disabled = false;
    $('rhFPersona').focus();
    // A new role needs a name: ask before the form is submitted.
    const name = window.prompt(t('roles.newNamePrompt'));
    if (!name) { $('rhEmpty').hidden = false; $('rhForm').hidden = true; return; }
    const clean = String(name).toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/-{2,}/g, '-').replace(/^-|-$/g, '').slice(0, 64);
    if (!clean) { flash($('rhStatus'), t('roles.newNameInvalid'), 'error'); return; }
    state.name = clean;
    set($('rhName'), clean);
    state.detail = {name: clean, revision: state.revision || null};
    // A brand-new role has no detail yet; borrow the current revision token.
    gui.api('/api/roles/detail?name=general').then((body) => {
      state.revision = body.data.revision;
    }).catch(() => {});
  };
  $('rhWorkbench').onclick = () => {
    if (typeof window.nexusWorkbenchShow === 'function') window.nexusWorkbenchShow('manage');
    else $('guiSettings')?.click();
  };

  /* ── lifecycle ───────────────────────────────────────────────────────── */
  async function ensureAgents() {
    if (Object.keys(state.agents).length) return;
    try {
      const body = await gui.api('/api/agents');
      state.agents = body || {};
    } catch { state.agents = {}; }
  }

  let loaded = false;
  async function boot() {
    if (loaded) return;
    loaded = true;
    await ensureAgents();
    await load({});
  }

  window.addEventListener('nexus:view', (event) => {
    if (event.detail === 'roles') boot().catch((error) => set($('rhMeta'), String(error.message || error)));
  });
  i18n.onChange(() => {
    i18n.apply(root);
    paintStats();
    if (state.rows.length) paintRows();
    if (state.detail) paintDetail(state.detail);
    if (state.tab === 'history') paintHistory();
    if (state.quality) paintQuality();
    if (state.route) paintRoute();
  });
  if (gui.view === 'roles') boot().catch(() => {});

  window.nexusRoleHub = {
    load, root,
    state: () => ({...state, rows: state.rows.length, items: state.rows.map((row) => row.name),
                   detail: state.detail?.name || null}),
    open: openRole, switchTab, previewRoute, history: loadHistory, quality: loadQuality,
  };
})();
