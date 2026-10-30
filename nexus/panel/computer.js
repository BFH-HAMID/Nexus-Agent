/* Computer Control: what this machine lets the dashboard do, and how to stop it.

   Everything on this page is read from `/api/computer/*`. The studio store can
   only narrow the workspace policy (`permissions.yaml` + the adapter opt-ins):
   a capability the operator disabled there stays disabled here. Screen, apps and
   the VS Code bridge are probed and reported, never simulated. Every write,
   trash, restore and panic carries its own exact typed token. Opening this page
   starts nothing. */
(() => {
  'use strict';

  const gui = window.nexusGui;
  const i18n = window.nexusI18n;
  if (!gui || !i18n) return;
  const t = i18n.t;
  const $ = (id) => document.getElementById(id);

  const root = document.createElement('section');
  root.id = 'view-computer';
  root.className = 'gui-modules computer-page';
  root.dataset.guiView = 'computer';
  root.hidden = gui.view !== 'computer';
  root.innerHTML = `
    <header class="computer-head">
      <div>
        <p class="computer-eyebrow" data-i18n="computer.eyebrow">Computer Control</p>
        <h1 data-i18n="computer.title">What this machine allows</h1>
        <p class="computer-sub" data-i18n="computer.subtitle"></p>
      </div>
      <div class="computer-quick" role="group" aria-label="Computer actions">
        <button class="btn" type="button" id="ccRefresh"><span class="btn-label" data-i18n="computer.refresh"></span></button>
        <button class="btn ghost" type="button" id="ccSetup"><span class="btn-label" data-i18n="computer.linkSetup"></span></button>
        <span class="computer-stamp" id="ccUpdated" data-i18n="computer.never"></span>
      </div>
    </header>

    <div class="computer-toolbar">
      <div class="computer-tabs" role="tablist" aria-label="Computer Control sections">
        <button type="button" role="tab" id="ccTabOverview" data-cc-tab="overview" aria-selected="true" aria-controls="ccPanelOverview" data-i18n="computer.tabOverview"></button>
        <button type="button" role="tab" id="ccTabPermissions" data-cc-tab="permissions" aria-selected="false" aria-controls="ccPanelPermissions" data-i18n="computer.tabPermissions"></button>
        <button type="button" role="tab" id="ccTabFiles" data-cc-tab="files" aria-selected="false" aria-controls="ccPanelFiles" data-i18n="computer.tabFiles"></button>
        <button type="button" role="tab" id="ccTabBrowser" data-cc-tab="browser" aria-selected="false" aria-controls="ccPanelBrowser" data-i18n="computer.tabBrowser"></button>
        <button type="button" role="tab" id="ccTabPanic" data-cc-tab="panic" aria-selected="false" aria-controls="ccPanelPanic" data-i18n="computer.tabPanic"></button>
      </div>
      <span class="computer-chip" id="ccLatch" data-tone="ok" role="status" aria-live="polite">—</span>
    </div>

    <div class="computer-body">
      <section class="computer-panel" id="ccPanelOverview" role="tabpanel" aria-labelledby="ccTabOverview" data-cc-panel="overview">
        <div class="computer-grid" id="ccCapabilities"></div>
        <div class="computer-grid">
          <article class="computer-card">
            <h2 data-i18n="computer.workspacePolicy"></h2>
            <dl class="computer-facts" id="ccPolicy"></dl>
            <p class="computer-hint" data-i18n="computer.narrowing"></p>
          </article>
          <article class="computer-card">
            <h2 data-i18n="computer.notChecked"></h2>
            <ul class="computer-notes" id="ccNotes"></ul>
          </article>
        </div>
      </section>

      <section class="computer-panel" id="ccPanelPermissions" role="tabpanel" aria-labelledby="ccTabPermissions" data-cc-panel="permissions" hidden>
        <div class="computer-grid">
          <article class="computer-card">
            <div class="computer-cardhead">
              <h2 data-i18n="computer.permissions"></h2>
              <span class="computer-chip" id="ccSettingsRevision">—</span>
            </div>
            <label class="computer-field"><span data-i18n="computer.profile"></span>
              <select id="ccProfile"></select></label>
            <p class="computer-hint" id="ccProfileHint"></p>
            <div class="computer-levels" id="ccLevels"></div>
            <label class="computer-field"><span data-i18n="computer.roots"></span>
              <textarea id="ccRoots" rows="3" spellcheck="false" data-i18n-ph="computer.rootsHint"></textarea></label>
            <label class="computer-field"><span data-i18n="computer.origins"></span>
              <textarea id="ccOrigins" rows="3" spellcheck="false" data-i18n-ph="computer.originsHint"></textarea></label>
            <div class="computer-actions">
              <button class="btn primary" type="button" id="ccSave"><span class="btn-label" data-i18n="computer.save"></span></button>
              <span class="computer-status" id="ccSaveStatus" role="status"></span>
            </div>
            <p class="computer-hint" data-i18n="computer.saveHint"></p>
          </article>
          <article class="computer-card">
            <h2 data-i18n="computer.policyDefault"></h2>
            <dl class="computer-facts" id="ccPolicyDetail"></dl>
            <p class="computer-hint" data-i18n="computer.policyHint"></p>
            <button class="btn ghost" type="button" id="ccOpenSetup"><span class="btn-label" data-i18n="computer.linkSetup"></span></button>
          </article>
        </div>
      </section>

      <section class="computer-panel" id="ccPanelFiles" role="tabpanel" aria-labelledby="ccTabFiles" data-cc-panel="files" hidden>
        <div class="computer-grid computer-grid-files">
          <article class="computer-card">
            <div class="computer-cardhead">
              <h2 data-i18n="computer.files"></h2>
              <span class="computer-chip" id="ccScopeChip">—</span>
            </div>
            <div class="computer-pathrow">
              <label class="computer-field computer-grow"><span class="sr-only" data-i18n="computer.path"></span>
                <input id="ccPath" autocomplete="off" spellcheck="false" maxlength="400" value="."></label>
              <button class="btn" type="button" id="ccOpen"><span class="btn-label" data-i18n="computer.open"></span></button>
            </div>
            <ul class="computer-tree" id="ccTree" role="list"></ul>
            <p class="computer-hint" id="ccTreeNote"></p>
            <h3 data-i18n="computer.snapshots"></h3>
            <ul class="computer-list" id="ccSnapshots"></ul>
            <h3 data-i18n="computer.trash"></h3>
            <ul class="computer-list" id="ccTrash"></ul>
          </article>
          <article class="computer-card">
            <div class="computer-cardhead">
              <h2 id="ccPreviewName" data-i18n="computer.preview"></h2>
              <span class="computer-chip" id="ccPreviewMeta">—</span>
            </div>
            <textarea class="computer-editor" id="ccText" spellcheck="false" aria-labelledby="ccPreviewName" readonly></textarea>
            <div class="computer-actions">
              <button class="btn primary" type="button" id="ccWrite"><span class="btn-label" data-i18n="computer.write"></span></button>
              <button class="btn danger" type="button" id="ccTrashButton"><span class="btn-label" data-i18n="computer.trashAction"></span></button>
              <span class="computer-status" id="ccFileStatus" role="status"></span>
            </div>
            <p class="computer-hint" data-i18n="computer.writeHint"></p>
          </article>
        </div>
      </section>

      <section class="computer-panel" id="ccPanelBrowser" role="tabpanel" aria-labelledby="ccTabBrowser" data-cc-panel="browser" hidden>
        <div class="computer-grid">
          <article class="computer-card">
            <h2 data-i18n="computer.browser"></h2>
            <p class="computer-hint" id="ccBrowserReason"></p>
            <ul class="computer-list" id="ccOriginsList"></ul>
            <div class="computer-pathrow">
              <label class="computer-field computer-grow"><span class="sr-only" data-i18n="computer.url"></span>
                <input id="ccUrl" type="url" autocomplete="off" spellcheck="false" maxlength="2048" data-i18n-ph="computer.urlHint"></label>
              <button class="btn primary" type="button" id="ccInspect"><span class="btn-label" data-i18n="computer.inspect"></span></button>
            </div>
            <p class="computer-hint" data-i18n="computer.browserHint"></p>
          </article>
          <article class="computer-card">
            <div class="computer-cardhead">
              <h2 id="ccBrowserTitle" data-i18n="computer.browserResult"></h2>
              <span class="computer-chip" id="ccBrowserMeta">—</span>
            </div>
            <pre class="computer-browserout" id="ccBrowserOut" tabindex="0"></pre>
            <p class="computer-hint" data-i18n="computer.untrusted"></p>
            <span class="computer-status" id="ccBrowserStatus" role="status"></span>
          </article>
        </div>
      </section>

      <section class="computer-panel" id="ccPanelPanic" role="tabpanel" aria-labelledby="ccTabPanic" data-cc-panel="panic" hidden>
        <article class="computer-card computer-panic">
          <h2 data-i18n="computer.panicTitle"></h2>
          <p class="computer-hint" data-i18n="computer.panicLead"></p>
          <dl class="computer-facts" id="ccPanicFacts"></dl>
          <label class="computer-field"><span data-i18n="computer.panicConfirm"></span>
            <input id="ccPanicWord" autocomplete="off" spellcheck="false" placeholder="PANIC STOP"></label>
          <div class="computer-actions">
            <button class="btn danger" type="button" id="ccPanic" disabled><span class="btn-label" data-i18n="computer.panic"></span></button>
            <span class="computer-status" id="ccPanicStatus" role="status"></span>
          </div>
          <hr class="computer-rule">
          <label class="computer-field"><span data-i18n="computer.resumeConfirm"></span>
            <input id="ccResumeWord" autocomplete="off" spellcheck="false" placeholder="RESUME COMPUTER"></label>
          <label class="computer-check"><input type="checkbox" id="ccResumeQueue">
            <span data-i18n="computer.resumeQueue"></span></label>
          <div class="computer-actions">
            <button class="btn" type="button" id="ccResume" disabled><span class="btn-label" data-i18n="computer.resume"></span></button>
            <span class="computer-status" id="ccResumeStatus" role="status"></span>
          </div>
          <p class="computer-hint" data-i18n="computer.panicNote"></p>
        </article>
      </section>

      <div class="computer-confirm" id="ccConfirmRow" hidden>
        <p class="computer-hint" id="ccConfirmLabel"></p>
        <div class="computer-pathrow">
          <label class="computer-field computer-grow"><span class="sr-only" data-i18n="computer.confirmPrompt"></span>
            <input id="ccConfirm" autocomplete="off" spellcheck="false"></label>
          <button class="btn primary" type="button" id="ccConfirmGo"><span class="btn-label" data-i18n="computer.confirmGo"></span></button>
          <button class="btn ghost" type="button" id="ccConfirmCancel"><span class="btn-label" data-i18n="computer.cancel"></span></button>
        </div>
      </div>
    </div>

    <footer class="computer-foot">
      <span id="ccSource" data-i18n="computer.source"></span>
      <span id="ccStats">—</span>
    </footer>`;

  (document.querySelector('main.deck') || document.body).after(root);

  const state = {
    tab: 'overview', data: null, policy: null, settings: null, levels: {}, profile: 'locked',
    entries: [], open: null, trash: [], snapshots: [], browser: null, pending: null, busy: false,
  };

  const defaultRoot = () => (state.settings?.scopes.roots?.[0] || '.');
  let openSeq = 0;
  const clock = (value) => {
    if (!value) return '—';
    const date = new Date(value * 1000);
    return Number.isNaN(date.getTime()) ? '—'
      : date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  };
  const message = (error) => error?.payload?.error?.message || error?.message || String(error);
  const set = (node, text, tone) => {
    if (!node) return;
    node.textContent = text;
    if (tone !== undefined) node.dataset.tone = tone;
  };
  function flash(node, text, tone = '') {
    set(node, text, tone);
    if (text) window.setTimeout(() => { if (node.textContent === text) set(node, '', ''); }, 6000);
  }
  function chip(text, tone) {
    const span = document.createElement('span');
    span.className = 'computer-chip';
    span.textContent = text;
    if (tone) span.dataset.tone = tone;
    return span;
  }
  const levelLabel = (value) => t(`computer.level.${value}`);
  const levelTone = (value) => ({off: 'muted', read: 'accent', ask: 'warn', auto: 'warn', on: 'ok', unavailable: 'muted', unknown: 'muted'}[value] || 'muted');

  /* ── data ─────────────────────────────────────────────────────────────── */
  async function load() {
    root.dataset.busy = 'true';
    try {
      const data = await gui.api('/api/computer/state').then((r) => r.data);
      state.data = data;
      state.policy = data.policy;
      state.settings = data.store.settings;
      state.levels = {...data.store.settings.levels};
      state.profile = data.store.settings.profile;
      paintAll();
      set($('ccUpdated'), t('computer.updated', {time: clock(data.observed_at)}));
      if (state.tab === 'files') await loadFiles();
      if (state.tab === 'browser' && !state.browser) await loadBrowser();
    } catch (error) {
      set($('ccUpdated'), message(error), 'error');
      if (!state.data) set($('ccLatch'), t('computer.unavailable'), 'error');
    } finally {
      root.dataset.busy = 'false';
    }
  }

  async function loadFiles() {
    const cap = (state.data?.capabilities || []).find((row) => row.id === 'files');
    if (!cap || cap.level === 'off') {
      state.trash = [];
      state.snapshots = [];
      paintFiles();
      return;
    }
    const [trash, snapshots] = await Promise.all([
      gui.api('/api/computer/trash').then((r) => r.data).catch(() => ({entries: []})),
      gui.api('/api/computer/snapshots').then((r) => r.data).catch(() => ({items: []})),
    ]);
    state.trash = trash.entries || [];
    state.snapshots = snapshots.items || [];
    paintFiles();
  }

  async function loadBrowser() {
    try {
      state.browser = await gui.api('/api/computer/browser').then((r) => r.data);
      paintBrowser();
    } catch (error) {
      state.browser = null;
      set($('ccBrowserReason'), message(error), 'error');
    }
  }

  function paintAll() {
    paintLatch();
    paintCapabilities();
    paintPolicy();
    paintPermissions();
    paintFiles();
    paintBrowser();
    paintPanic();
    set($('ccStats'), t('computer.stats', {
      caps: state.data.capabilities.length,
      owned: state.data.capabilities.filter((row) => row.owned).length,
    }));
  }

  function paintLatch() {
    const stopped = Boolean(state.data?.latch?.stopped);
    const node = $('ccLatch');
    node.textContent = stopped ? t('computer.latched') : t('computer.clear');
    node.dataset.tone = stopped ? 'error' : 'ok';
  }

  const capLabel = (id) => t(`computer.cap.${id}`);

  function paintCapabilities() {
    const host = $('ccCapabilities');
    host.replaceChildren();
    for (const row of state.data?.capabilities || []) {
      const card = document.createElement('article');
      card.className = 'computer-card computer-cap';
      card.dataset.cap = row.id;
      const head = document.createElement('div');
      head.className = 'computer-cardhead';
      const title = document.createElement('h2');
      title.textContent = capLabel(row.id);
      head.append(title, chip(levelLabel(row.level), levelTone(row.level)));
      const reason = document.createElement('p');
      reason.className = 'computer-hint';
      reason.textContent = row.reason;
      const meta = document.createElement('dl');
      meta.className = 'computer-facts';
      for (const [key, value] of [
        [t('computer.managedBy'), row.managed_by],
        [t('computer.ceiling'), row.ceiling === 'n/a' ? t('computer.na') : levelLabel(row.ceiling)],
        [t('computer.available'), row.available ? t('computer.yes') : t('computer.no')],
      ]) {
        const dt = document.createElement('dt');
        dt.textContent = key;
        const dd = document.createElement('dd');
        dd.textContent = value;
        meta.append(dt, dd);
      }
      card.append(head, reason, meta);
      if (row.auto_note) {
        const note = document.createElement('p');
        note.className = 'computer-hint';
        note.textContent = row.auto_note;
        card.append(note);
      }
      host.append(card);
    }
  }

  function paintPolicy() {
    const policy = state.data?.policy || {};
    const host = $('ccPolicy');
    host.replaceChildren();
    for (const [key, value] of [
      [t('computer.policyDefault'), policy.default || t('computer.unknown')],
      [t('computer.policyBrowser'), policy.computer?.browser ? t('computer.on') : t('computer.off')],
      [t('computer.policyOffice'), policy.computer?.office ? t('computer.on') : t('computer.off')],
      [t('computer.policyEditor'), policy.computer?.editor ? t('computer.on') : t('computer.off')],
      [t('computer.policyShell'), policy.computer?.shell_backend || t('computer.unknown')],
      [t('computer.policyLocal'), state.data?.local_only ? t('computer.on') : t('computer.off')],
    ]) {
      const dt = document.createElement('dt');
      dt.textContent = key;
      const dd = document.createElement('dd');
      dd.textContent = value;
      host.append(dt, dd);
    }
    const notes = $('ccNotes');
    notes.replaceChildren();
    for (const text of state.data?.notes || []) {
      const li = document.createElement('li');
      li.textContent = text;
      notes.append(li);
    }
    if (!state.data?.policy?.readable) {
      const li = document.createElement('li');
      li.textContent = t('computer.policyUnreadable');
      notes.append(li);
    }
    paintPolicyDetail();
  }

  function paintPolicyDetail() {
    const host = $('ccPolicyDetail');
    if (!host) return;
    host.replaceChildren();
    const rows = [
      [t('computer.policyDefault'), state.data?.policy?.default || t('computer.unknown')],
      [t('computer.policyBrowser'), state.data?.policy?.computer?.browser ? t('computer.on') : t('computer.off')],
      [t('computer.policyOffice'), state.data?.policy?.computer?.office ? t('computer.on') : t('computer.off')],
      [t('computer.policyEditor'), state.data?.policy?.computer?.editor ? t('computer.on') : t('computer.off')],
      [t('computer.policyShell'), state.data?.policy?.computer?.shell_backend || t('computer.unknown')],
    ];
    for (const [key, value] of rows) {
      const dt = document.createElement('dt');
      dt.textContent = key;
      const dd = document.createElement('dd');
      dd.textContent = value;
      host.append(dt, dd);
    }
  }

  function paintPermissions() {
    if (!state.data) return;
    const select = $('ccProfile');
    select.replaceChildren();
    for (const name of Object.keys(state.data.profiles)) {
      const option = document.createElement('option');
      option.value = name;
      option.textContent = t(`computer.profile.${name}`);
      select.append(option);
    }
    select.value = state.profile;
    set($('ccProfileHint'), t(`computer.profileHint.${state.profile}`));

    const host = $('ccLevels');
    host.replaceChildren();
    for (const row of state.data.capabilities.filter((item) => item.owned)) {
      const wrap = document.createElement('div');
      wrap.className = 'computer-level';
      const label = document.createElement('span');
      label.textContent = capLabel(row.id);
      const choice = document.createElement('select');
      choice.id = `ccLevel-${row.id}`;
      choice.dataset.cap = row.id;
      for (const level of row.levels) {
        const option = document.createElement('option');
        option.value = level;
        option.textContent = levelLabel(level);
        choice.append(option);
      }
      choice.value = state.levels[row.id] || 'off';
      choice.onchange = () => {
        state.levels[row.id] = choice.value;
        set($('ccSaveStatus'), t('computer.unsaved'), 'warn');
      };
      const ceiling = document.createElement('small');
      ceiling.textContent = t('computer.ceilingIs', {level: levelLabel(row.ceiling)});
      wrap.append(label, choice, ceiling);
      if (row.auto_note) {
        const note = document.createElement('p');
        note.className = 'computer-hint';
        note.textContent = row.auto_note;
        wrap.append(note);
      }
      host.append(wrap);
    }
    $('ccRoots').value = (state.settings?.scopes.roots || ['.']).join('\n');
    $('ccOrigins').value = (state.settings?.scopes.origins || []).join('\n');
    set($('ccSettingsRevision'), state.data.store.revision || '—');
  }

  function paintFiles() {
    const tree = $('ccTree');
    tree.replaceChildren();
    const cap = (state.data?.capabilities || []).find((row) => row.id === 'files');
    const filesOn = cap && cap.level !== 'off';
    $('ccOpen').disabled = !filesOn;
    $('ccPath').disabled = !filesOn;
    $('ccWrite').disabled = true;
    $('ccTrashButton').disabled = true;
    if (!filesOn) {
      set($('ccTreeNote'), t('computer.filesOff'));
      set($('ccScopeChip'), t('computer.level.off'), 'muted');
      return;
    }
    for (const entry of state.entries) {
      const li = document.createElement('li');
      li.className = 'computer-tree-row';
      li.dataset.kind = entry.kind;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'computer-tree-btn';
      button.style.paddingLeft = `${8 + (entry.depth - 1) * 14}px`;
      const name = document.createElement('span');
      name.className = 'computer-tree-name';
      name.textContent = entry.kind === 'dir' ? `${entry.name}/` : entry.name;
      const size = document.createElement('span');
      size.className = 'computer-tree-size';
      size.textContent = entry.kind === 'dir' ? '' : `${Math.max(1, Math.round(entry.size / 1024))} KB`;
      button.append(name, size);
      button.onclick = () => {
        if (entry.kind === 'dir') openPath(entry.path);
        else previewFile(entry.path);
      };
      button.setAttribute('aria-current', String(state.open?.path === entry.path));
      li.append(button);
      tree.append(li);
    }
    set($('ccTreeNote'), state.entries.length
      ? t('computer.treeNote', {count: state.entries.length, root: state.root || $('ccPath').value || '.'})
      : t('computer.treeEmpty'));
    set($('ccScopeChip'), t('computer.scopeChip', {roots: (state.settings?.scopes.roots || ['.']).join(', ')}), 'muted');

    const snapshots = $('ccSnapshots');
    snapshots.replaceChildren();
    for (const item of state.snapshots.slice(0, 8)) {
      const li = document.createElement('li');
      li.className = 'computer-list-row';
      const text = document.createElement('span');
      text.textContent = `${item.path} · ${clock(item.created)} · ${item.status}`;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn ghost';
      button.textContent = t('computer.undo');
      button.disabled = item.status !== 'applied';
      button.onclick = () => confirmAction(`RESTORE ${item.id}`, t('computer.undoHint'), async (token) => {
        const body = (await gui.api('/api/computer/file/undo', {id: item.id, confirmation: token})).data;
        flash($('ccFileStatus'), t('computer.undone', {path: body.restored.path}), 'ok');
        await load();
        await loadFiles();
      });
      li.append(text, button);
      snapshots.append(li);
    }
    if (!state.snapshots.length) snapshots.append(emptyRow(t('computer.snapshotEmpty')));

    const trash = $('ccTrash');
    trash.replaceChildren();
    for (const item of state.trash.slice(0, 8)) {
      const li = document.createElement('li');
      li.className = 'computer-list-row';
      const text = document.createElement('span');
      text.textContent = `${item.path} · ${clock(item.at)}`;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn ghost';
      button.textContent = t('computer.restore');
      button.onclick = () => confirmAction(`RESTORE ${item.id}`, t('computer.restoreHint'), async (token) => {
        const body = (await gui.api('/api/computer/trash/restore', {id: item.id, confirmation: token})).data;
        flash($('ccFileStatus'), t('computer.restored', {path: body.restored}), 'ok');
        await load();
        await loadFiles();
        await openPath($('ccPath').value || defaultRoot());
      });
      li.append(text, button);
      trash.append(li);
    }
    if (!state.trash.length) trash.append(emptyRow(t('computer.trashEmpty')));
  }

  function emptyRow(text) {
    const li = document.createElement('li');
    li.className = 'computer-list-empty';
    li.textContent = text;
    return li;
  }

  function paintBrowser() {
    const view = state.browser;
    if (!view) return;
    set($('ccBrowserReason'), view.available ? t('computer.browserReady', {level: levelLabel(view.level)})
      : `${t('computer.unavailable')}: ${view.reason}`);
    const list = $('ccOriginsList');
    list.replaceChildren();
    for (const origin of view.origins) list.append(emptyRow(origin));
    if (!view.origins.length) list.append(emptyRow(t('computer.originsEmpty')));
    $('ccInspect').disabled = !view.available;
  }

  function paintPanic() {
    const stopped = Boolean(state.data?.latch?.stopped);
    const facts = $('ccPanicFacts');
    facts.replaceChildren();
    for (const [key, value] of [
      [t('computer.latch'), stopped ? t('computer.latched') : t('computer.clear')],
      [t('computer.queue'), state.data?.queue?.paused ? t('computer.paused') : t('computer.running')],
      [t('computer.sessions'), String(window.nexusGui?.state?.sessions ?? '—')],
    ]) {
      const dt = document.createElement('dt');
      dt.textContent = key;
      const dd = document.createElement('dd');
      dd.textContent = value;
      facts.append(dt, dd);
    }
    $('ccResume').disabled = !stopped || $('ccResumeWord').value.trim() !== 'RESUME COMPUTER';
    $('ccPanic').disabled = stopped || $('ccPanicWord').value.trim() !== 'PANIC STOP';
  }

  /* ── actions ──────────────────────────────────────────────────────────── */
  function confirmAction(token, hint, run) {
    state.pending = {token, run};
    $('ccConfirmRow').hidden = false;
    $('ccConfirmLabel').textContent = `${hint} ${t('computer.confirmPrompt')} ${token}`;
    $('ccConfirm').value = '';
    $('ccConfirm').placeholder = token;
    $('ccConfirm').focus();
  }
  function closeConfirm() {
    state.pending = null;
    $('ccConfirmRow').hidden = true;
    $('ccConfirm').value = '';
  }

  async function runPending() {
    const pending = state.pending;
    if (!pending) return;
    const typed = $('ccConfirm').value.trim();
    if (typed !== pending.token) {
      flash($('ccSaveStatus'), t('computer.wrongToken'), 'error');
      return;
    }
    closeConfirm();
    try {
      await pending.run(typed);
    } catch (error) {
      flash($('ccSaveStatus'), message(error), 'error');
    }
  }

  async function openPath(path) {
    // A slow auto-open of the default folder must never overwrite a folder the
    // operator asked for in the meantime, so only the newest request may paint.
    const seq = ++openSeq;
    const typed = $('ccPath').value;
    try {
      const body = (await gui.api(`/api/computer/files?path=${encodeURIComponent(path)}&depth=1`)).data;
      if (seq !== openSeq) return;
      state.entries = body.entries;
      state.root = body.root;
      // Never overwrite a folder the operator typed while this request was running.
      if ($('ccPath').value === typed) $('ccPath').value = body.root;
      paintFiles();
    } catch (error) {
      if (seq === openSeq) flash($('ccFileStatus'), message(error), 'error');
    }
  }

  async function previewFile(path) {
    try {
      const body = (await gui.api('/api/computer/file/preview', {path})).data;
      state.open = body;
      $('ccPreviewName').textContent = body.path;
      $('ccPreviewMeta').textContent = body.binary ? t('computer.binary')
        : `${Math.max(1, Math.round(body.size / 1024))} KB`;
      paintFiles();
      $('ccText').value = body.binary ? '' : body.text;
      $('ccText').readOnly = body.binary;
      $('ccWrite').disabled = body.binary;
      $('ccTrashButton').disabled = false;
    } catch (error) {
      flash($('ccFileStatus'), message(error), 'error');
    }
  }

  async function saveSettings() {
    const roots = $('ccRoots').value.split('\n').map((line) => line.trim()).filter(Boolean);
    const origins = $('ccOrigins').value.split('\n').map((line) => line.trim()).filter(Boolean);
    confirmAction('SAVE COMPUTER POLICY', t('computer.saveConfirm'), async (token) => {
      const body = (await gui.api('/api/computer/settings', {
        profile: $('ccProfile').value, levels: state.levels,
        scopes: {roots: roots.length ? roots : ['.'], origins}, revision: state.data.store.revision,
        confirmation: token,
      })).data;
      state.settings = body.settings;
      flash($('ccSaveStatus'), t('computer.saveDone', {revision: body.revision}), 'ok');
      await load();
    });
  }

  async function writeFile() {
    if (!state.open) return;
    const path = state.open.path;
    const text = $('ccText').value;
    confirmAction(`WRITE ${path}`, t('computer.writeHint'), async (token) => {
      try {
        const body = (await gui.api('/api/computer/file/write', {
          path, text, confirmation: token, revision: state.open.revision,
        })).data;
        flash($('ccFileStatus'), t('computer.written', {path: body.saved, bytes: body.bytes}), 'ok');
        await previewFile(path);
        await loadFiles();
      } catch (error) {
        flash($('ccFileStatus'), message(error), 'error');
      }
    });
  }

  async function trashFile() {
    if (!state.open) return;
    const path = state.open.path;
    confirmAction(`TRASH ${path}`, t('computer.trashHint'), async (token) => {
      try {
        const body = (await gui.api('/api/computer/file/trash', {path, confirmation: token})).data;
        flash($('ccFileStatus'), t('computer.trashed', {path: body.path, id: body.trashed}), 'ok');
        state.open = null;
        $('ccText').value = '';
        await openPath($('ccPath').value || defaultRoot());
        await loadFiles();
      } catch (error) {
        flash($('ccFileStatus'), message(error), 'error');
      }
    });
  }

  async function inspect() {
    const url = $('ccUrl').value.trim();
    if (!url) return;
    $('ccInspect').disabled = true;
    set($('ccBrowserStatus'), t('computer.loading'), '');
    try {
      const body = (await gui.api('/api/computer/browser/open', {url})).data;
      $('ccBrowserTitle').textContent = body.title || body.url;
      $('ccBrowserMeta').textContent = `${body.provider_calls} · ${clock(body.observed_at)}`;
      $('ccBrowserOut').textContent = `${body.url}\n\n${body.text}${body.truncated ? '\n…' : ''}`;
      set($('ccBrowserStatus'), t('computer.inspectDone'), 'ok');
    } catch (error) {
      set($('ccBrowserStatus'), message(error), 'error');
    } finally {
      $('ccInspect').disabled = !state.browser?.available;
    }
  }

  async function panic() {
    if ($('ccPanicWord').value.trim() !== 'PANIC STOP') return;
    set($('ccPanicStatus'), t('computer.panicSending'), '');
    try {
      const body = (await gui.api('/api/computer/panic', {confirmation: 'PANIC STOP'})).data;
      set($('ccPanicStatus'), t('computer.panicDone', {sessions: body.sessions_cancelled.length}), 'ok');
      $('ccPanicWord').value = '';
      await load();
    } catch (error) {
      set($('ccPanicStatus'), message(error), 'error');
    }
  }

  async function resume() {
    if ($('ccResumeWord').value.trim() !== 'RESUME COMPUTER') return;
    set($('ccResumeStatus'), t('computer.resumeSending'), '');
    try {
      await gui.api('/api/computer/resume', {confirmation: 'RESUME COMPUTER',
        resume_queue: Boolean($('ccResumeQueue')?.checked)});
      set($('ccResumeStatus'), t('computer.resumeDone'), 'ok');
      $('ccResumeWord').value = '';
      await load();
    } catch (error) {
      set($('ccResumeStatus'), message(error), 'error');
    }
  }

  /* ── tabs ─────────────────────────────────────────────────────────────── */
  function switchTab(name) {
    state.tab = name;
    root.querySelectorAll('[data-cc-tab]').forEach((node) => {
      node.setAttribute('aria-selected', String(node.dataset.ccTab === name));
    });
    root.querySelectorAll('[data-cc-panel]').forEach((node) => { node.hidden = node.dataset.ccPanel !== name; });
    const cap = (state.data?.capabilities || []).find((row) => row.id === 'files');
    if (name === 'files') {
      loadFiles().catch(() => {});
      if (cap && cap.level !== 'off' && !state.entries.length) openPath(defaultRoot()).catch(() => {});
    }
    if (name === 'browser') loadBrowser().catch(() => {});
  }

  root.querySelectorAll('[data-cc-tab]').forEach((node) => { node.onclick = () => switchTab(node.dataset.ccTab); });
  root.querySelector('[role="tablist"]').addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...root.querySelectorAll('[data-cc-tab]')];
    const index = tabs.findIndex((tab) => tab.dataset.ccTab === state.tab);
    const next = event.key === 'Home' ? tabs[0]
      : event.key === 'End' ? tabs[tabs.length - 1]
        : tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
    next.focus();
    switchTab(next.dataset.ccTab);
  });

  /* ── wiring ───────────────────────────────────────────────────────────── */
  $('ccRefresh').onclick = () => load();
  $('ccSetup').onclick = () => $('ccOpenSetup').click();
  $('ccOpenSetup').onclick = () => {
    $('wbOpen')?.click();
    const button = [...document.querySelectorAll('#wbNav button')].find((node) => /desktop/i.test(node.dataset.pane || ''));
    button?.click();
  };
  $('ccOpen').onclick = () => openPath($('ccPath').value.trim() || '.');
  $('ccPath').onkeydown = (event) => { if (event.key === 'Enter') { event.preventDefault(); openPath($('ccPath').value.trim() || '.'); } };
  $('ccWrite').onclick = writeFile;
  $('ccTrashButton').onclick = trashFile;
  $('ccSave').onclick = saveSettings;
  $('ccInspect').onclick = inspect;
  $('ccConfirmGo').onclick = runPending;
  $('ccConfirmCancel').onclick = closeConfirm;
  $('ccConfirm').onkeydown = (event) => { if (event.key === 'Enter') { event.preventDefault(); runPending(); } };
  $('ccPanicWord').oninput = () => { $('ccPanic').disabled = $('ccPanicWord').value.trim() !== 'PANIC STOP'; };
  $('ccPanic').onclick = panic;
  $('ccResumeWord').oninput = () => {
    $('ccResume').disabled = $('ccResumeWord').value.trim() !== 'RESUME COMPUTER' || !state.data?.latch?.stopped;
  };
  $('ccResume').onclick = resume;
  $('ccProfile').onchange = () => {
    const chosen = $('ccProfile').value;
    state.profile = chosen;
    state.levels = {...(state.data?.profiles?.[chosen] || state.levels)};
    paintPermissions();
    set($('ccSaveStatus'), t('computer.unsaved'), 'warn');
  };

  /* ── lifecycle ────────────────────────────────────────────────────────── */
  let loaded = false;
  async function boot() {
    if (loaded) return;
    loaded = true;
    await load();
  }
  window.addEventListener('nexus:view', (event) => {
    if (event.detail === 'computer') boot().catch((error) => set($('ccUpdated'), message(error)));
  });
  i18n.onChange(() => {
    i18n.apply(root);
    if (state.data) paintAll();
    if (state.data) set($('ccUpdated'), t('computer.updated', {time: clock(state.data.observed_at)}));
  });
  if (gui.view === 'computer') boot().catch(() => {});

  window.nexusComputer = {
    load, root, tab: switchTab,
    state: () => ({...state, entries: state.entries.length}),
    openPath, previewFile: (path) => previewFile(path),
    confirmAction, runPending,
  };
})();
