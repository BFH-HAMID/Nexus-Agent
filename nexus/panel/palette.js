/* Global command palette: Ctrl+K and Ctrl+Shift+P.

   The index is one small projection from /api/palette (roles, agents,
   pipelines) plus pages from the section registry, live tasks from /api/tasks
   and lazily loaded past runs, workflows and changed files. Nothing here
   executes work: every action either navigates, opens an existing pane, or
   opens a surface where the usual approval gate applies. */
(() => {
  'use strict';

  const gui = window.nexusGui;
  const i18n = window.nexusI18n;
  if (!gui || !i18n) return;
  /* The section registry normally comes from nav.js. If that script could not
     run (older webview, hostile storage, a matchMedia stub) the palette still
     works on the four built-in views instead of disappearing. */
  const FALLBACK = [
    {id: 'mission', group: 'operate', icon: 'i-radar', label: 'nav.mission', view: 'mission'},
    {id: 'run', group: 'operate', icon: 'i-chat', label: 'nav.run', view: 'run'},
    {id: 'approvals', group: 'operate', icon: 'i-shield', label: 'nav.approvals', view: 'operations', anchor: 'reviews'},
    {id: 'world', group: 'configure', icon: 'i-globe', label: 'nav.world', view: 'world'},
    {id: 'controls', group: 'configure', icon: 'i-grid', label: 'nav.controls', view: 'control'},
  ];
  const nav = window.nexusNav || {
    sections: FALLBACK,
    go: (id) => {
      const section = FALLBACK.find((row) => row.id === id);
      if (!section) return false;
      gui.show(section.view);
      if (section.anchor) window.setTimeout(() => document.getElementById(section.anchor)?.scrollIntoView({block: 'center'}), 80);
      return true;
    },
  };
  const t = i18n.t;
  const $ = (id) => document.getElementById(id);
  const dialog = $('palette');
  const input = $('paletteInput');
  const list = $('paletteList');
  const hint = $('paletteHint');
  const counter = $('paletteCount');
  if (!dialog || !input || !list) return;

  const RECENTS = 'nexus.ui.palette.recents';
  const PINS = 'nexus.ui.palette.pins';
  const readStore = (name) => {
    try {
      const value = JSON.parse(localStorage.getItem(name) || '[]');
      return Array.isArray(value) ? value.filter((item) => typeof item === 'string').slice(0, 12) : [];
    } catch { return []; }
  };
  const writeStore = (name, value) => {
    try { localStorage.setItem(name, JSON.stringify(value.slice(0, 12))); } catch { /* in-session only */ }
  };

  const data = {loaded: false, loading: false, failed: '', roles: [], agents: [], pipelines: [], tasks: [],
    runs: null, workflows: null, files: null, builtAt: 0};
  const state = {items: [], filtered: [], active: 0, lastMs: 0, query: ''};

  const ICONS = {page: 'i-layers', role: 'i-users', agent: 'i-cpu', task: 'i-chat', run: 'i-refresh',
    action: 'i-play', file: 'i-folder', workflow: 'i-workflow', setting: 'i-sliders', control: 'i-grid'};

  /* ── scoring: substring first, then ordered-subsequence ────────────── */
  function score(query, text) {
    if (!query) return 1;
    const hay = String(text || '').toLowerCase();
    if (!hay) return -1;
    const at = hay.indexOf(query);
    if (at >= 0) {
      let value = 1000 - at * 4 - Math.min(120, hay.length - query.length);
      if (at === 0) value += 240;
      if (at > 0 && hay[at - 1] === ' ') value += 90;
      return value;
    }
    let cursor = 0;
    let gaps = 0;
    let boundary = 0;
    for (const char of query) {
      const found = hay.indexOf(char, cursor);
      if (found < 0) return -1;
      if (found > 0 && /[\s\-_/.]/.test(hay[found - 1])) boundary += 12;
      gaps += found - cursor;
      cursor = found + 1;
    }
    return 220 + boundary - gaps - Math.min(80, hay.length * 0.05);
  }

  function best(query, record) {
    const label = score(query, record.label);
    const detail = record.detail ? score(query, record.detail) * 0.55 : -1;
    const extra = record.extra ? score(query, record.extra) * 0.45 : -1;
    return Math.max(label * 1.15, detail, extra);
  }

  /* ── pages: every section plus the dialogs and panes ───────────────── */
  function pageItems() {
    const pages = nav.sections.map((section) => ({
      key: 'page:' + section.id, kind: 'page', label: t(section.label), detail: section.detail || '',
      icon: section.icon, section: section.id,
      run: () => nav.go(section.id),
      panel: section.run ? () => section.run() : null,
    }));
    pages.push(
      {key: 'page:workbench', kind: 'page', label: 'Workbench', detail: 'models · policy · history · queue · files', icon: 'i-terminal',
        run: () => $('wbOpen')?.click(), panel: () => $('wbOpen')?.click()},
      {key: 'page:studio', kind: 'page', label: 'Studio', detail: 'sessions · review · evidence · workflows', icon: 'i-layers',
        run: () => $('studioOpen')?.click(), panel: () => $('studioOpen')?.click()},
      {key: 'page:keys', kind: 'setting', label: 'API keys', detail: 'provider credentials', icon: 'i-key',
        run: () => { nav.go('run'); window.setTimeout(() => $('keyList')?.scrollIntoView({block: 'center'}), 120); },
        panel: () => $('guiSettings')?.click()},
    );
    return pages;
  }

  function actions() {
    return [
      {key: 'action:compose', kind: 'action', label: t('mission.compose'), detail: 'run · composer', icon: 'i-chat',
        run: () => { nav.go('run'); window.setTimeout(() => $('taskInput')?.focus(), 120); }},
      {key: 'action:approvals', kind: 'action', label: t('mission.review'), detail: 'operations', icon: 'i-shield',
        run: () => nav.go('approvals')},
      {key: 'action:controls', kind: 'action', label: t('nav.controls'), detail: '18 cards', icon: 'i-grid',
        run: () => nav.go('controls')},
      {key: 'action:kill', kind: 'action', label: 'Emergency kill…', detail: 'double confirmation', icon: 'i-alert',
        run: () => $('emergencyKill')?.click()},
      {key: 'action:worker', kind: 'action', label: 'Worker controls', detail: 'start · pause · resume · stop · kill', icon: 'i-play',
        run: () => nav.go('approvals')},
      {key: 'action:theme', kind: 'action', label: 'Toggle theme', detail: 'dark / light', icon: 'i-sun',
        run: () => $('themeToggle')?.click()},
      {key: 'action:compact', kind: 'action', label: 'Compact layout', detail: 'appearance', icon: 'i-sliders',
        run: () => [...document.querySelectorAll('.ui-preferences button')].find((node) => /compact/i.test(node.textContent))?.click()},
      {key: 'action:still', kind: 'action', label: 'Reduce effects', detail: 'appearance', icon: 'i-sliders',
        run: () => [...document.querySelectorAll('.ui-preferences button')].find((node) => /reduce/i.test(node.textContent))?.click()},
      {key: 'action:wb', kind: 'action', label: 'Open Workbench', detail: 'Ctrl+K in the old layout', icon: 'i-terminal',
        run: () => $('wbOpen')?.click()},
      {key: 'action:studio', kind: 'action', label: 'Open Studio', detail: 'workflows · evidence', icon: 'i-layers',
        run: () => $('studioOpen')?.click()},
    ];
  }

  function paneCommands() {
    const items = [];
    const seen = new Set();
    document.querySelectorAll('.wb-nav button').forEach((button) => {
      const label = (button.textContent || '').trim();
      if (!label || seen.has('wb:' + label)) return;
      seen.add('wb:' + label);
      const openPane = () => { $('wbOpen')?.click(); window.setTimeout(() => button.click(), 60); };
      items.push({key: 'wb:' + label, kind: 'action', label: 'Workbench · ' + label, detail: 'existing workbench pane',
        icon: 'i-terminal', run: openPane, panel: openPane});
    });
    document.querySelectorAll('#stNav button').forEach((button) => {
      const label = (button.textContent || '').trim();
      if (!label || seen.has('st:' + label)) return;
      seen.add('st:' + label);
      const openStudioPane = () => { $('studioOpen')?.click(); window.setTimeout(() => button.click(), 60); };
      items.push({key: 'st:' + label, kind: 'action', label: 'Studio · ' + label, detail: 'existing studio pane',
        icon: 'i-layers', run: openStudioPane, panel: openStudioPane});
    });
    items.push({key: 'wb:commands', kind: 'action', label: 'Workbench command list', detail: 'Ctrl+Alt+K inside the workbench',
      icon: 'i-command', run: () => { $('wbOpen')?.click(); }});
    return items;
  }

  function allItems() {
    const items = pageItems().concat(actions(), paneCommands());
    for (const role of data.roles) {
      items.push({key: 'role:' + role.name, kind: 'role', label: role.title || role.name,
        detail: [role.name, role.category, role.agent].filter(Boolean).join(' · '), icon: ICONS.role,
        extra: role.name + ' ' + role.category,
        run: () => pickRole(role.name), panel: () => pickRole(role.name, true)});
    }
    for (const agent of data.agents) {
      items.push({key: 'agent:' + agent.name, kind: 'agent', label: agent.name,
        detail: [agent.provider, agent.model].filter(Boolean).join(' · '), icon: ICONS.agent,
        run: () => { nav.go('cost'); }, panel: () => panel('models')});
    }
    for (const pipeline of data.pipelines) {
      items.push({key: 'pipeline:' + pipeline.name, kind: 'workflow', label: pipeline.name,
        detail: (pipeline.description || '') + (pipeline.roles?.length ? ' · ' + pipeline.roles.join(' → ') : ''),
        icon: ICONS.workflow, run: () => usePipeline(pipeline.name), panel: () => $('studioOpen')?.click()});
    }
    for (const task of data.tasks) {
      items.push({key: 'task:' + task.task_id, kind: task.status === 'done' ? 'run' : 'task',
        label: String(task.task || '').slice(0, 110) || task.task_id,
        detail: [task.status, task.role, task.agent].filter(Boolean).join(' · '), icon: ICONS.task,
        run: () => openRun(task.task_id), panel: () => openRun(task.task_id)});
    }
    for (const run of data.runs || []) {
      items.push({key: 'run:' + run.task_id, kind: 'run', label: String(run.task || '').slice(0, 110) || run.task_id,
        detail: [run.status, run.role, run.created ? new Date(run.created * 1000).toLocaleString() : ''].filter(Boolean).join(' · '),
        icon: ICONS.run, run: () => openRun(run.task_id), panel: () => openRun(run.task_id)});
    }
    for (const workflow of data.workflows || []) {
      items.push({key: 'workflow:' + (workflow.id || workflow.name), kind: 'workflow',
        label: workflow.name || workflow.title || workflow.id, detail: workflow.description || 'Studio workflow',
        icon: ICONS.workflow, run: () => panel('Workflows', 'studio'), panel: () => panel('Workflows', 'studio')});
    }
    for (const file of data.files || []) {
      items.push({key: 'file:' + file, kind: 'file', label: file, detail: 'changed file', icon: ICONS.file,
        run: () => panel('files'), panel: () => panel('files')});
    }
    return items;
  }

  function pickRole(name, keepView) {
    const select = $('roleSelect');
    if (!select) return;
    if (![...select.options].some((option) => option.value === name)) select.add(new Option(name, name));
    select.value = name;
    if (!keepView) {
      nav.go('run');
      window.setTimeout(() => $('taskInput')?.focus(), 120);
    } else {
      window.nexusToast?.(`Role pinned in the composer: ${name}`, {tone: 'info'});
    }
  }
  function usePipeline(name) {
    nav.go('run');
    window.setTimeout(() => {
      const select = $('pipelineSelect');
      if (select) select.value = name;
      $('taskInput')?.focus();
    }, 120);
  }
  function openRun(taskId) {
    nav.go('run');
    window.setTimeout(() => { try { window.nexusAttachTask?.(taskId); } catch { /* attach is best effort */ } }, 120);
  }
  function panel(pane, owner = 'workbench') {
    if (owner === 'studio') {
      $('studioOpen')?.click();
      const button = [...document.querySelectorAll('#stNav button')].find((node) => node.textContent.trim() === pane);
      button?.click();
      return;
    }
    if (typeof window.nexusWorkbenchShow === 'function') window.nexusWorkbenchShow(pane);
    else $('wbOpen')?.click();
  }

  /* ── data loading ──────────────────────────────────────────────────── */
  async function loadIndex() {
    if (data.loaded || data.loading) return;
    data.loading = true;
    render();
    try {
      const [index, tasks] = await Promise.all([gui.api('/api/palette'), gui.api('/api/tasks')]);
      const payload = index?.data || index || {};
      data.roles = Array.isArray(payload.roles) ? payload.roles : [];
      data.agents = Array.isArray(payload.agents) ? payload.agents : [];
      data.pipelines = Array.isArray(payload.pipelines) ? payload.pipelines : [];
      data.builtAt = Number(payload.built_at) || 0;
      data.tasks = Array.isArray(tasks) ? tasks : [];
      data.loaded = true;
      data.failed = '';
    } catch (error) {
      data.failed = error?.message || 'index unavailable';
    } finally {
      data.loading = false;
      render();
    }
  }

  async function loadLazy(prefix) {
    if (prefix === '#' && data.runs === null) {
      try {
        const runs = await gui.api('/api/workbench/history');
        const rows = Array.isArray(runs) ? runs : runs?.data;
        data.runs = Array.isArray(rows) ? rows.slice(0, 60) : [];
      } catch { data.runs = []; }
    }
    if (prefix === '>' && (data.workflows === null || data.files === null)) {
      const [workflows, files] = await Promise.allSettled([
        gui.api('/api/workbench/studio/items?kind=workflow'),
        gui.api('/api/workbench/files'),
      ]);
      data.workflows = workflows.status === 'fulfilled'
        ? (Array.isArray(workflows.value) ? workflows.value : workflows.value?.data || []) : [];
      data.files = files.status === 'fulfilled'
        ? (Array.isArray(files.value) ? files.value : files.value?.data || []) : [];
    }
    if (state.query) render();
  }

  /* ── rendering ─────────────────────────────────────────────────────── */
  const row = (item, index, pinned) => {
    const li = document.createElement('li');
    li.className = 'palette-row';
    li.id = 'palette-opt-' + index;
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', String(index === state.active));
    li.dataset.kind = item.kind;
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('class', 'ic');
    icon.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#' + (item.icon || ICONS[item.kind] || 'i-search'));
    icon.append(use);
    const body = document.createElement('div');
    body.className = 'palette-body';
    const title = document.createElement('b');
    title.textContent = item.label;
    const detail = document.createElement('span');
    detail.textContent = item.detail || '';
    body.append(title, detail);
    const kind = document.createElement('span');
    kind.className = 'palette-kind';
    kind.textContent = t('palette.type.' + item.kind);
    const keys = document.createElement('kbd');
    keys.textContent = pinned ? '⇧↵' : '↵';
    li.append(icon, body, kind, keys);
    return li;
  };

  function currentItems() {
    const started = performance.now();
    const done = (rows) => {
      state.lastMs = Math.round((performance.now() - started) * 10) / 10;
      return rows;
    };
    const raw = state.query;
    const prefix = '>@#/'.includes(raw[0] || '') ? raw[0] : '';
    const text = prefix ? raw.slice(1).trim() : raw.trim();
    const query = text.toLowerCase();

    if (!prefix && !query) {
      const all = allItems();
      const byKey = new Map(all.map((item) => [item.key, item]));
      const pins = readStore(PINS).map((key) => byKey.get(key)).filter(Boolean);
      const recents = readStore(RECENTS).map((key) => byKey.get(key)).filter(Boolean);
      const pages = all.filter((item) => item.kind === 'page');
      const out = [];
      const push = (item, pinned) => { if (!out.some((row) => row.key === item.key)) out.push({...item, pinned}); };
      pins.forEach((item) => push(item, true));
      recents.forEach((item) => push(item, false));
      pages.forEach((item) => push(item, false));
      return done(out.slice(0, 60));
    }

    const wants = prefix === '>' ? ['action'] : prefix === '@' ? ['role'] : prefix === '#' ? ['task', 'run']
      : prefix === '/' ? ['page', 'setting', 'control'] : null;
    const pool = allItems().filter((item) => !wants || wants.includes(item.kind));
    const scored = [];
    for (const item of pool) {
      const value = best(query, item);
      if (value > 0) scored.push([value, item]);
    }
    scored.sort((a, b) => b[0] - a[0] || a[1].label.localeCompare(b[1].label));
    return done(scored.slice(0, 60).map((pair) => pair[1]));
  }

  function render() {
    if (!dialog.open) return;
    state.items = currentItems();
    if (state.active >= state.items.length) state.active = Math.max(0, state.items.length - 1);
    list.replaceChildren();
    if (data.loading && !data.loaded) {
      const li = document.createElement('li');
      li.className = 'palette-empty';
      li.textContent = t('palette.loading');
      list.append(li);
    } else if (data.failed && !data.loaded) {
      const li = document.createElement('li');
      li.className = 'palette-empty';
      li.textContent = t('palette.offline');
      list.append(li);
    } else if (!state.items.length) {
      const li = document.createElement('li');
      li.className = 'palette-empty';
      li.textContent = t('palette.empty');
      list.append(li);
    } else {
      state.items.forEach((item, index) => list.append(row(item, index, item.pinned)));
    }
    if (state.items.length) input.setAttribute('aria-activedescendant', 'palette-opt-' + state.active);
    else input.removeAttribute('aria-activedescendant');
    if (counter) counter.textContent = state.items.length ? `${state.items.length} ${t('palette.count')}` : '';
    if (hint) hint.textContent = t('palette.hint');
  }

  /* ── keyboard + opening ────────────────────────────────────────────── */
  function setActive(index) {
    if (!state.items.length) return;
    state.active = (index + state.items.length) % state.items.length;
    [...list.children].forEach((node, position) => node.setAttribute?.('aria-selected', String(position === state.active)));
    list.children[state.active]?.scrollIntoView({block: 'nearest'});
  }

  function remember(item) {
    const recents = readStore(RECENTS).filter((key) => key !== item.key);
    recents.unshift(item.key);
    writeStore(RECENTS, recents.slice(0, 8));
  }

  function close() {
    if (!dialog.open) return;
    dialog.close();
    input.value = '';
    state.query = '';
  }

  function choose(mode) {
    const item = state.items[state.active];
    if (!item) return;
    remember(item);
    if (mode === 'panel' && (item.panel || item.section)) {
      if (item.section) {
        const url = location.pathname + '#' + item.section;
        window.open(url, '_blank', 'noopener');
        close();
        return;
      }
      item.panel();
      close();
      return;
    }
    if (mode === 'pin') {
      const pins = readStore(PINS);
      const next = pins.includes(item.key) ? pins.filter((key) => key !== item.key) : [item.key, ...pins];
      writeStore(PINS, next);
      window.nexusToast?.(item.key + (next.includes(item.key) ? ' pinned' : ' unpinned'), {tone: 'info'});
      render();
      return;
    }
    close();
    item.run();
  }

  function open(query = '') {
    if (dialog.open) return;
    dialog.showModal();
    input.value = query;
    state.query = query;
    state.active = 0;
    render();
    loadIndex();
    if (query && '>@#/'.includes(query[0])) loadLazy(query[0]);
    input.focus();
  }

  input.addEventListener('input', () => {
    state.query = input.value;
    state.active = 0;
    render();
    const first = state.query[0];
    if ('>@#/'.includes(first || '')) loadLazy(first);
  });

  input.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive(state.active + 1); return; }
    if (event.key === 'ArrowUp') { event.preventDefault(); setActive(state.active - 1); return; }
    if (event.key === 'Home') { event.preventDefault(); setActive(0); return; }
    if (event.key === 'End') { event.preventDefault(); setActive(state.items.length - 1); return; }
    if (event.key === 'Escape') { event.preventDefault(); close(); return; }
    if (event.key === 'Enter') {
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) choose('pin');
      else choose(event.shiftKey ? 'panel' : 'open');
      return;
    }
    if (event.key === 'Tab' && !event.shiftKey && state.items.length) {
      const item = state.items[state.active];
      const prefix = item.kind === 'role' ? '@' : item.kind === 'action' ? '>' : ['task', 'run'].includes(item.kind) ? '#' : '/';
      event.preventDefault();
      input.value = prefix + (state.query.replace(/^[>@#/]/, '').trim());
      state.query = input.value;
      render();
    }
  });

  list.addEventListener('click', (event) => {
    const rowNode = event.target.closest('.palette-row');
    if (!rowNode) return;
    state.active = Number(rowNode.id.replace('palette-opt-', ''));
    choose('open');
  });
  list.addEventListener('mousemove', (event) => {
    const rowNode = event.target.closest('.palette-row');
    if (!rowNode) return;
    const index = Number(rowNode.id.replace('palette-opt-', ''));
    if (index !== state.active) setActive(index);
  });

  document.addEventListener('keydown', (event) => {
    const combo = (event.ctrlKey || event.metaKey) && (event.key === 'k' || event.key === 'K' || (event.shiftKey && (event.key === 'p' || event.key === 'P')));
    if (combo) {
      event.preventDefault();
      if (dialog.open) close();
      else open();
    }
  });

  dialog.addEventListener('click', (event) => { if (event.target === dialog) close(); });
  dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(); });

  i18n.onChange(() => { if (dialog.open) render(); });

  window.nexusPalette = {
    open,
    close,
    search(query) { open(query); return state.items.length; },
    get lastMs() { return state.lastMs; },
    get count() { return state.items.length; },
    get data() { return data; },
    loadIndex,
  };

  /* Warm the index during idle time so the first Ctrl+K is instant. */
  gui.ready.then(() => {
    const warm = () => loadIndex().catch(() => {});
    if ('requestIdleCallback' in window) window.requestIdleCallback(warm, {timeout: 4000});
    else window.setTimeout(warm, 1500);
  });
})();
