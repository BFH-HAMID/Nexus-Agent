/* Memory & knowledge: what the agent already knows, and what it may look up.

   Two halves, one page. Curated memory (MEMORY.md, USER.md, per-role notes)
   already reaches the agent; this page lets the operator read it, add to it,
   forget an entry and switch it off for dashboard runs. Document retrieval is
   new and **off by default**: nothing is indexed, read or injected until a
   typed token turns it on and a collection is indexed explicitly.

   The page reuses the module chrome (security-* classes) so the tools look
   like one product; memory-* classes carry only the parts unique to this page. */
(() => {
  'use strict';

  const gui = window.nexusGui;
  const i18n = window.nexusI18n;
  if (!gui || !i18n) return;
  const t = i18n.t;
  const $ = (id) => document.getElementById(id);

  const TABS = ['overview', 'memory', 'collections', 'retrieve', 'context'];
  const TOKENS = {
    save: 'SAVE MEMORY POLICY', index: 'INDEX COLLECTION', delete: 'DELETE COLLECTION',
    document: 'REMOVE DOCUMENT', add: 'ADD MEMORY ENTRY', forget: 'FORGET MEMORY ENTRY',
    clean: 'CLEAN MEMORY STORE',
    personalAdd: 'ADD PERSONAL MEMORY', personalConfirm: 'CONFIRM PROFILE FACT',
    personalEdit: 'EDIT PERSONAL MEMORY', personalDelete: 'DELETE PERSONAL MEMORY',
  };

  const state = {
    tab: 'overview', overview: null, policy: null, memory: null, personal: null,
    collections: null, detail: null, results: null, context: null, selected: '', busy: false,
    editingPersonalId: '',
  };

  const root = document.createElement('section');
  root.id = 'view-memory';
  root.className = 'gui-modules security-page memory-page';
  root.dataset.guiView = 'memory';
  root.hidden = gui.view !== 'memory';
  root.innerHTML = `
    <header class="security-head">
      <div>
        <p class="security-eyebrow" data-i18n="memory.eyebrow"></p>
        <h1 data-i18n="memory.title"></h1>
        <p class="security-sub" data-i18n="memory.subtitle"></p>
      </div>
      <div class="security-quick" role="group" aria-label="Memory actions">
        <button class="btn" type="button" id="memRefresh"><span class="btn-label" data-i18n="memory.refresh"></span></button>
        <button class="btn ghost" type="button" id="memOpenRun"><span class="btn-label" data-i18n="memory.openRun"></span></button>
        <span class="security-stamp" id="memUpdated" data-i18n="memory.never"></span>
      </div>
    </header>

    <p class="security-banner" id="memError" role="alert" hidden></p>

    <div class="security-toolbar">
      <div class="security-tabs" role="tablist" aria-label="Memory sections">
        <button type="button" role="tab" id="memTabOverview" data-mem-tab="overview" aria-selected="true" aria-controls="memPanelOverview" data-i18n="memory.tabOverview"></button>
        <button type="button" role="tab" id="memTabMemory" data-mem-tab="memory" aria-selected="false" aria-controls="memPanelMemory" data-i18n="memory.tabMemory"></button>
        <button type="button" role="tab" id="memTabCollections" data-mem-tab="collections" aria-selected="false" aria-controls="memPanelCollections" data-i18n="memory.tabCollections"></button>
        <button type="button" role="tab" id="memTabRetrieve" data-mem-tab="retrieve" aria-selected="false" aria-controls="memPanelRetrieve" data-i18n="memory.tabRetrieve"></button>
        <button type="button" role="tab" id="memTabContext" data-mem-tab="context" aria-selected="false" aria-controls="memPanelContext" data-i18n="memory.tabContext"></button>
      </div>
      <span class="security-chip" id="memChip" role="status" aria-live="polite">—</span>
    </div>

    <div class="security-panels">
      <section class="security-panel" id="memPanelOverview" role="tabpanel" aria-labelledby="memTabOverview" data-mem-panel="overview">
        <div class="security-kpis" id="memKpis"></div>

        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="memory.policy.title"></h2>
            <span class="security-stamp" id="memPolicyFile"></span>
          </div>
          <p class="security-note" data-i18n="memory.policy.intro"></p>
          <div class="memory-switches" id="memSwitches"></div>
          <div class="security-row">
            <label class="security-field"><span data-i18n="memory.policy.topK"></span>
              <input id="memTopK" type="number" min="1" max="12" step="1"></label>
            <label class="security-field"><span data-i18n="memory.policy.maxChars"></span>
              <input id="memMaxChars" type="number" min="1000" max="12000" step="500"></label>
            <label class="security-field"><span data-i18n="memory.policy.chunkChars"></span>
              <input id="memChunkChars" type="number" min="300" max="4000" step="100"></label>
          </div>
          <div class="security-row">
            <button class="btn" type="button" id="memPolicySave"><span class="btn-label" data-i18n="memory.policy.save"></span></button>
            <span class="security-stamp" id="memPolicyRevision"></span>
          </div>
          <ul class="security-notes" id="memPolicyNotes"></ul>
        </section>

        <section class="security-card">
          <div class="security-cardhead"><h2 data-i18n="memory.overview.title"></h2></div>
          <p class="security-note" data-i18n="memory.overview.intro"></p>
          <div class="memory-facts" id="memFacts"></div>
        </section>
      </section>

      <section class="security-panel" id="memPanelMemory" role="tabpanel" aria-labelledby="memTabMemory" data-mem-panel="memory" hidden>
        <div class="security-split">
          <section class="security-card">
            <div class="security-cardhead">
              <h2 data-i18n="memory.block.memoryTitle"></h2>
              <span class="security-stamp" id="memMemoryCount"></span>
            </div>
            <p class="security-note" data-i18n="memory.block.memoryIntro"></p>
            <ul class="memory-entries" id="memMemoryEntries"></ul>
            <label class="security-field"><span data-i18n="memory.block.addLabel"></span>
              <textarea id="memAddText" rows="3" maxlength="600" data-i18n-ph="memory.block.addPlaceholder"></textarea></label>
            <div class="security-row">
              <label class="security-field"><span data-i18n="memory.block.section"></span>
                <select id="memAddSection">
                  <option value="memory" data-i18n="memory.block.memoryOption"></option>
                  <option value="user" data-i18n="memory.block.userOption"></option>
                </select></label>
              <button class="btn" type="button" id="memAdd"><span class="btn-label" data-i18n="memory.block.add"></span></button>
            </div>
          </section>
          <section class="security-card">
            <div class="security-cardhead">
              <h2 data-i18n="memory.block.userTitle"></h2>
              <span class="security-stamp" id="memUserCount"></span>
            </div>
            <p class="security-note" data-i18n="memory.block.userIntro"></p>
            <ul class="memory-entries" id="memUserEntries"></ul>
          </section>
        </div>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="memory.notes.title"></h2>
            <span class="security-stamp" id="memNotesDir"></span>
          </div>
          <p class="security-note" data-i18n="memory.notes.intro"></p>
          <div class="security-tablewrap">
            <table class="security-table">
              <thead><tr>
                <th data-i18n="memory.notes.colRole"></th>
                <th data-i18n="memory.notes.colChars"></th>
                <th data-i18n="memory.notes.colUpdated"></th>
                <th data-i18n="memory.notes.colPreview"></th>
              </tr></thead>
              <tbody id="memNoteRows"></tbody>
            </table>
          </div>
        </section>

        <section class="security-card" id="memPersonalCard">
          <div class="security-cardhead">
            <h2 data-i18n="memory.personal.title"></h2>
            <span class="security-stamp" id="memPersonalStatus"></span>
          </div>
          <p class="security-note" data-i18n="memory.personal.intro"></p>
          <div class="security-row">
            <label class="security-field security-grow"><span data-i18n="memory.personal.search"></span>
              <input id="memPersonalSearch" type="search" maxlength="200" autocomplete="off" data-i18n-ph="memory.personal.searchPlaceholder"></label>
            <button class="btn ghost" type="button" id="memPersonalSearchBtn"><span class="btn-label" data-i18n="memory.personal.search"></span></button>
            <button class="btn ghost" type="button" id="memPersonalExport"><span class="btn-label" data-i18n="memory.personal.export"></span></button>
          </div>
          <ul class="memory-entries" id="memPersonalEntries"></ul>
          <label class="security-field"><span data-i18n="memory.personal.addLabel"></span>
            <textarea id="memPersonalAddText" rows="2" maxlength="1200" data-i18n-ph="memory.personal.addPlaceholder"></textarea></label>
          <div class="security-row">
            <label class="security-field"><span>Store</span>
              <select id="memPersonalStore">
                <option value="profile">Profile</option>
                <option value="vector">Vector memory</option>
              </select></label>
            <label class="security-field"><span>Category</span>
              <select id="memPersonalCategory">
                <option value="">Auto</option>
                <option value="preferences">preferences</option>
                <option value="routines">routines</option>
                <option value="languages">languages</option>
                <option value="people">people</option>
                <option value="projects">projects</option>
                <option value="working_hours">working_hours</option>
                <option value="tone_preferences">tone_preferences</option>
                <option value="fact">fact</option>
              </select></label>
            <button class="btn" type="button" id="memPersonalAdd"><span class="btn-label" data-i18n="memory.personal.add"></span></button>
          </div>
        </section>
      </section>

      <section class="security-panel" id="memPanelCollections" role="tabpanel" aria-labelledby="memTabCollections" data-mem-panel="collections" hidden>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="memory.collections.title"></h2>
            <span class="security-stamp" id="memStoreLine"></span>
          </div>
          <p class="security-note" data-i18n="memory.collections.intro"></p>
          <div class="security-tablewrap">
            <table class="security-table">
              <thead><tr>
                <th data-i18n="memory.collections.colName"></th>
                <th data-i18n="memory.collections.colPatterns"></th>
                <th data-i18n="memory.collections.colDocuments"></th>
                <th data-i18n="memory.collections.colChunks"></th>
                <th data-i18n="memory.collections.colUpdated"></th>
                <th data-i18n="memory.collections.colAction"></th>
              </tr></thead>
              <tbody id="memCollectionRows"></tbody>
            </table>
          </div>
        </section>

        <section class="security-card">
          <div class="security-cardhead"><h2 data-i18n="memory.build.title"></h2></div>
          <p class="security-note" data-i18n="memory.build.intro"></p>
          <div class="memory-build">
            <label class="security-field"><span data-i18n="memory.build.name"></span>
              <input id="memBuildName" type="text" maxlength="32" placeholder="docs" spellcheck="false"></label>
            <label class="security-field"><span data-i18n="memory.build.patterns"></span>
              <textarea id="memBuildPatterns" rows="2" spellcheck="false" placeholder="docs&#10;README.md"></textarea></label>
          </div>
          <div class="security-row">
            <button class="btn" type="button" id="memBuild"><span class="btn-label" data-i18n="memory.build.go"></span></button>
            <span class="security-stamp" data-i18n="memory.build.hint"></span>
          </div>
        </section>

        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="memory.documents.title"></h2>
            <span class="security-stamp" id="memDetailName"></span>
          </div>
          <p class="security-note" data-i18n="memory.documents.intro"></p>
          <div class="security-tablewrap">
            <table class="security-table">
              <thead><tr>
                <th data-i18n="memory.documents.colPath"></th>
                <th data-i18n="memory.documents.colChunks"></th>
                <th data-i18n="memory.documents.colBytes"></th>
                <th data-i18n="memory.documents.colAction"></th>
              </tr></thead>
              <tbody id="memDocumentRows"></tbody>
            </table>
          </div>
        </section>

        <section class="security-card">
          <div class="security-cardhead"><h2 data-i18n="memory.cleanup.title"></h2></div>
          <p class="security-note" data-i18n="memory.cleanup.intro"></p>
          <div class="security-row">
            <button class="btn ghost" type="button" id="memClean"><span class="btn-label" data-i18n="memory.cleanup.go"></span></button>
          </div>
        </section>
      </section>

      <section class="security-panel" id="memPanelRetrieve" role="tabpanel" aria-labelledby="memTabRetrieve" data-mem-panel="retrieve" hidden>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="memory.retrieve.title"></h2>
            <span class="security-stamp" id="memEngine"></span>
          </div>
          <p class="security-note" data-i18n="memory.retrieve.intro"></p>
          <p class="security-banner" id="memRetrieveOff" role="status" hidden></p>
          <div class="security-row">
            <label class="security-field security-grow"><span data-i18n="memory.retrieve.query"></span>
              <input id="memQuery" type="search" maxlength="400" autocomplete="off" data-i18n-ph="memory.retrieve.queryHint"></label>
            <label class="security-field"><span data-i18n="memory.retrieve.collection"></span>
              <select id="memQueryCollection"><option value="" data-i18n="memory.retrieve.allCollections"></option></select></label>
            <label class="security-field"><span data-i18n="memory.retrieve.limit"></span>
              <input id="memQueryLimit" type="number" min="1" max="12" step="1" value="6"></label>
            <button class="btn" type="button" id="memSearch"><span class="btn-label" data-i18n="memory.retrieve.go"></span></button>
          </div>
          <p class="security-note" id="memSearchNote"></p>
        </section>
        <section class="security-card" id="memResultsCard" hidden>
          <div class="security-cardhead">
            <h2 data-i18n="memory.results.title"></h2>
            <span class="security-stamp" id="memResultsMeta"></span>
          </div>
          <ol class="memory-results" id="memResults"></ol>
        </section>
      </section>

      <section class="security-panel" id="memPanelContext" role="tabpanel" aria-labelledby="memTabContext" data-mem-panel="context" hidden>
        <section class="security-card">
          <div class="security-cardhead"><h2 data-i18n="memory.context.title"></h2></div>
          <p class="security-note" data-i18n="memory.context.intro"></p>
          <div class="security-row">
            <label class="security-field security-grow"><span data-i18n="memory.context.task"></span>
              <input id="memTask" type="text" maxlength="400" autocomplete="off" data-i18n-ph="memory.context.taskHint"></label>
            <button class="btn" type="button" id="memPreview"><span class="btn-label" data-i18n="memory.context.go"></span></button>
          </div>
          <p class="security-note" id="memContextRetrieval"></p>
        </section>
        <section class="security-card" id="memContextCard" hidden>
          <div class="security-cardhead">
            <h2 data-i18n="memory.context.resultTitle"></h2>
            <span class="security-stamp" id="memContextMeta"></span>
          </div>
          <div class="memory-budget"><span id="memBudgetBar"></span></div>
          <div class="memory-facts" id="memContextFacts"></div>
          <pre class="memory-context" id="memContextText"></pre>
          <p class="security-note" data-i18n="memory.context.caveat"></p>
        </section>
      </section>
    </div>`;
  (document.querySelector('main.deck') || document.body).after(root);
  // The typed-token sheet sits right under the tabs, where the operator is
  // looking — never below a long panel they would have to scroll to find.
  const host = document.createElement('div');
  host.id = 'memActionHost';
  host.hidden = true;
  root.querySelector('.security-toolbar').after(host);

  /* ── helpers ────────────────────────────────────────────────────────── */
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  const message = (error) => error?.payload?.error?.message || error?.message || String(error);
  const stampOf = (value) => {
    if (!value) return '—';
    const date = new Date(value * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    const options = {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'};
    if (date.getFullYear() !== new Date().getFullYear()) options.year = 'numeric';
    return date.toLocaleString([], options);
  };
  const bytesOf = (size) => {
    const n = Number(size) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1048576).toFixed(2)} MB`;
  };
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const chip = (text, tone) => {
    const node = el('span', 'memory-chip', text);
    if (tone) node.dataset.tone = tone;
    return node;
  };
  function showError(error) {
    const banner = $('memError');
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
  const api = (path, body) => gui.api(path, body).then((reply) => reply.data);

  /* ── confirm sheet ──────────────────────────────────────────────────── */
  function closeAction() {
    const host = $('memActionHost');
    host.hidden = true;
    host.replaceChildren();
  }
  function openAction({title, token, detail, onConfirm, extraFields = ''}) {
    const host = $('memActionHost');
    host.hidden = false;
    host.innerHTML = `
      <section class="security-card security-confirm" role="group" aria-labelledby="memActionTitle">
        <div class="security-cardhead"><h2 id="memActionTitle">${esc(title)}</h2></div>
        <p class="security-note">${esc(detail || '')}</p>
        ${extraFields}
        <p class="security-code" id="memActionExpected">${esc(token)}</p>
        <label class="security-field"><span data-i18n="memory.confirm.tokenLabel"></span>
          <input id="memActionToken" type="text" autocomplete="off" spellcheck="false" autocapitalize="characters"></label>
        <div class="security-confirmrow">
          <button class="btn" type="button" id="memActionGo" disabled><span class="btn-label" data-i18n="memory.confirm.go"></span></button>
          <button class="btn ghost" type="button" id="memActionCancel"><span class="btn-label" data-i18n="memory.confirm.cancel"></span></button>
        </div>
        <p class="security-note" id="memActionError" role="alert"></p>
      </section>`;
    i18n.apply(host);
    const input = $('memActionToken');
    const go = $('memActionGo');
    input.focus();
    const sync = () => { go.disabled = input.value.trim() !== token; };
    input.oninput = sync;
    go.onclick = async () => {
      if (go.disabled) return;
      go.disabled = true;
      $('memActionError').textContent = '';
      try {
        await onConfirm({confirmation: token});
        closeAction();
      } catch (error) {
        $('memActionError').textContent = message(error);
        go.disabled = false;
      }
    };
    $('memActionCancel').onclick = closeAction;
    host.scrollIntoView({block: 'nearest'});
  }

  /* ── tabs ───────────────────────────────────────────────────────────── */
  function switchTab(tab) {
    if (!TABS.includes(tab)) return;
    state.tab = tab;
    for (const name of TABS) {
      const panel = $(`memPanel${name[0].toUpperCase()}${name.slice(1)}`);
      const button = $(`memTab${name[0].toUpperCase()}${name.slice(1)}`);
      const on = name === tab;
      if (panel) panel.hidden = !on;
      if (button) button.setAttribute('aria-selected', String(on));
    }
    closeAction();
    paintChip();
    if (tab === 'memory') loadMemory().catch(showError);
    if (tab === 'collections') loadCollections().catch(showError);
    if (tab === 'retrieve') loadRetrieval().catch(showError);
    if (tab === 'context') paintContext();
    if (tab === 'overview') loadOverview().catch(showError);
  }

  /* ── overview ───────────────────────────────────────────────────────── */
  function kpi(label, value, tone) {
    const node = el('div', 'security-count');
    if (tone) node.dataset.tone = tone;
    node.append(el('b', '', value), el('span', '', label));
    return node;
  }

  function paintOverview() {
    const data = state.overview;
    if (!data) return;
    const policy = data.policy || {};
    const memory = data.memory || {};
    const totals = data.totals || {};
    $('memKpis').replaceChildren(
      kpi(t('memory.kpi.memory'), String(memory.memory?.entries?.length ?? 0), 'ok'),
      kpi(t('memory.kpi.user'), String(memory.user?.entries?.length ?? 0), 'ok'),
      kpi(t('memory.kpi.notes'), String(memory.notes?.files?.length ?? 0), ''),
      kpi(t('memory.kpi.collections'), String(totals.collections ?? 0), ''),
      kpi(t('memory.kpi.documents'), String(totals.documents ?? 0), ''),
      kpi(t('memory.kpi.chunks'), String(totals.chunks ?? 0), ''),
    );
    const switches = $('memSwitches');
    switches.replaceChildren();
    for (const [key, label] of [['memory', 'memory.policy.switchMemory'],
      ['rag', 'memory.policy.switchRag'], ['inject', 'memory.policy.switchInject']]) {
      const box = el('label', 'security-switch');
      const input = el('input');
      input.type = 'checkbox';
      input.id = `memSwitch${key[0].toUpperCase()}${key.slice(1)}`;
      input.checked = Boolean(policy[key]);
      input.disabled = key === 'inject' && !policy.rag;
      if (key === 'rag') {
        input.onchange = () => {
          const injectBox = $('memSwitchInject');
          if (!input.checked) injectBox.checked = false;
          injectBox.disabled = !input.checked;
        };
      }
      box.append(input, el('span', '', t(label)));
      switches.append(box);
    }
    $('memTopK').value = policy.top_k ?? 6;
    $('memMaxChars').value = policy.max_chars ?? 4000;
    $('memChunkChars').value = policy.chunk_chars ?? 1200;
    $('memPolicyFile').textContent = t('memory.policy.file', {path: data.file || '—'});
    $('memPolicyRevision').textContent = data.revision
      ? t('memory.policy.revision', {rev: data.revision.slice(0, 12)})
      : t('memory.policy.unsaved');
    const notes = $('memPolicyNotes');
    notes.replaceChildren();
    for (const key of ['retrieval_off', 'index_is_local', 'excerpts_are_data', 'memory_files']) {
      notes.append(el('li', '', t(`memory.notes.${key}`)));
    }
    if (data.policy_error) notes.append(el('li', '', data.policy_error));
    const environment = Object.entries(data.environment || {});
    if (environment.length) {
      notes.append(el('li', '', t('memory.policy.environment', {
        name: environment[0][0], value: environment[0][1]})));
    }
    const facts = $('memFacts');
    facts.replaceChildren();
    for (const [term, value] of [
      [t('memory.overview.store'), data.store?.path || '—'],
      [t('memory.overview.size'), bytesOf(data.store?.bytes || 0)],
      [t('memory.overview.engine'), data.store?.engine || t('memory.overview.noIndex')],
      [t('memory.overview.roleNotes'), memory.notes?.dir || '—'],
      [t('memory.overview.memoryFile'), memory.memory?.path || '—'],
      [t('memory.overview.userFile'), memory.user?.path || '—'],
    ]) {
      facts.append(el('span', 'memory-fact-term', term), el('span', 'memory-fact-value', String(value)));
    }
    $('memUpdated').textContent = t('memory.updated', {when: stampOf(data.observed_at)});
    paintChip();
  }

  async function loadOverview() {
    state.overview = await api('/api/knowledge/overview');
    state.collections = {collections: state.overview.collections, store: state.overview.store};
    paintOverview();
    paintCollectionPicker();
  }

  /* ── memory entries ─────────────────────────────────────────────────── */
  function entryList(host, entries, section) {
    host.replaceChildren();
    if (!entries.length) {
      host.append(el('li', 'security-empty', t('memory.block.empty')));
      return;
    }
    for (const entry of entries) {
      const item = el('li', 'memory-entry');
      item.append(el('p', 'memory-entry-text', entry.text));
      const row = el('div', 'memory-entry-foot');
      row.append(el('span', 'security-stamp', entry.id));
      const forget = el('button', 'btn ghost security-small', t('memory.block.forget'));
      forget.type = 'button';
      forget.onclick = () => askForget(entry, section);
      row.append(forget);
      item.append(row);
      host.append(item);
    }
  }

  function paintMemory() {
    const data = state.memory;
    if (!data) return;
    entryList($('memMemoryEntries'), data.memory?.entries || [], 'memory');
    entryList($('memUserEntries'), data.user?.entries || [], 'user');
    $('memMemoryCount').textContent = t('memory.block.count', {
      chars: data.memory?.chars ?? 0, limit: data.limits?.memory ?? 0});
    $('memUserCount').textContent = t('memory.block.count', {
      chars: data.user?.chars ?? 0, limit: data.limits?.user ?? 0});
    $('memNotesDir').textContent = data.notes?.dir || '—';
    const rows = $('memNoteRows');
    rows.replaceChildren();
    const files = data.notes?.files || [];
    if (!files.length) {
      const tr = el('tr');
      const td = el('td', 'security-empty', t('memory.notes.empty'));
      td.colSpan = 4;
      tr.append(td);
      rows.append(tr);
    }
    for (const note of files) {
      const tr = el('tr');
      const role = el('td');
      role.append(el('b', '', note.role), el('span', 'security-subline', ` ${note.path}`));
      tr.append(role, el('td', 'security-mono', String(note.chars)),
        el('td', 'security-mono', stampOf(note.updated)),
        el('td', 'security-target', note.preview || '—'));
      rows.append(tr);
    }
    if (!data.enabled) $('memError').textContent = t('memory.block.disabled');
    paintPersonal();
  }

  function paintPersonal() {
    const data = state.personal;
    const host = $('memPersonalEntries');
    if (!host) return;
    host.replaceChildren();
    if (!data) return;
    $('memPersonalStatus').textContent = data.enabled
      ? `${data.vector_count ?? 0} vectors · ${data.encrypted ? 'encrypted AES-GCM' : 'local'}`
      : t('memory.personal.disabled');
    const entries = data.entries || [];
    if (!entries.length) {
      host.append(el('li', 'security-empty', t('memory.personal.empty')));
      return;
    }
    for (const entry of entries) {
      const item = el('li', 'memory-entry');
      const head = el('div', 'memory-result-head');
      head.append(
        chip(entry.store || 'profile', ''),
        chip(entry.category || entry.kind || 'fact', ''),
        chip(entry.status || 'confirmed', entry.status === 'suggested' ? 'warn' : 'ok'),
        el('span', 'security-stamp', `source: ${entry.source || 'user'} · ${stampOf(entry.updated_at || entry.created_at)}`),
      );
      item.append(head);
      if (state.editingPersonalId === entry.id) {
        const input = el('textarea', 'security-field');
        input.rows = 2;
        input.value = entry.text || '';
        input.id = `memPersonalEditInput_${entry.id}`;
        const actions = el('div', 'security-row');
        const save = el('button', 'btn security-small', t('memory.personal.save'));
        save.type = 'button';
        save.onclick = () => askPersonalEdit(entry, input.value);
        const cancel = el('button', 'btn ghost security-small', t('memory.personal.cancel'));
        cancel.type = 'button';
        cancel.onclick = () => { state.editingPersonalId = ''; paintPersonal(); };
        actions.append(save, cancel);
        item.append(input, actions);
      } else {
        item.append(el('p', 'memory-entry-text', entry.text));
        const foot = el('div', 'memory-entry-foot');
        foot.append(el('span', 'security-stamp', `${entry.id} · ${entry.date || ''}`));
        const btns = el('div', 'security-row');
        if (entry.status === 'suggested') {
          const conf = el('button', 'btn security-small', t('memory.personal.confirm'));
          conf.type = 'button';
          conf.onclick = () => askPersonalConfirm(entry, 'confirm');
          const rej = el('button', 'btn ghost security-small', t('memory.personal.reject'));
          rej.type = 'button';
          rej.onclick = () => askPersonalConfirm(entry, 'reject');
          btns.append(conf, rej);
        }
        const editBtn = el('button', 'btn ghost security-small', t('memory.personal.edit'));
        editBtn.type = 'button';
        editBtn.onclick = () => { state.editingPersonalId = entry.id; paintPersonal(); };
        const delBtn = el('button', 'btn ghost security-small', t('memory.personal.delete'));
        delBtn.type = 'button';
        delBtn.onclick = () => askPersonalDelete(entry);
        btns.append(editBtn, delBtn);
        foot.append(btns);
        item.append(foot);
      }
      host.append(item);
    }
  }

  async function loadPersonal(query = '') {
    const q = query ? `?q=${encodeURIComponent(query)}` : '';
    state.personal = await api(`/api/knowledge/personal/list${q}`);
    paintPersonal();
  }

  async function loadMemory() {
    state.memory = await api('/api/knowledge/memory');
    state.personal = await api('/api/knowledge/personal/list').catch(() => null);
    paintMemory();
  }

  function askPersonalAdd() {
    const text = ($('memPersonalAddText').value || '').trim();
    if (!text) { showError(t('memory.block.addEmpty')); return; }
    const store = $('memPersonalStore').value || 'profile';
    const category = $('memPersonalCategory').value || '';
    openAction({
      title: t('memory.personal.addTitle'),
      token: TOKENS.personalAdd,
      detail: t('memory.personal.addDetail', {store}),
      onConfirm: async (payload) => {
        state.personal = await api('/api/knowledge/personal/add', {...payload, text, store, category});
        $('memPersonalAddText').value = '';
        paintPersonal();
        window.nexusToast?.(t('memory.block.added'), {tone: 'done'});
      },
    });
  }

  function askPersonalConfirm(entry, action) {
    openAction({
      title: t('memory.personal.confirmTitle'),
      token: `${TOKENS.personalConfirm} ${entry.id}`,
      detail: t('memory.personal.confirmDetail', {id: entry.id}),
      onConfirm: async (payload) => {
        state.personal = await api('/api/knowledge/personal/confirm', {...payload, id: entry.id, action});
        paintPersonal();
      },
    });
  }

  function askPersonalEdit(entry, text) {
    const clean = (text || '').trim();
    if (!clean) { showError(t('memory.block.addEmpty')); return; }
    openAction({
      title: t('memory.personal.editTitle'),
      token: `${TOKENS.personalEdit} ${entry.id}`,
      detail: t('memory.personal.editDetail', {id: entry.id}),
      onConfirm: async (payload) => {
        state.personal = await api('/api/knowledge/personal/edit', {...payload, id: entry.id, text: clean});
        state.editingPersonalId = '';
        paintPersonal();
      },
    });
  }

  function askPersonalDelete(entry) {
    openAction({
      title: t('memory.personal.deleteTitle'),
      token: `${TOKENS.personalDelete} ${entry.id}`,
      detail: t('memory.personal.deleteDetail', {id: entry.id}),
      onConfirm: async (payload) => {
        state.personal = await api('/api/knowledge/personal/delete', {...payload, id: entry.id});
        paintPersonal();
      },
    });
  }

  async function exportPersonalJson() {
    const exported = await api('/api/knowledge/personal/export');
    const blob = new Blob([JSON.stringify(exported, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `nexus-personal-memory-${exported.exported_date || 'export'}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    window.nexusToast?.(t('memory.personal.exported'), {tone: 'done'});
  }

  async function askAdd() {
    const text = $('memAddText').value.trim();
    if (!text) { showError(t('memory.block.addEmpty')); return; }
    const section = $('memAddSection').value;
    openAction({
      title: t('memory.block.addTitle'),
      token: TOKENS.add,
      detail: t('memory.block.addDetail', {section}),
      onConfirm: async (payload) => {
        state.memory = await api('/api/knowledge/entry/add', {...payload, text, section});
        $('memAddText').value = '';
        paintMemory();
        await loadOverview();
        window.nexusToast?.(t('memory.block.added'), {tone: 'done'});
      },
    });
  }

  function askForget(entry, section) {
    openAction({
      title: t('memory.block.forgetTitle'),
      token: `${TOKENS.forget} ${entry.id}`,
      detail: t('memory.block.forgetDetail', {id: entry.id}),
      onConfirm: async (payload) => {
        state.memory = await api('/api/knowledge/entry/remove', {...payload, section, id: entry.id});
        paintMemory();
        await loadOverview();
        window.nexusToast?.(t('memory.block.forgotten'), {tone: 'done'});
      },
    });
  }

  /* ── collections ────────────────────────────────────────────────────── */
  function paintCollections() {
    const data = state.collections || {};
    const tbody = $('memCollectionRows');
    tbody.replaceChildren();
    const rows = data.collections || [];
    if (!rows.length) {
      const tr = el('tr');
      const td = el('td', 'security-empty', t('memory.collections.empty'));
      td.colSpan = 6;
      tr.append(td);
      tbody.append(tr);
    }
    for (const row of rows) {
      const tr = el('tr');
      tr.dataset.collection = row.name;
      const name = el('td');
      name.append(el('b', '', row.name), el('span', 'security-subline', ` ${row.encoder || ''}`));
      const patterns = el('td', 'security-mono security-target', (row.patterns || []).join(' · ') || '—');
      patterns.title = (row.patterns || []).join('\n');
      const box = el('div', 'security-row');
      const open = el('button', 'btn ghost security-small', t('memory.collections.open'));
      open.type = 'button';
      open.onclick = () => guard(async () => {
        state.detail = await api(`/api/knowledge/collection?name=${encodeURIComponent(row.name)}`);
        paintDocuments();
        $('memDetailName').scrollIntoView({block: 'nearest'});
      });
      const index = el('button', 'btn ghost security-small', t('memory.collections.reindex'));
      index.type = 'button';
      index.onclick = () => askIndex(row.name, null);
      const remove = el('button', 'btn ghost security-small', t('memory.collections.delete'));
      remove.type = 'button';
      remove.onclick = () => askDelete(row.name);
      box.append(open, index, remove);
      tr.append(name, patterns, el('td', 'security-mono', String(row.documents ?? 0)),
        el('td', 'security-mono', String(row.chunks ?? 0)),
        el('td', 'security-mono', stampOf(row.updated)), el('td', ''));
      tr.lastChild.append(box);
      tbody.append(tr);
    }
    const store = data.store || {};
    $('memStoreLine').textContent = t('memory.collections.store', {
      path: store.path || '—', size: bytesOf(store.bytes || 0),
      engine: store.engine || t('memory.overview.noIndex')});
    paintCollectionPicker();
    if (!state.detail && rows.length) state.selected = rows[0].name;
  }

  function paintCollectionPicker() {
    const select = $('memQueryCollection');
    if (!select) return;
    const rows = state.collections?.collections || [];
    const previous = select.value;
    select.replaceChildren(new Option(t('memory.retrieve.allCollections'), ''));
    for (const row of rows) select.append(new Option(row.name, row.name));
    if (previous && [...select.options].some((option) => option.value === previous)) select.value = previous;
  }

  function paintDocuments() {
    const detail = state.detail;
    const tbody = $('memDocumentRows');
    tbody.replaceChildren();
    if (!detail) {
      const tr = el('tr');
      const td = el('td', 'security-empty', t('memory.documents.pick'));
      td.colSpan = 4;
      tr.append(td);
      tbody.append(tr);
      $('memDetailName').textContent = '—';
      return;
    }
    $('memDetailName').textContent = t('memory.documents.heading', {
      name: detail.collection?.name || '—', count: detail.documents?.length ?? 0});
    if (!detail.documents?.length) {
      const tr = el('tr');
      const td = el('td', 'security-empty', t('memory.documents.empty'));
      td.colSpan = 4;
      tr.append(td);
      tbody.append(tr);
      return;
    }
    for (const doc of detail.documents) {
      const tr = el('tr');
      const path = el('td');
      path.append(el('b', '', doc.path), el('span', 'security-subline', ` ${doc.id}`));
      const action = el('td');
      const remove = el('button', 'btn ghost security-small', t('memory.documents.remove'));
      remove.type = 'button';
      remove.onclick = () => askRemoveDocument(doc, detail.collection?.name || '');
      action.append(remove);
      tr.append(path, el('td', 'security-mono', String(doc.chunks)),
        el('td', 'security-mono', bytesOf(doc.bytes)), action);
      tbody.append(tr);
    }
  }

  async function loadCollections() {
    if (state.overview) {
      state.collections = {collections: state.overview.collections, store: state.overview.store};
    } else {
      state.collections = await api('/api/knowledge/collections');
    }
    paintCollections();
    if (state.selected && state.detail?.collection?.name !== state.selected) {
      state.detail = await api(`/api/knowledge/collection?name=${encodeURIComponent(state.selected)}`)
        .catch(() => null);
    }
    paintDocuments();
  }

  function askIndex(name, patterns) {
    openAction({
      title: t('memory.build.title'),
      token: `${TOKENS.index} ${name}`,
      detail: t('memory.build.detail', {name}),
      onConfirm: async (payload) => {
        const body = {...payload, name};
        if (patterns && patterns.length) body.patterns = patterns;
        const result = await api('/api/knowledge/index', body);
        state.detail = result;
        state.selected = name;
        await loadOverview();
        state.collections = {collections: state.overview.collections, store: state.overview.store};
        paintCollections();
        paintDocuments();
        window.nexusToast?.(t('memory.build.done', {
          name, docs: result.indexed?.length ?? 0, chunks: result.chunks ?? 0}), {tone: 'done'});
      },
    });
  }

  function askDelete(name) {
    openAction({
      title: t('memory.collections.deleteTitle'),
      token: `${TOKENS.delete} ${name}`,
      detail: t('memory.collections.deleteDetail', {name}),
      onConfirm: async (payload) => {
        await api('/api/knowledge/collection/delete', {...payload, name});
        if (state.selected === name) { state.selected = ''; state.detail = null; }
        await loadOverview();
        state.collections = {collections: state.overview.collections, store: state.overview.store};
        paintCollections();
        paintDocuments();
        window.nexusToast?.(t('memory.collections.deleted', {name}), {tone: 'done'});
      },
    });
  }

  function askRemoveDocument(doc, name) {
    openAction({
      title: t('memory.documents.removeTitle'),
      token: `${TOKENS.document} ${doc.id}`,
      detail: t('memory.documents.removeDetail', {path: doc.path, name}),
      onConfirm: async (payload) => {
        state.detail = await api('/api/knowledge/document/delete', {...payload, id: doc.id});
        paintDocuments();
        await loadOverview();
        window.nexusToast?.(t('memory.documents.removed'), {tone: 'done'});
      },
    });
  }

  function askClean() {
    openAction({
      title: t('memory.cleanup.title'),
      token: TOKENS.clean,
      detail: t('memory.cleanup.detail'),
      onConfirm: async (payload) => {
        const result = await api('/api/knowledge/cleanup', payload);
        state.collections = result;
        await loadOverview();
        paintCollections();
        paintDocuments();
        window.nexusToast?.(t('memory.cleanup.done', {
          docs: result.removed?.documents ?? 0, chunks: result.removed?.chunks ?? 0}), {tone: 'done'});
      },
    });
  }

  /* ── retrieval ──────────────────────────────────────────────────────── */
  async function loadRetrieval() {
    if (!state.overview) await loadOverview();
    const policy = state.overview?.policy || {};
    const off = $('memRetrieveOff');
    off.hidden = Boolean(policy.rag);
    off.textContent = t('memory.retrieve.off');
    $('memSearch').disabled = !policy.rag;
    $('memEngine').textContent = state.overview?.store?.engine || '—';
    paintChip();
  }

  function paintResults() {
    const found = state.results;
    if (!found) return;
    $('memResultsCard').hidden = false;
    $('memSearchNote').textContent = found.note || '';
    $('memResultsMeta').textContent = found.rag === false
      ? t('memory.retrieve.off')
      : t('memory.results.meta', {count: found.results?.length ?? 0, mode: found.mode || '—',
        engine: found.engine || '—'});
    const list = $('memResults');
    list.replaceChildren();
    if (!found.results?.length) {
      list.append(el('li', 'security-empty', t('memory.results.empty')));
      return;
    }
    for (const row of found.results) {
      const item = el('li', 'memory-result');
      const head = el('div', 'memory-result-head');
      head.append(el('b', '', `${row.rank}. ${row.path}`),
        el('span', 'security-mono', `:${row.start_line}-${row.end_line}`),
        chip(t('memory.results.score', {score: Number(row.score).toFixed(2)}), 'ok'),
        chip(t('memory.results.coverage', {percent: Math.round((row.coverage || 0) * 100)}), ''),
        chip(row.collection, ''),
      );
      if (row.flag) head.append(chip(t('memory.results.flagged', {rule: row.flag}), 'warn'));
      const body = el('pre', 'memory-result-text', row.text);
      item.append(head, body);
      list.append(item);
    }
  }

  async function search() {
    const query = $('memQuery').value.trim();
    if (!query) { showError(t('memory.retrieve.empty')); return; }
    const collection = $('memQueryCollection').value;
    const limit = $('memQueryLimit').value || 6;
    state.results = await api(`/api/knowledge/search?q=${encodeURIComponent(query)}`
      + `&collection=${encodeURIComponent(collection)}&limit=${encodeURIComponent(limit)}`);
    paintResults();
  }

  /* ── context preview ────────────────────────────────────────────────── */
  function paintContext() {
    const body = state.context;
    const card = $('memContextCard');
    if (!body) { card.hidden = true; return; }
    card.hidden = false;
    const ratio = Math.min(1, (body.chars || 0) / Math.max(1, body.budget || 1));
    $('memBudgetBar').style.width = `${Math.round(ratio * 100)}%`;
    $('memBudgetBar').dataset.tone = ratio >= 1 ? 'warn' : 'ok';
    $('memContextMeta').textContent = t('memory.context.meta', {
      chars: body.chars, budget: body.budget, sections: body.sections?.length ?? 0});
    const documents = (body.sections || []).find((section) => section.id === 'documents');
    const retrieval = body.retrieval || {};
    $('memContextRetrieval').textContent = retrieval.ran
      ? t('memory.context.retrieved', {count: documents?.items?.length ?? 0})
      : (retrieval.enabled === false ? t('memory.context.retrievalOff') : t('memory.context.noRetrieval'));
    const facts = $('memContextFacts');
    facts.replaceChildren();
    for (const section of body.sections || []) {
      facts.append(el('span', 'memory-fact-term', t(`memory.section.${section.id}`)),
        el('span', 'memory-fact-value', t('memory.context.sectionValue', {
          items: section.items?.length ?? 0, chars: section.chars ?? 0})));
    }
    for (const row of body.notes?.flagged || []) {
      facts.append(el('span', 'memory-fact-term', t('memory.context.flagged')),
        el('span', 'memory-fact-value', `${row.path} · ${row.rule}`));
    }
    $('memContextText').textContent = body.text || t('memory.context.empty');
  }

  async function previewContext() {
    const task = $('memTask').value.trim();
    const top = $('memQueryLimit').value || 6;
    state.context = await api(`/api/knowledge/context?task=${encodeURIComponent(task)}&top_k=${top}`);
    paintContext();
    $('memContextCard').scrollIntoView({block: 'nearest'});
  }

  /* ── policy save ────────────────────────────────────────────────────── */
  function readPolicyForm() {
    const body = {
      memory: $('memSwitchMemory').checked,
      rag: $('memSwitchRag').checked,
      inject: $('memSwitchInject').checked,
      top_k: Number($('memTopK').value),
      max_chars: Number($('memMaxChars').value),
      chunk_chars: Number($('memChunkChars').value),
    };
    if (body.inject && !body.rag) throw Error(t('memory.policy.injectNeedsRag'));
    return body;
  }

  function askSavePolicy() {
    let wanted;
    try { wanted = readPolicyForm(); } catch (error) { showError(error); return; }
    openAction({
      title: t('memory.policy.saveTitle'),
      token: TOKENS.save,
      detail: t('memory.policy.saveDetail'),
      onConfirm: async (payload) => {
        const saved = await api('/api/knowledge/policy/save', {
          ...payload, ...wanted, revision: state.overview?.revision || ''});
        state.policy = saved;
        await loadOverview();
        await loadRetrieval();
        window.nexusToast?.(t('memory.policy.saved'), {tone: 'done'});
      },
    });
  }

  /* ── chip ───────────────────────────────────────────────────────────── */
  function paintChip() {
    const policy = state.overview?.policy;
    if (!policy) { $('memChip').textContent = '—'; return; }
    $('memChip').textContent = t('memory.chip', {
      rag: policy.rag ? t('memory.on') : t('memory.off'),
      inject: policy.inject ? t('memory.on') : t('memory.off'),
      memory: policy.memory ? t('memory.on') : t('memory.off')});
  }

  /* ── wiring ─────────────────────────────────────────────────────────── */
  for (const tab of TABS) {
    const button = $(`memTab${tab[0].toUpperCase()}${tab.slice(1)}`);
    if (button) button.onclick = () => switchTab(tab);
  }
  $('memRefresh').onclick = () => guard(async () => { await loadOverview(); switchTab(state.tab); });
  $('memOpenRun').onclick = () => gui.show('run');
  $('memPolicySave').onclick = askSavePolicy;
  $('memAdd').onclick = () => guard(askAdd);
  $('memPersonalAdd')?.addEventListener('click', () => guard(askPersonalAdd));
  $('memPersonalSearchBtn')?.addEventListener('click', () => guard(() => loadPersonal($('memPersonalSearch').value.trim())));
  $('memPersonalSearch')?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      guard(() => loadPersonal($('memPersonalSearch').value.trim()));
    }
  });
  $('memPersonalExport')?.addEventListener('click', () => guard(exportPersonalJson));
  $('memBuild').onclick = () => {
    const name = ($('memBuildName').value.trim() || 'docs').toLowerCase();
    if (!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(name)) { showError(t('memory.build.badName')); return; }
    const patterns = $('memBuildPatterns').value.split('\n').map((row) => row.trim()).filter(Boolean);
    if (!patterns.length) { showError(t('memory.build.noPatterns')); return; }
    askIndex(name, patterns);
  };
  $('memClean').onclick = askClean;
  $('memSearch').onclick = () => guard(search);
  $('memQuery').onkeydown = (event) => { if (event.key === 'Enter') { event.preventDefault(); guard(search); } };
  $('memPreview').onclick = () => guard(previewContext);
  $('memTask').onkeydown = (event) => { if (event.key === 'Enter') { event.preventDefault(); guard(previewContext); } };

  window.addEventListener('nexus:view', (event) => {
    if (event.detail === 'memory' && !state.overview) loadOverview().catch(showError);
  });

  i18n.onChange(() => {
    i18n.apply(root);
    i18n.apply(host);
    paintOverview();
    paintMemory();
    paintPersonal();
    paintCollections();
    paintDocuments();
    paintResults();
    paintContext();
    paintChip();
  });

  const initial = new URLSearchParams(location.search).get('tab');
  switchTab(TABS.includes(initial) ? initial : 'overview');

  window.nexusMemory = {
    root,
    tab: switchTab,
    load: loadOverview,
    state: () => ({tab: state.tab, rag: state.overview?.policy?.rag ?? null,
      inject: state.overview?.policy?.inject ?? null,
      memory: state.overview?.policy?.memory ?? null,
      collections: state.overview?.totals?.collections ?? null,
      chunks: state.overview?.totals?.chunks ?? null,
      results: state.results?.results?.length ?? null,
      contextChars: state.context?.chars ?? null}),
  };
})();
