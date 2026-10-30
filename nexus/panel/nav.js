/* Section navigation, breadcrumb, approval badge and live status mirrors.

   Nothing in this file executes a task, grants a permission or changes policy:
   every entry opens an existing page, dialog or pane. The four legacy hashes
   (#run, #control, #operations, #world) keep working because the view switcher
   in gui.js still owns them. */
(() => {
  'use strict';

  const gui = window.nexusGui;
  const i18n = window.nexusI18n;
  if (!gui || !i18n) return;
  const {t} = i18n;
  const $ = (id) => document.getElementById(id);

  const openWorkbench = (pane) => {
    if (typeof window.nexusWorkbenchShow === 'function') window.nexusWorkbenchShow(pane);
    else $('guiSettings')?.click();
  };
  const openStudio = (pane) => {
    const open = $('studioOpen');
    if (!open) return;
    open.click();
    const button = [...document.querySelectorAll('#stNav button')].find((node) => node.textContent.trim() === pane);
    button?.click();
  };

  /* ── the twelve sections ───────────────────────────────────────────── */
  const SECTIONS = [
    {id: 'mission', group: 'operate', icon: 'i-radar', label: 'nav.mission', aria: 'nav.missionAria', view: 'mission', hint: '1'},
    {id: 'run', group: 'operate', icon: 'i-chat', label: 'nav.run', aria: 'nav.runAria', view: 'run', hint: '2'},
    {id: 'roles', group: 'operate', icon: 'i-route', label: 'nav.roles', view: 'roles',
      detail: '1000 roles, prompts, history'},
    {id: 'computer', group: 'operate', icon: 'i-monitor', label: 'nav.computer', run: () => openWorkbench('desktopSetup'),
      detail: 'permissions, browser, editor, service'},
    {id: 'workflows', group: 'operate', icon: 'i-workflow', label: 'nav.workflows', run: () => openStudio('Workflows'),
      detail: 'pipelines & Studio workflows'},
    {id: 'approvals', group: 'operate', icon: 'i-shield', label: 'nav.approvals', aria: 'nav.approvalAria', view: 'operations', anchor: 'reviews',
      badge: true, hint: '3'},
    {id: 'security', group: 'operate', icon: 'i-lock', label: 'nav.security', view: 'operations', anchor: 'audit',
      detail: 'permission audit, traces, diagnostics'},
    {id: 'memory', group: 'configure', icon: 'i-brain', label: 'nav.memory', view: 'run', anchor: 'memCard',
      focus: 'brainQuery', detail: 'role memory & learning brain'},
    {id: 'cost', group: 'configure', icon: 'i-coin', label: 'nav.cost', run: () => openWorkbench('models'),
      detail: 'Ollama inventory, prices, policy'},
    {id: 'world', group: 'configure', icon: 'i-globe', label: 'nav.world', aria: 'nav.worldAria', view: 'world', hint: '4'},
    {id: 'integrations', group: 'configure', icon: 'i-puzzle', label: 'nav.integrations', view: 'run', anchor: 'integrations',
      detail: 'providers, keys, MCP & Telegram'},
    {id: 'controls', group: 'configure', icon: 'i-grid', label: 'nav.controls', aria: 'nav.controlAria', view: 'control',
      detail: 'all 18 control cards'},
    {id: 'settings', group: 'configure', icon: 'i-sliders', label: 'nav.settings', run: () => $('guiSettings')?.click(),
      detail: 'layout, language, policy'},
  ];

  const byId = new Map(SECTIONS.map((section) => [section.id, section]));
  const viewTitles = {mission: 'nav.mission', roles: 'nav.roles', run: 'nav.run', operations: 'nav.approvals',
    world: 'nav.world', control: 'nav.controls'};
  let activeId = null;
  let pendingAnchor = null;

  /* ── localisation of the static shell ──────────────────────────────── */
  function paintLabels() {
    document.querySelectorAll('[data-i18n]').forEach((node) => { node.textContent = t(node.dataset.i18n); });
    document.querySelectorAll('[data-i18n-aria]').forEach((node) => { node.setAttribute('aria-label', t(node.dataset.i18nAria)); });
    document.querySelectorAll('[data-i18n-title]').forEach((node) => { node.setAttribute('title', t(node.dataset.i18nTitle)); });
    for (const section of SECTIONS) {
      const item = document.querySelector(`.sidebar-nav [data-nav-id="${section.id}"]`);
      if (item) {
        item.setAttribute('aria-label', t(section.aria || section.label));
        const label = item.querySelector('.nav-label');
        if (label) label.textContent = t(section.label);
      }
    }
    paintActive();
  }

  /* ── active state, breadcrumb and page title ───────────────────────── */
  function paintActive() {
    const section = activeId ? byId.get(activeId) : null;
    document.querySelectorAll('.sidebar-nav .nav-item').forEach((node) => {
      const on = node.dataset.navId === activeId || node.dataset.guiTab === activeId;
      if (on) node.setAttribute('aria-current', 'page');
      else node.removeAttribute('aria-current');
      if (node.getAttribute('role') === 'tab') node.setAttribute('aria-selected', String(on));
    });
    document.querySelectorAll('#tabbar [data-nav]').forEach((node) => {
      const view = node.dataset.nav;
      const on = section ? (section.view === view || (view === 'approvals' && section.view === 'operations')) : false;
      if (on) { node.setAttribute('aria-current', 'page'); node.setAttribute('aria-selected', 'true'); }
      else { node.removeAttribute('aria-current'); node.setAttribute('aria-selected', 'false'); }
    });
    if (section) {
      const title = $('pageTitle');
      if (title) title.textContent = t(section.label);
      const group = section.group === 'configure' ? 'group.configure' : 'group.operate';
      const crumb = $('crumbSection');
      if (crumb) crumb.textContent = t(group);
    }
  }

  function sectionForView(view) {
    return SECTIONS.find((section) => section.view === view && !section.anchor && !section.run)
      || SECTIONS.find((section) => section.view === view)
      || null;
  }

  window.addEventListener('nexus:view', (event) => {
    const view = event.detail;
    const current = activeId ? byId.get(activeId) : null;
    if (!current || current.view !== view) {
      const match = sectionForView(view);
      if (match) activeId = match.id;
      else if (viewTitles[view]) activeId = null;
    }
    if (activeId === null) {
      const title = $('pageTitle');
      if (title && viewTitles[view]) title.textContent = t(viewTitles[view]);
    }
    paintActive();
    if (pendingAnchor) {
      const anchor = pendingAnchor;
      pendingAnchor = null;
      window.requestAnimationFrame(() => scrollTo(anchor));
    }
  });

  function scrollTo(anchor) {
    const node = $(anchor);
    if (!node) return;
    node.scrollIntoView({block: 'center', behavior: document.body.classList.contains('ui-still') ? 'auto' : 'smooth'});
    if (node.matches('input, textarea, select, button')) node.focus({preventScroll: true});
    else {
      const target = node.querySelector('input, textarea, button');
      if (target) target.focus({preventScroll: true});
    }
    if (!node.hasAttribute('tabindex') && !node.matches('input, textarea, select, button')) {
      node.setAttribute('tabindex', '-1');
      node.focus({preventScroll: true});
    }
  }

  /* ── tab roles follow which control is actually on screen ───────────
     Desktop: the drawer is the visible list, so its items are the tabs.
     ≤1080 px: the drawer is off-canvas and the bottom bar is the tablist, so
     the drawer items become plain buttons. Without this, assistive tech (and
     any role query) sees two copies of every section. */
  const narrow = window.matchMedia('(max-width: 1080px)');
  function syncTabRoles() {
    const drawerItems = [...document.querySelectorAll('.sidebar-nav .nav-item')];
    const drawer = document.getElementById('sidebarNav');
    if (narrow.matches) {
      drawerItems.forEach((node) => {
        node.removeAttribute('role');
        node.removeAttribute('aria-selected');
      });
      drawer?.removeAttribute('role');
      document.querySelectorAll('#tabbar button').forEach((node) => node.setAttribute('role', 'tab'));
    } else {
      drawerItems.filter((node) => node.dataset.guiTab).forEach((node) => node.setAttribute('role', 'tab'));
      drawer?.setAttribute('role', 'tablist');
      document.querySelectorAll('#tabbar button').forEach((node) => node.removeAttribute('role'));
    }
  }
  narrow.addEventListener?.('change', () => { syncTabRoles(); paintActive(); });
  syncTabRoles();

  /* ── navigation ────────────────────────────────────────────────────── */
  function go(id, options = {}) {
    const section = byId.get(id);
    if (!section) return false;
    activeId = id;
    if (section.view) {
      pendingAnchor = section.anchor || null;
      const pendingFocus = section.focus || null;
      gui.show(section.view);
      if (!section.anchor && pendingFocus) scrollTo(pendingFocus);
    } else if (section.run) {
      section.run();
    }
    paintActive();
    if (options.closeDrawer !== false) window.nexusShell?.closeNav?.();
    return true;
  }

  document.querySelectorAll('.sidebar-nav [data-nav-id], #tabbar [data-nav]').forEach((node) => {
    const id = node.dataset.navId || node.dataset.nav;
    if (!byId.has(id)) return;
    node.addEventListener('click', () => go(id));
  });

  /* Keyboard: Alt+1…4 jumps to the four primary views, Alt+0 opens All controls. */
  const shortcut = {'1': 'mission', '2': 'run', '3': 'approvals', '4': 'world', '0': 'controls'};
  document.addEventListener('keydown', (event) => {
    if (!event.altKey || event.ctrlKey || event.metaKey) return;
    const id = shortcut[event.key];
    if (!id) return;
    event.preventDefault();
    go(id);
  });

  /* ── approval badge + notification source ──────────────────────────── */
  const badge = $('navApprovalBadge');
  const bellCount = $('bellCount');
  let lastState = null;

  function paintBadge(count) {
    const value = Number.isFinite(count) ? count : null;
    for (const node of [badge, bellCount]) {
      if (!node) continue;
      node.textContent = value === null ? '–' : String(Math.min(value, 99));
      node.hidden = !value;
      node.classList.toggle('hot', !!value && value > 0);
    }
    const approvalItem = document.querySelector('.sidebar-nav [data-nav-id="approvals"]');
    if (approvalItem) {
      const name = t('nav.approvalAria');
      const text = value ? t('bell.pending', {count: value}) : '';
      approvalItem.title = text ? `${name} — ${text}` : name;
      /* the accessible name stays stable for assistive tech and old tests */
      approvalItem.setAttribute('aria-label', name);
      approvalItem.setAttribute('aria-description', text);
    }
  }

  async function pollState() {
    if (document.visibilityState !== 'visible') return;
    try {
      lastState = await gui.api('/api/operations/state');
    } catch {
      paintBadge(null);
      return;
    }
    paintBadge(Number(lastState.pending_actions_total) || 0);
    window.dispatchEvent(new CustomEvent('nexus:pending', {detail: lastState}));
  }

  /* The Mission Control feed pushes approvals; paint the badge at once
     instead of waiting for the next poll. */
  window.addEventListener('nexus:pending', (event) => {
    const pending = Number(event.detail?.pending_actions_total);
    if (Number.isFinite(pending)) paintBadge(pending);
  });

  gui.ready.then(() => {
    pollState();
    setInterval(pollState, 20000);
  });

  /* ── mirrors: agent chip and SSE state in the status footer ────────── */
  const agentChip = $('metaAgent');
  const statusAgent = $('sbAgent');
  if (agentChip && statusAgent) {
    const sync = () => {
      const text = (statusAgent.textContent || '').replace(/^agent\s*/i, '').trim();
      agentChip.textContent = text && text !== '—' ? text : t('topbar.autoRoute');
    };
    new MutationObserver(sync).observe(statusAgent, {childList: true, characterData: true, subtree: true});
    sync();
  }

  const sse = $('sbSse');
  const setSse = (state) => {
    if (!sse) return;
    const live = state === 'streaming';
    sse.textContent = live ? 'SSE · live' : `SSE · ${t('mission.idle')}`;
    sse.dataset.state = live ? 'live' : 'idle';
  };
  setSse('idle');
  /* Two independent streams can be open: the run's own event log and the
     global Mission Control feed. The footer is live while either one is. */
  const sources = new Map();
  const refreshSse = () => setSse([...sources.values()].some((value) => value === 'streaming') ? 'streaming' : 'idle');
  window.addEventListener('nexus:sse', (event) => {
    sources.set('run', event.detail);
    refreshSse();
  });
  window.addEventListener('nexus:feed', (event) => {
    sources.set('mission', event.detail);
    refreshSse();
  });

  /* ── language switching ────────────────────────────────────────────── */
  i18n.onChange(() => { paintLabels(); paintActive(); });
  document.addEventListener('nexus:language', paintLabels);

  paintLabels();
  activeId = (SECTIONS.find((section) => section.view === (gui.view || 'mission')) || {}).id || null;
  paintActive();

  /* Palette and Mission Control read this registry instead of a second list. */
  window.nexusNav = {
    sections: SECTIONS,
    go,
    get active() { return activeId; },
    setActive(id) { if (byId.has(id)) { activeId = id; paintActive(); } },
    detail(section) { return section.detail || ''; },
  };
})();
