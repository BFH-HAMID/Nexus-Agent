/* World desk: what public data this machine actually holds, and how old it is.

   The globe below draws the world; this header explains where its data comes
   from. Every figure is read from the local cache the widget already fills —
   opening this page never contacts a publisher, and closing it loses nothing.
   The only control that leaves the machine is "Contact sources", and it needs
   the typed token, then reports each group's real answer (live / cached /
   offline). Nothing here is invented, and no headline is ever fed to an agent. */
(() => {
  'use strict';

  const gui = window.nexusGui;
  const i18n = window.nexusI18n;
  if (!gui || !i18n) return;
  const t = i18n.t;
  const $ = (id) => document.getElementById(id);

  const GROUPS = ['news', 'crypto', 'quakes', 'weather'];
  const TOKEN = 'REFRESH PUBLIC SOURCES';
  const TONES = {fresh: 'ok', stale: 'warn', missing: 'error'};
  const state = {overview: null, visible: false, busy: false, timer: 0, readAt: 0};

  const view = document.getElementById('view-world');
  const widget = document.getElementById('world-widget');
  if (!view || !widget) return;

  const root = document.createElement('section');
  root.id = 'worldDesk';
  root.className = 'security-page worlddesk';
  root.innerHTML = `
    <header class="security-head">
      <div class="security-headtext">
        <p class="security-eyebrow" data-i18n="worlddesk.eyebrow"></p>
        <h1 data-i18n="worlddesk.title"></h1>
        <p class="security-sub" data-i18n="worlddesk.subtitle"></p>
      </div>
      <div class="security-tools" id="wdTools">
        <button class="btn ghost" type="button" id="wdReload"><span class="btn-label" data-i18n="worlddesk.reload"></span></button>
        <button class="btn" type="button" id="wdRefresh"><span class="btn-label" data-i18n="worlddesk.refresh"></span></button>
        <span class="security-stamp" id="wdStamp"></span>
      </div>
    </header>
    <p class="security-banner" id="wdError" role="alert" hidden></p>
    <div class="security-kpis" id="wdKpis"></div>
    <section class="security-card">
      <div class="security-cardhead">
        <h2 data-i18n="worlddesk.sourcesTitle"></h2>
        <span class="security-chip" id="wdChip" role="status" aria-live="polite">—</span>
      </div>
      <p class="security-note" data-i18n="worlddesk.sourcesIntro"></p>
      <div class="security-tablewrap">
        <table class="security-table">
          <thead><tr>
            <th scope="col" data-i18n="worlddesk.colSource"></th>
            <th scope="col" data-i18n="worlddesk.colGroup"></th>
            <th scope="col" data-i18n="worlddesk.colLast"></th>
            <th scope="col" data-i18n="worlddesk.colAge"></th>
            <th scope="col" data-i18n="worlddesk.colEvery"></th>
            <th scope="col" data-i18n="worlddesk.colState"></th>
          </tr></thead>
          <tbody id="wdRows"></tbody>
        </table>
      </div>
      <ul class="security-notes" id="wdNotes"></ul>
    </section>
    <section class="security-card">
      <div class="security-cardhead"><h2 data-i18n="worlddesk.weatherTitle"></h2></div>
      <p class="security-note" data-i18n="worlddesk.weatherIntro"></p>
      <div class="memory-facts" id="wdWeatherFacts"></div>
    </section>
    <div id="wdActionHost" hidden></div>`;

  /* The header goes above the globe, inside the same view, so the widget keeps
     every id and every behaviour it already had. */
  view.insertBefore(root, widget);

  const host = $('wdActionHost');

  /* ── helpers ────────────────────────────────────────────────────────── */
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  const message = (error) => error?.payload?.error?.message || error?.message || String(error);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const stamp = (value) => {
    if (!value) return '—';
    const date = new Date(value * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
  };
  const duration = (seconds) => {
    if (seconds === null || seconds === undefined) return '—';
    const value = Math.max(0, Math.round(Number(seconds)));
    if (value < 90) return `${value}s`;
    if (value < 5400) return `${Math.round(value / 60)}m`;
    return `${(value / 3600).toFixed(1)}h`;
  };
  function showError(error) {
    const banner = $('wdError');
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

  /* ── painting ───────────────────────────────────────────────────────── */
  function kpi(label, value, tone) {
    const node = el('div', 'security-count');
    if (tone) node.dataset.tone = tone;
    node.append(el('b', '', value), el('span', '', label));
    return node;
  }

  function paintSummary() {
    const data = state.overview;
    if (!data) return;
    const totals = data.totals;
    $('wdKpis').replaceChildren(
      kpi(t('worlddesk.kpi.fresh'), `${totals.fresh}/${totals.feeds}`, totals.fresh ? 'ok' : ''),
      kpi(t('worlddesk.kpi.stale'), String(totals.stale), totals.stale ? 'warn' : ''),
      kpi(t('worlddesk.kpi.missing'), String(totals.missing), totals.missing ? 'warn' : ''),
      kpi(t('worlddesk.kpi.headlines'), String(totals.headlines)),
      kpi(t('worlddesk.kpi.quakes'), String(data.groups.quakes.items)),
      kpi(t('worlddesk.kpi.key'), data.key_present ? t('worlddesk.yes') : t('worlddesk.no'),
        data.key_present ? 'ok' : 'warn'),
    );
    $('wdStamp').textContent = t('worlddesk.observed', {when: stamp(data.observed_at)});
    $('wdChip').textContent = data.enabled
      ? t('worlddesk.chip', {fresh: totals.fresh, feeds: totals.feeds, provider: data.provider_label})
      : t('worlddesk.disabled');
    $('wdChip').dataset.tone = data.enabled ? (totals.fresh ? 'ok' : 'warn') : 'error';
    $('wdRefresh').disabled = !data.enabled;
  }

  function paintRows() {
    const data = state.overview;
    const body = $('wdRows');
    body.replaceChildren();
    for (const feed of data.sources) {
      const row = el('tr');
      row.dataset.state = feed.status;
      const name = el('td');
      name.dataset.label = t('worlddesk.colSource');
      name.append(el('b', '', feed.name));
      if (feed.terms) {
        const link = el('a', 'worlddesk-terms', t('worlddesk.terms'));
        link.href = feed.terms;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        name.append(' ', link);
      }
      const group = el('td', '', t(`worlddesk.group.${feed.group}`));
      group.dataset.label = t('worlddesk.colGroup');
      if (feed.category) group.title = feed.category;
      const last = el('td', '', feed.present ? stamp(feed.at) : t('worlddesk.never'));
      last.dataset.label = t('worlddesk.colLast');
      const age = el('td', 'worlddesk-age', feed.present ? duration(feed.age_s) : '—');
      age.dataset.label = t('worlddesk.colAge');
      const every = el('td', '', `${Math.round(feed.ttl_s / 60)}m`);
      every.dataset.label = t('worlddesk.colEvery');
      const chip = el('span', 'security-chip', t(`worlddesk.state.${feed.status}`));
      chip.dataset.tone = TONES[feed.status] || '';
      const status = el('td');
      status.dataset.label = t('worlddesk.colState');
      status.append(chip);
      row.append(name, group, last, age, every, status);
      body.append(row);
    }
    const notes = $('wdNotes');
    notes.replaceChildren();
    for (const key of ['cache', 'never', 'public', 'display']) notes.append(el('li', '', t(`worlddesk.note.${key}`)));
    const facts = $('wdWeatherFacts');
    facts.replaceChildren();
    for (const [term, value] of [
      [t('worlddesk.fact.provider'), data.provider_label],
      [t('worlddesk.fact.key'), data.key_present ? t('worlddesk.present') : t('worlddesk.missing')],
      [t('worlddesk.fact.location'), `${data.location.name} · ${data.location.latitude}, ${data.location.longitude}`],
      [t('worlddesk.fact.news'), t('worlddesk.every', {window: duration(data.refresh.news)})],
      [t('worlddesk.fact.crypto'), t('worlddesk.every', {window: duration(data.refresh.crypto)})],
      [t('worlddesk.fact.quakes'), t('worlddesk.every', {window: duration(data.refresh.quakes)})],
      [t('worlddesk.fact.weather'), t('worlddesk.every', {window: duration(data.refresh.weather)})],
    ]) {
      facts.append(el('span', 'memory-fact-term', term), el('span', 'memory-fact-value', String(value)));
    }
  }

  function paint() {
    paintSummary();
    paintRows();
  }

  async function load() {
    state.overview = await api('/api/world/overview');
    state.readAt = Date.now();
    paint();
  }

  /* ── the one control that leaves the machine ────────────────────────── */
  function closeAction() {
    host.hidden = true;
    host.replaceChildren();
  }
  function openAction() {
    const data = state.overview;
    if (!data) return;
    const keyed = data.key_present || data.provider !== 'weatherapi';
    host.hidden = false;
    host.innerHTML = `
      <section class="security-card security-confirm" role="group" aria-labelledby="wdActionTitle">
        <div class="security-cardhead"><h2 id="wdActionTitle">${esc(t('worlddesk.refreshTitle'))}</h2></div>
        <p class="security-note">${esc(t('worlddesk.refreshDetail'))}</p>
        <div class="worlddesk-groups" role="group" aria-label="${esc(t('worlddesk.colGroup'))}">
          ${GROUPS.map((group) => `<label class="worlddesk-groupcheck"><input type="checkbox" id="wdGroup-${group}" value="${group}"
            ${group === 'weather' && !keyed ? '' : 'checked'} ${group === 'weather' && !keyed ? 'disabled' : ''}>
            ${esc(t(`worlddesk.group.${group}`))}</label>`).join('')}
        </div>
        ${keyed ? '' : `<p class="security-note worlddesk-hint" id="wdWeatherHint">${esc(t('worlddesk.hint.weather'))}</p>`}
        <p class="security-code" id="wdActionExpected">${esc(TOKEN)}</p>
        <label class="security-field"><span data-i18n="worlddesk.tokenLabel"></span>
          <input id="wdActionToken" type="text" autocomplete="off" spellcheck="false" autocapitalize="characters"></label>
        <div class="security-confirmrow">
          <button class="btn" type="button" id="wdActionGo" disabled><span class="btn-label" data-i18n="worlddesk.confirm"></span></button>
          <button class="btn ghost" type="button" id="wdActionCancel"><span class="btn-label" data-i18n="worlddesk.cancel"></span></button>
        </div>
        <p class="security-note" id="wdActionError" role="alert"></p>
      </section>`;
    i18n.apply(host);
    const input = $('wdActionToken');
    const go = $('wdActionGo');
    input.oninput = () => { go.disabled = input.value.trim() !== TOKEN; };
    input.focus();
    go.onclick = async () => {
      const groups = GROUPS.filter((group) => $(`wdGroup-${group}`)?.checked);
      if (!groups.length) {
        $('wdActionError').textContent = t('worlddesk.pickGroup');
        return;
      }
      go.disabled = true;
      $('wdActionError').textContent = '';
      state.busy = true;
      try {
        const result = await api('/api/world/refresh', {confirmation: TOKEN, groups});
        state.overview = result.overview;
        state.readAt = Date.now();
        paint();
        closeAction();
        const failed = Object.values(result.results).filter((row) => row.status === 'offline' || row.status === 'error');
        window.nexusToast?.(
          failed.length
            ? t('worlddesk.toastPartial', {failed: failed.length, groups: result.groups.length})
            : t('worlddesk.toastDone', {count: result.groups.length, seconds: result.seconds}),
          {tone: failed.length ? 'warn' : 'done'});
        window.dispatchEvent(new CustomEvent('nexus:world-settings'));
      } catch (error) {
        $('wdActionError').textContent = message(error);
        go.disabled = false;
      } finally {
        state.busy = false;
      }
    };
    $('wdActionCancel').onclick = closeAction;
  }

  /* ── wiring ─────────────────────────────────────────────────────────── */
  $('wdReload').onclick = () => guard(load);
  $('wdRefresh').onclick = () => guard(async () => { if (!state.overview) await load(); openAction(); });
  i18n.onChange(() => {
    i18n.apply(root);
    i18n.apply(host);
    paint();
  });

  let poll = 0;
  const startPolling = () => {
    window.clearInterval(poll);
    poll = window.setInterval(() => {
      if (!state.visible || document.hidden || state.busy) return;
      if (Date.now() - state.readAt < 20000) return;
      load().catch(() => {});
    }, 20000);
  };
  window.addEventListener('nexus:view', (event) => {
    const on = event.detail === 'world';
    state.visible = on;
    if (!on) {
      window.clearInterval(poll);
      return;
    }
    startPolling();
    // The globe fetches as soon as it loads; read the desk again after it lands
    // so the timestamps describe the data actually on screen.
    guard(load).then(() => setTimeout(() => { if (state.visible) load().catch(() => {}); }, 3000));
  });

  // The World settings shortcut existed before this header did; keep the same
  // button and id, but place it with the other tools instead of floating above.
  function adoptSettings() {
    const shortcut = $('guiWorldSettings');
    if (shortcut && shortcut.parentElement !== $('wdTools')) {
      shortcut.classList.add('btn', 'ghost');
      $('wdTools').prepend(shortcut);
    }
  }
  adoptSettings();
  new MutationObserver(adoptSettings).observe(view, {childList: true});

  window.nexusWorldDesk = {
    root,
    load,
    state: () => ({
      enabled: state.overview?.enabled ?? null,
      fresh: state.overview?.totals?.fresh ?? null,
      feeds: state.overview?.totals?.feeds ?? null,
      headlines: state.overview?.totals?.headlines ?? null,
      provider: state.overview?.provider ?? null,
      keyPresent: state.overview?.key_present ?? null,
    }),
  };
})();
