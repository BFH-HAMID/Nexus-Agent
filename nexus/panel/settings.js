/* Settings: one page that says where each choice lives, and nothing more.

   Three kinds of choice existed before this page and each lived somewhere
   different: the theme and the two display toggles in this browser's storage,
   the card layout and pane language in the Workbench's SQLite preferences, and
   the World switches in widget.yaml. The hub reads all three and edits none of
   them directly — it calls the owner (window.nexusAppearance, nexusI18n, the
   Workbench pane, the World settings button), so there is still exactly one
   implementation of each preference.

   Nothing here executes a task, grants a permission or changes policy. Every
   button opens an existing pane, and the one server read is a read-only
   snapshot (/api/settings/summary) whose timestamp is printed on the page. */
(() => {
  'use strict';

  const gui = window.nexusGui;
  const i18n = window.nexusI18n;
  const appearance = window.nexusAppearance;
  if (!gui || !i18n) return;
  const t = i18n.t;
  const $ = (id) => document.getElementById(id);
  const view = $('view-settings');
  if (!view) return;

  const STALE_MS = 30000;
  const state = {data: null, readAt: 0, busy: false, langSaved: null};

  const locale = () => (i18n.lang === 'bn' ? 'bn-BD' : 'en-GB');
  const stamp = (seconds) => {
    const ms = Number(seconds) * 1000;
    if (!Number.isFinite(ms) || ms <= 0) return t('settings.unknown');
    try {
      return new Date(ms).toLocaleString(locale(), {dateStyle: 'medium', timeStyle: 'medium'});
    } catch {
      return new Date(ms).toISOString();
    }
  };
  const say = (message, tone = '') => {
    const node = $('settingsStatus');
    if (!node) return;
    node.textContent = message || '';
    if (tone) node.dataset.tone = tone;
    else delete node.dataset.tone;
  };
  const fail = (message) => {
    const node = $('settingsError');
    if (!node) return;
    node.textContent = message;
    node.hidden = !message;
  };

  /* ── facts: a labelled value straight from the server read ───────────── */
  function factList(host, rows) {
    if (!host) return;
    host.replaceChildren(...rows.map(([label, value, tone]) => {
      const box = document.createElement('div');
      const dt = document.createElement('dt');
      const dd = document.createElement('dd');
      dt.textContent = label;
      dd.textContent = value === undefined || value === null || value === '' ? t('settings.unknown') : String(value);
      if (tone) dd.dataset.tone = tone;
      box.append(dt, dd);
      return box;
    }));
  }

  function paintFacts() {
    const data = state.data;
    const workspace = data?.workspace || {};
    const world = data?.world || {};
    const build = data?.build || {};
    const yes = t('settings.yes');
    const no = t('settings.no');
    const unknown = t('settings.unknown');

    factList($('settingsWorkspaceFacts'), workspace.available ? [
      [t('settings.factLanguage'), workspace.language === 'bn' ? 'বাংলা' : 'English'],
      [t('settings.factFocus'), workspace.focus_mode ? yes : no, workspace.focus_mode ? 'ok' : 'off'],
      [t('settings.factHidden'), String(workspace.hidden_cards ?? 0)],
      [t('settings.factModel'), workspace.default_local_model || unknown, workspace.default_local_model ? '' : 'off'],
      [t('settings.factLocalOnly'), workspace.local_only ? yes : no, workspace.local_only ? 'ok' : 'warn'],
      [t('settings.factWrite'), String(workspace.write_folders ?? 0)],
      [t('settings.factPriced'), String(workspace.priced_models ?? 0)],
      [t('settings.factMapped'), String(workspace.mapped_roles ?? 0)],
    ] : [[t('settings.factWorkspace'), unknown, 'warn']]);

    factList($('settingsWorldFacts'), world.available ? [
      [t('settings.factWorldEnabled'), world.enabled ? yes : no, world.enabled ? 'ok' : 'off'],
      [t('settings.factProvider'), world.provider || unknown],
      [t('settings.factSources'), `${world.sources ?? 0} / ${world.authorized_sources ?? 0}`],
      [t('settings.factKey'), world.weather_key_present ? t('settings.configured') : t('settings.notConfigured'),
        world.weather_key_present ? 'ok' : 'warn'],
    ] : [[t('settings.factWorld'), unknown, 'warn']]);

    factList($('settingsBuildFacts'), data ? [
      [t('settings.factVersion'), build.version || unknown],
      [t('settings.factRoles'), String(build.roles ?? 0)],
      [t('settings.factAgents'), String(build.agents ?? 0)],
      [t('settings.factPipelines'), String(build.pipelines ?? 0)],
      [t('settings.factDemo'), build.demo ? t('settings.demoOn') : t('settings.demoOff'), build.demo ? 'warn' : 'ok'],
      [t('settings.factAuth'), build.auth_required ? t('settings.tokenOn') : t('settings.tokenOff'),
        build.auth_required ? 'ok' : 'warn'],
    ] : [[t('settings.factBuild'), unknown, 'warn']]);

    chip('settingsWorkspaceChip', data ? (workspace.available ? t('settings.chipWorkspace') : t('settings.chipUnavailable')) : t('settings.chipUnread'),
      data ? (workspace.available ? 'ok' : 'warn') : 'off');
    chip('settingsWorldChip', data ? (world.available ? t('settings.chipConfig') : t('settings.chipUnavailable')) : t('settings.chipUnread'),
      data ? (world.available ? 'ok' : 'warn') : 'off');
    chip('settingsBuildChip', data ? t('settings.chipBuild') : t('settings.chipUnread'), data ? 'ok' : 'off');
    const stampNode = $('settingsStamp');
    if (stampNode) stampNode.textContent = data ? t('settings.observed', {at: stamp(data.observed_at)}) : '—';
  }

  function chip(id, text, tone = '') {
    const node = $(id);
    if (!node) return;
    node.textContent = text;
    if (tone) node.dataset.tone = tone;
    else delete node.dataset.tone;
  }

  /* ── appearance: reads and writes go through window.nexusAppearance ───── */
  function paintAppearance() {
    const current = appearance ? appearance.settings() : {theme: document.documentElement.dataset.theme || 'dark', compact: false, still: false};
    view.querySelectorAll('[data-theme-choice]').forEach((button) => {
      button.setAttribute('aria-pressed', String(button.dataset.themeChoice === current.theme));
    });
    for (const [id, key] of [['settingsCompact', 'compact'], ['settingsStill', 'still']]) {
      const button = $(id);
      if (!button) continue;
      const on = Boolean(current[key]);
      button.setAttribute('aria-pressed', String(on));
      const label = button.querySelector('.settings-state');
      if (label) label.textContent = on ? t('settings.on') : t('settings.off');
    }
    chip('settingsAppearanceChip', t('settings.chipBrowser'), 'ok');
  }

  /* ── language: the dashboard owner is nexusI18n, mirrored to the pane ─── */
  function paintLanguage() {
    view.querySelectorAll('[data-lang-choice]').forEach((button) => {
      button.setAttribute('aria-pressed', String(button.dataset.langChoice === i18n.lang));
    });
    chip('settingsLanguageChip', state.langSaved === 'browser' ? t('settings.chipBrowserOnly') : t('settings.chipSynced'),
      state.langSaved === 'browser' ? 'warn' : 'ok');
  }

  async function setLanguage(next) {
    if (next !== 'bn' && next !== 'en') return;
    i18n.setLang(next);
    state.langSaved = null;
    paintLanguage();
    try {
      /* The Workbench panes keep their own server-side language; keep the two
         in step instead of letting them drift apart. */
      await gui.api('/api/workbench/settings', {language: next});
      state.langSaved = 'workspace';
      say(t('settings.statusLanguage', {language: next === 'bn' ? 'বাংলা' : 'English'}), 'ok');
    } catch (error) {
      state.langSaved = 'browser';
      say(t('settings.statusLanguageLocal', {reason: error.message}), 'warn');
    }
    paintLanguage();
  }

  /* ── the one read ─────────────────────────────────────────────────────── */
  async function load() {
    if (state.busy) return;
    state.busy = true;
    fail('');
    try {
      const payload = await gui.api('/api/settings/summary');
      const data = payload?.data ?? payload;
      if (!data || typeof data !== 'object' || !data.build) throw new Error(t('settings.unknownShape'));
      state.data = data;
      state.readAt = Date.now();
      paintFacts();
    } catch (error) {
      state.data = null;
      paintFacts();
      fail(t('settings.readFailed', {reason: error.message}));
    } finally {
      state.busy = false;
    }
  }

  /* ── opening existing panes: never a new action ───────────────────────── */
  const openWorkbench = (pane) => {
    if (typeof window.nexusWorkbenchShow === 'function') window.nexusWorkbenchShow(pane);
    else $('wbOpen')?.click();
  };
  const openWorldSettings = () => {
    const button = $('guiWorldSettings');
    if (button) button.click();
    else openWorkbench('worldSettings');
  };
  const go = (id) => {
    if (window.nexusNav?.go) window.nexusNav.go(id);
    else gui.show(id);
  };

  /* ── wiring ───────────────────────────────────────────────────────────── */
  view.querySelectorAll('[data-theme-choice]').forEach((button) => {
    button.onclick = () => {
      if (!appearance) return;
      const theme = appearance.theme(button.dataset.themeChoice);
      paintAppearance();
      say(t('settings.statusTheme', {theme: theme === 'light' ? t('settings.light') : t('settings.dark')}), 'ok');
    };
  });
  for (const [id, key, label] of [['settingsCompact', 'compact', 'settings.compact'], ['settingsStill', 'still', 'settings.still']]) {
    const button = $(id);
    if (!button) continue;
    button.onclick = () => {
      if (!appearance) return;
      const on = appearance.toggle(key);
      paintAppearance();
      say(t('settings.statusToggle', {what: t(label), state: on ? t('settings.on') : t('settings.off')}), 'ok');
    };
  }
  const reset = $('settingsResetDisplay');
  if (reset) reset.onclick = () => {
    if (!appearance) return;
    appearance.reset();
    appearance.theme('dark');
    paintAppearance();
    say(t('settings.statusReset'), 'ok');
  };
  view.querySelectorAll('[data-lang-choice]').forEach((button) => {
    button.onclick = () => { setLanguage(button.dataset.langChoice); };
  });
  const bind = (id, handler) => { const node = $(id); if (node) node.onclick = handler; };
  bind('settingsReload', () => load());
  bind('settingsOpenLayout', () => openWorkbench('layout'));
  bind('settingsOpenWorkbench', () => { const open = $('wbOpen'); if (open) open.click(); else openWorkbench('overview'); });
  bind('settingsOpenWorldSettings', openWorldSettings);
  bind('settingsOpenWorld', () => go('world'));
  bind('settingsOpenDesktopSetup', () => {
    if (typeof window.nexusDesktopSetup === 'function') window.nexusDesktopSetup('context');
    else openWorkbench('desktopSetup');
  });
  bind('settingsOpenControls', () => go('controls'));
  view.querySelectorAll('[data-goto]').forEach((button) => {
    button.onclick = () => go(button.dataset.goto);
  });

  window.addEventListener('nexus:appearance', paintAppearance);
  window.addEventListener('nexus:theme', paintAppearance);
  document.addEventListener('nexus:language', () => { paintAppearance(); paintLanguage(); paintFacts(); });
  window.addEventListener('nexus:view', (event) => {
    if (event.detail !== 'settings') return;
    paintAppearance();
    paintLanguage();
    if (!state.data || Date.now() - state.readAt > STALE_MS) load();
    else paintFacts();
  });

  paintAppearance();
  paintLanguage();
  paintFacts();
  if (gui.view === 'settings') gui.ready.then(load);

  window.nexusSettings = {
    refresh: load,
    open: () => gui.show('settings'),
    get data() { return state.data; },
    get readAt() { return state.readAt; },
    facts: () => Object.fromEntries([...view.querySelectorAll('.settings-facts')].map((list) => [
      list.id, [...list.querySelectorAll('dt')].map((dt) => [dt.textContent, dt.nextElementSibling?.textContent]),
    ])),
  };
})();
