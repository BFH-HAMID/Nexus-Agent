/* Alerts & channels: what happened, how you are told, and how this page fits a phone.

   The list is the real notice store the Jarvis inbox already owns: a run that
   finishes, fails or stops for approval appends one row. Delivery is the
   authoritative Jarvis policy (off / local / native, quiet, DND). Voice is the
   browser's own speech synthesis — no audio is recorded, nothing is uploaded,
   and if the browser has no voice the page says so instead of pretending.
   Mobile-lite is a display preference; it can never change what an agent does. */
(() => {
  'use strict';

  const gui = window.nexusGui;
  const i18n = window.nexusI18n;
  if (!gui || !i18n) return;
  const t = i18n.t;
  const $ = (id) => document.getElementById(id);

  const TABS = ['inbox', 'delivery', 'telegram', 'voice', 'mobile'];
  const TOKENS = {save: 'SAVE ALERT POLICY', pull: 'PULL NOTICES', push: 'SEND OS ALERT'};
  const TRANSPORTS = ['off', 'local', 'native'];
  const READS = ['digest', 'latest', 'notice'];

  const state = {
    tab: 'inbox', visible: false, overview: null, inbox: null, delivery: null,
    telegram: null, preferences: null, voices: [], reading: null,
  };

  const root = document.createElement('section');
  root.id = 'view-channels';
  root.className = 'gui-modules security-page channels-page';
  root.dataset.guiView = 'channels';
  root.hidden = gui.view !== 'channels';
  root.innerHTML = `
    <header class="security-head">
      <div class="security-headtext">
        <p class="security-eyebrow" data-i18n="channels.eyebrow"></p>
        <h1 data-i18n="channels.title"></h1>
        <p class="security-sub" data-i18n="channels.subtitle"></p>
      </div>
      <div class="security-tools">
        <button class="btn" type="button" id="chRefresh"><span class="btn-label" data-i18n="channels.refresh"></span></button>
        <span class="security-stamp" id="chUpdated"></span>
      </div>
    </header>
    <div class="security-tabs" role="tablist" aria-label="Alerts and channels">
      <button type="button" role="tab" id="chTabInbox" data-ch-tab="inbox" aria-selected="true" aria-controls="chPanelInbox" data-i18n="channels.tabInbox"></button>
      <button type="button" role="tab" id="chTabDelivery" data-ch-tab="delivery" aria-selected="false" aria-controls="chPanelDelivery" data-i18n="channels.tabDelivery"></button>
      <button type="button" role="tab" id="chTabTelegram" data-ch-tab="telegram" aria-selected="false" aria-controls="chPanelTelegram" data-i18n="channels.tabTelegram"></button>
      <button type="button" role="tab" id="chTabVoice" data-ch-tab="voice" aria-selected="false" aria-controls="chPanelVoice" data-i18n="channels.tabVoice"></button>
      <button type="button" role="tab" id="chTabMobile" data-ch-tab="mobile" aria-selected="false" aria-controls="chPanelMobile" data-i18n="channels.tabMobile"></button>
      <span class="security-chip" id="chChip" role="status" aria-live="polite">—</span>
    </div>
    <p class="security-banner" id="chError" role="alert" hidden></p>

    <div class="security-body">
      <section class="security-panel" id="chPanelInbox" role="tabpanel" aria-labelledby="chTabInbox" data-ch-panel="inbox">
        <div class="security-kpis" id="chKpis"></div>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="channels.inbox.title"></h2>
            <span class="security-stamp" id="chInboxStamp"></span>
          </div>
          <p class="security-note" data-i18n="channels.inbox.intro"></p>
          <div class="security-row">
            <button class="btn ghost" type="button" id="chReadAll"><span class="btn-label" data-i18n="channels.inbox.readAll"></span></button>
            <button class="btn ghost" type="button" id="chPull"><span class="btn-label" data-i18n="channels.inbox.pull"></span></button>
          </div>
          <ul class="channels-list" id="chRows"></ul>
        </section>
        <section class="security-card">
          <div class="security-cardhead"><h2 data-i18n="channels.inbox.pendingTitle"></h2></div>
          <p class="security-note" data-i18n="channels.inbox.pendingIntro"></p>
          <div class="memory-facts" id="chPendingFacts"></div>
        </section>
      </section>

      <section class="security-panel" id="chPanelDelivery" role="tabpanel" aria-labelledby="chTabDelivery" data-ch-panel="delivery" hidden>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="channels.delivery.title"></h2>
            <span class="security-stamp" id="chDeliveryStamp"></span>
          </div>
          <p class="security-note" data-i18n="channels.delivery.intro"></p>
          <div class="security-grid">
            <label class="security-field"><span data-i18n="channels.delivery.transport"></span>
              <select id="chTransport"></select></label>
            <label class="security-field"><span data-i18n="channels.delivery.dnd"></span>
              <input id="chDnd" type="number" min="0" max="1440" step="15" inputmode="numeric"></label>
          </div>
          <label class="security-switch"><input type="checkbox" id="chQuiet"><span data-i18n="channels.delivery.quiet"></span></label>
          <div class="security-row">
            <button class="btn" type="button" id="chSaveDelivery"><span class="btn-label" data-i18n="channels.delivery.save"></span></button>
            <button class="btn ghost" type="button" id="chPush"><span class="btn-label" data-i18n="channels.delivery.push"></span></button>
          </div>
          <ul class="security-notes" id="chDeliveryNotes"></ul>
          <div class="memory-facts" id="chDeliveryFacts"></div>
        </section>
        <section class="security-card">
          <div class="security-cardhead"><h2 data-i18n="channels.delivery.howTitle"></h2></div>
          <p class="security-note" data-i18n="channels.delivery.howIntro"></p>
        </section>
      </section>

      <section class="security-panel" id="chPanelTelegram" role="tabpanel" aria-labelledby="chTabTelegram" data-ch-panel="telegram" hidden>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="channels.telegram.title"></h2>
            <span class="security-stamp" id="chTelegramPhase"></span>
          </div>
          <p class="security-note" data-i18n="channels.telegram.intro"></p>
          <ul class="channels-checks" id="chTelegramChecks"></ul>
          <div class="security-row">
            <button class="btn" type="button" id="chTelegramStart"><span class="btn-label" data-i18n="channels.telegram.start"></span></button>
            <button class="btn ghost" type="button" id="chTelegramStop"><span class="btn-label" data-i18n="channels.telegram.stop"></span></button>
            <button class="btn ghost" type="button" id="chTelegramRefresh"><span class="btn-label" data-i18n="channels.telegram.refresh"></span></button>
          </div>
          <div class="memory-facts" id="chTelegramFacts"></div>
          <ul class="security-notes" id="chTelegramNotes"></ul>
        </section>
      </section>

      <section class="security-panel" id="chPanelVoice" role="tabpanel" aria-labelledby="chTabVoice" data-ch-panel="voice" hidden>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="channels.voice.title"></h2>
            <span class="security-stamp" id="chVoiceStamp"></span>
          </div>
          <p class="security-note" data-i18n="channels.voice.intro"></p>
          <label class="security-switch"><input type="checkbox" id="chVoiceEnabled"><span data-i18n="channels.voice.enabled"></span></label>
          <div class="security-grid">
            <label class="security-field"><span data-i18n="channels.voice.voice"></span>
              <select id="chVoiceName"></select></label>
            <label class="security-field"><span data-i18n="channels.voice.read"></span>
              <select id="chVoiceRead"></select></label>
            <label class="security-field"><span data-i18n="channels.voice.rate"></span>
              <span class="channels-scalerow"><input id="chVoiceRate" type="range" min="0.5" max="2" step="0.05"
                aria-describedby="chVoiceRateValue"><output id="chVoiceRateValue" for="chVoiceRate">1.00</output></span></label>
            <label class="security-field"><span data-i18n="channels.voice.pitch"></span>
              <span class="channels-scalerow"><input id="chVoicePitch" type="range" min="0" max="2" step="0.05"
                aria-describedby="chVoicePitchValue"><output id="chVoicePitchValue" for="chVoicePitch">1.00</output></span></label>
          </div>
          <div class="security-row">
            <button class="btn" type="button" id="chVoiceSpeak"><span class="btn-label" data-i18n="channels.voice.speak"></span></button>
            <button class="btn ghost" type="button" id="chVoiceStop"><span class="btn-label" data-i18n="channels.voice.stop"></span></button>
            <button class="btn ghost" type="button" id="chSaveVoice"><span class="btn-label" data-i18n="channels.voice.save"></span></button>
          </div>
          <p class="security-note" id="chVoiceState" role="status"></p>
          <ul class="security-notes" id="chVoiceNotes"></ul>
        </section>
      </section>

      <section class="security-panel" id="chPanelMobile" role="tabpanel" aria-labelledby="chTabMobile" data-ch-panel="mobile" hidden>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="channels.mobile.title"></h2>
            <span class="security-stamp" id="chMobileStamp"></span>
          </div>
          <p class="security-note" data-i18n="channels.mobile.intro"></p>
          <label class="security-switch"><input type="checkbox" id="chMobileLite"><span data-i18n="channels.mobile.lite"></span></label>
          <label class="security-switch"><input type="checkbox" id="chMobileCards"><span data-i18n="channels.mobile.cards"></span></label>
          <label class="security-field channels-scale"><span data-i18n="channels.mobile.scale"></span>
            <span class="channels-scalerow"><input id="chMobileScale" type="range" min="0.85" max="1.3" step="0.05"
              aria-describedby="chMobileScaleValue">
            <output id="chMobileScaleValue" for="chMobileScale" aria-live="polite">1.00</output></span></label>
          <div class="security-row">
            <button class="btn" type="button" id="chSaveMobile"><span class="btn-label" data-i18n="channels.mobile.save"></span></button>
          </div>
          <p class="security-note" id="chMobileState" role="status"></p>
          <ul class="security-notes" id="chMobileNotes"></ul>
        </section>
      </section>
    </div>`;
  (document.querySelector('main.deck') || document.body).after(root);

  const host = document.createElement('div');
  host.id = 'chActionHost';
  host.hidden = true;
  root.querySelector('.security-toolbar, .security-tabs').after(host);

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
  const number = (value) => (value === null || value === undefined ? '—' : Number(value).toLocaleString());
  const stampOf = (value) => {
    if (!value) return '—';
    const date = new Date(value * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
  };
  const minutes = (seconds) => {
    const value = Math.max(0, Math.round(Number(seconds || 0) / 60));
    if (value >= 60) return `${Math.floor(value / 60)}h ${value % 60}m`;
    return `${value}m`;
  };
  function showError(error) {
    const banner = $('chError');
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

  /* ── tabs ───────────────────────────────────────────────────────────── */
  function switchTab(tab) {
    if (!TABS.includes(tab)) return;
    state.tab = tab;
    for (const name of TABS) {
      const panel = $(`chPanel${name[0].toUpperCase()}${name.slice(1)}`);
      const button = $(`chTab${name[0].toUpperCase()}${name.slice(1)}`);
      const on = name === tab;
      if (panel) panel.hidden = !on;
      if (button) button.setAttribute('aria-selected', String(on));
    }
    closeAction();
    if (!state.visible) return;
    if (tab === 'inbox') loadInbox().catch(showError);
    if (tab === 'delivery') loadDelivery().catch(showError);
    if (tab === 'telegram') loadTelegram().catch(showError);
    if (tab === 'voice') { loadVoice().catch(showError); paintVoice(); }
    if (tab === 'mobile') { loadMobile().catch(showError); paintMobile(); }
  }

  /* ── inbox ──────────────────────────────────────────────────────────── */
  function kpi(label, value, tone) {
    const node = el('div', 'security-count');
    if (tone) node.dataset.tone = tone;
    node.append(el('b', '', value), el('span', '', label));
    return node;
  }

  function paintInbox() {
    const inbox = state.inbox || state.overview?.inbox;
    if (!inbox) return;
    const digest = inbox.digest || {};
    $('chKpis').replaceChildren(
      kpi(t('channels.kpi.notices'), number(digest.total ?? 0)),
      kpi(t('channels.kpi.unread'), number(digest.unread ?? 0), (digest.unread || 0) > 0 ? 'warn' : ''),
      kpi(t('channels.kpi.run'), number((digest.states || {}).done || 0)),
      kpi(t('channels.kpi.error'), number((digest.states || {}).error || 0), (digest.states || {}).error ? 'warn' : ''),
      kpi(t('channels.kpi.approval'), number((digest.states || {}).awaiting_approval || 0)),
      kpi(t('channels.kpi.delivery'), t(`channels.transport.${state.delivery?.policy?.transport || 'local'}`)),
    );
    $('chInboxStamp').textContent = inbox.error || t('channels.observed', {when: stampOf(inbox.observed_at)});
    const list = $('chRows');
    list.replaceChildren();
    const rows = inbox.items || [];
    if (!rows.length) {
      list.append(el('li', 'security-empty', t('channels.inbox.empty')));
    }
    for (const row of rows.slice(0, 60)) {
      const item = el('li', 'channels-row');
      item.dataset.kind = row.kind;
      item.dataset.state = row.state || row.kind || '';
      item.dataset.read = row.read ? '1' : '0';
      const dot = el('span', 'channels-dot');
      const body = el('div', 'channels-rowbody');
      body.append(el('b', 'channels-rowtitle', row.title || '—'));
      if (row.detail) body.append(el('span', 'channels-rowdetail', row.detail));
      const meta = el('div', 'channels-rowmeta');
      const chip = el('span', 'security-chip', row.state || row.kind);
      chip.dataset.tone = row.state === 'error' ? 'error' : row.state === 'awaiting_approval' ? 'warn' : 'ok';
      meta.append(chip, el('span', '', stampOf(row.created)));
      body.append(meta);
      const actions = el('div', 'channels-rowactions');
      if (!row.read) {
        const read = el('button', 'btn ghost small', t('channels.inbox.markRead'));
        read.type = 'button';
        read.onclick = () => guard(async () => {
          await api('/api/channels/ack', {ids: [row.id]});
          await loadInbox();
        });
        actions.append(read);
      }
      if (row.kind === 'approval' || row.state === 'awaiting_approval') {
        const open = el('button', 'btn ghost small', t('channels.inbox.openApprovals'));
        open.type = 'button';
        open.onclick = () => gui.show('approvals');
        actions.append(open);
      }
      if (speech() && state.preferences?.policy?.voice?.enabled) {
        const speak = el('button', 'btn ghost small', t('channels.inbox.readAloud'));
        speak.type = 'button';
        speak.onclick = () => speakText(`${row.title}. ${row.detail || ''}`);
        actions.append(speak);
      }
      item.append(dot, body);
      if (actions.childElementCount) item.append(actions);
      list.append(item);
    }
    const pending = inbox.pending || {};
    const facts = $('chPendingFacts');
    facts.replaceChildren();
    for (const [term, value] of [
      [t('channels.inbox.pendingJobs'), number(pending?.jobs ?? 0)],
      [t('channels.inbox.pendingActions'), number(pending?.actions ?? 0)],
      [t('channels.inbox.quietNow'), (state.delivery?.quiet_now ? t('channels.on') : t('channels.off'))],
      [t('channels.inbox.source'), inbox.note || '—'],
    ]) {
      facts.append(el('span', 'memory-fact-term', term), el('span', 'memory-fact-value', String(value)));
    }
  }

  async function loadInbox() {
    const [inbox, delivery] = await Promise.all([
      api('/api/channels/inbox?limit=60'),
      state.delivery ? Promise.resolve(state.delivery) : api('/api/channels/delivery'),
    ]);
    state.inbox = inbox;
    state.delivery = delivery;
    paintInbox();
    paintChip();
  }

  async function markAll() {
    const rows = (state.inbox?.items || []).filter((row) => !row.read).map((row) => row.id);
    if (!rows.length) return;
    await api('/api/channels/ack', {ids: rows});
    await loadInbox();
    window.nexusToast?.(t('channels.inbox.markedAll', {count: rows.length}), {tone: 'done'});
  }

  /* ── delivery ───────────────────────────────────────────────────────── */
  function paintDelivery() {
    const data = state.delivery;
    if (!data) return;
    const policy = data.policy || {};
    const select = $('chTransport');
    if (!select.options.length) {
      for (const name of TRANSPORTS) {
        const option = document.createElement('option');
        option.value = name;
        option.textContent = t(`channels.transport.${name}`);
        select.append(option);
      }
    }
    select.value = policy.transport || 'local';
    $('chQuiet').checked = Boolean(policy.quiet);
    $('chDnd').value = String(Math.round((policy.dnd_remaining_s || 0) / 60));
    $('chDeliveryStamp').textContent = data.quiet_now
      ? t('channels.delivery.suppressed', {left: minutes(policy.dnd_remaining_s)})
      : t('channels.delivery.active');
    const notes = $('chDeliveryNotes');
    notes.replaceChildren();
    for (const key of ['quiet', 'native', 'os']) notes.append(el('li', '', t(`channels.delivery.notes.${key}`)));
    if (!data.native?.present) notes.append(el('li', '', t('channels.delivery.notes.noProgram', {program: data.native?.program || '—'})));
    const facts = $('chDeliveryFacts');
    facts.replaceChildren();
    const last = policy.last_push_result;
    for (const [term, value] of [
      [t('channels.delivery.counters'), `${number(state.inbox?.digest?.total ?? 0)} / ${number(state.inbox?.digest?.unread ?? 0)}`],
      [t('channels.delivery.lastPush'), policy.last_push_at ? stampOf(policy.last_push_at) : '—'],
      [t('channels.delivery.lastResult'), last ? (last.submitted ? t('channels.delivery.submitted') : t('channels.delivery.refused')) : '—'],
      [t('channels.delivery.program'), data.native?.present ? data.native.program : t('channels.delivery.missing')],
      [t('channels.delivery.quietUntil'), policy.dnd_remaining_s ? minutes(policy.dnd_remaining_s) : '—'],
    ]) {
      facts.append(el('span', 'memory-fact-term', term), el('span', 'memory-fact-value', String(value)));
    }
  }

  async function loadDelivery() {
    state.delivery = await api('/api/channels/delivery');
    if (!state.inbox) state.inbox = await api('/api/channels/inbox?limit=20');
    paintDelivery();
    paintChip();
  }

  /* ── telegram ───────────────────────────────────────────────────────── */
  function paintTelegram() {
    const data = state.telegram;
    if (!data) return;
    const runtime = data.runtime || {};
    $('chTelegramPhase').textContent = runtime.alive
      ? t('channels.telegram.polling', {when: runtime.last_poll_at ? stampOf(runtime.last_poll_at) : '—'})
      : t(`channels.telegram.phase.${runtime.phase || 'stopped'}`);
    const list = $('chTelegramChecks');
    list.replaceChildren();
    for (const check of data.checks || []) {
      const item = el('li', 'channels-check');
      item.dataset.ok = String(Boolean(check.ok));
      item.append(el('span', 'channels-checkmark', check.ok ? '✓' : '✕'),
        el('span', 'channels-checktext', check.label), el('small', '', check.hint));
      list.append(item);
    }
    const facts = $('chTelegramFacts');
    facts.replaceChildren();
    for (const [term, value] of [
      [t('channels.telegram.whitelist'), number(data.policy?.whitelist ?? 0)],
      [t('channels.telegram.pollSeconds'), data.policy?.poll_seconds ?? '—'],
      [t('channels.telegram.started'), runtime.started_at ? stampOf(runtime.started_at) : '—'],
      [t('channels.telegram.ready'), data.ready ? t('channels.yes') : t('channels.no')],
    ]) {
      facts.append(el('span', 'memory-fact-term', term), el('span', 'memory-fact-value', String(value)));
    }
    $('chTelegramStart').disabled = !data.ready || Boolean(runtime.alive);
    $('chTelegramStop').disabled = !runtime.alive;
    const notes = $('chTelegramNotes');
    notes.replaceChildren();
    notes.append(el('li', '', data.note || ''));
    if (runtime.error) notes.append(el('li', '', runtime.error));
    if (runtime.scope) notes.append(el('li', '', runtime.scope));
    if (data.policy?.error) notes.append(el('li', '', t('channels.telegram.policyBroken')));
  }

  async function loadTelegram() {
    state.telegram = await api('/api/channels/telegram');
    paintTelegram();
    paintChip();
  }

  /* ── voice (browser speech, never a recording) ──────────────────────── */
  const speech = () => (typeof window.speechSynthesis === 'object' && typeof window.SpeechSynthesisUtterance === 'function'
    ? window.speechSynthesis : null);
  function loadVoices() {
    const engine = speech();
    if (!engine) { state.voices = []; return; }
    state.voices = engine.getVoices() || [];
  }
  function speakText(text) {
    const engine = speech();
    if (!engine) { $('chVoiceState').textContent = t('channels.voice.unsupported'); return false; }
    const prefs = (state.preferences?.policy?.voice) || {};
    if (prefs.enabled === false) { $('chVoiceState').textContent = t('channels.voice.disabled'); return false; }
    const trimmed = String(text || '').trim();
    if (!trimmed) { $('chVoiceState').textContent = t('channels.voice.nothing'); return false; }
    engine.cancel();
    const say = new SpeechSynthesisUtterance(trimmed.slice(0, 600));
    const chosen = state.voices.find((voice) => voice.name === prefs.voice);
    if (chosen) say.voice = chosen;
    say.rate = Number(prefs.rate) || 1;
    say.pitch = Number(prefs.pitch) || 1;
    say.onstart = () => { $('chVoiceState').textContent = t('channels.voice.speaking'); };
    say.onend = () => { $('chVoiceState').textContent = t('channels.voice.finished'); };
    say.onerror = () => { $('chVoiceState').textContent = t('channels.voice.failed'); };
    engine.speak(say);
    return true;
  }
  function digestLine() {
    const digest = state.inbox?.digest || {};
    const transport = state.delivery?.policy?.transport || 'local';
    return t('channels.voice.digestText', {
      total: digest.total ?? 0, unread: digest.unread ?? 0,
      transport: t(`channels.transport.${transport}`)});
  }
  function paintVoice() {
    loadVoices();
    const engine = speech();
    const prefs = (state.preferences?.policy?.voice) || {};
    $('chVoiceEnabled').checked = Boolean(prefs.enabled);
    const names = $('chVoiceName');
    names.replaceChildren();
    const auto = document.createElement('option');
    auto.value = '';
    auto.textContent = t('channels.voice.systemDefault');
    names.append(auto);
    for (const voice of state.voices.slice(0, 200)) {
      const option = document.createElement('option');
      option.value = voice.name;
      option.textContent = `${voice.name} · ${voice.lang}${voice.localService ? ' · ' + t('channels.voice.local') : ''}`;
      names.append(option);
    }
    names.value = prefs.voice || '';
    const read = $('chVoiceRead');
    if (!read.options.length) {
      for (const mode of READS) {
        const option = document.createElement('option');
        option.value = mode;
        option.textContent = t(`channels.voice.mode.${mode}`);
        read.append(option);
      }
    }
    read.value = prefs.read || 'digest';
    $('chVoiceRate').value = String(prefs.rate ?? 1);
    $('chVoicePitch').value = String(prefs.pitch ?? 1);
    $('chVoiceRateValue').textContent = Number($('chVoiceRate').value).toFixed(2);
    $('chVoicePitchValue').textContent = Number($('chVoicePitch').value).toFixed(2);
    $('chVoiceStamp').textContent = engine
      ? t('channels.voice.count', {count: state.voices.length})
      : t('channels.voice.unsupported');
    $('chVoiceSpeak').disabled = !engine;
    $('chVoiceStop').disabled = !engine;
    const notes = $('chVoiceNotes');
    notes.replaceChildren();
    for (const key of (engine ? ['privacy', 'browser', 'noMicrophone'] : ['unsupported', 'noMicrophone'])) {
      notes.append(el('li', '', t(`channels.voice.notes.${key}`)));
    }
  }

  async function loadVoice() {
    if (!state.preferences) state.preferences = await api('/api/channels/policy');
    if (!state.inbox) state.inbox = await api('/api/channels/inbox?limit=1');
  }

  /* ── mobile-lite ────────────────────────────────────────────────────── */
  function paintMobile() {
    const prefs = (state.preferences?.policy?.mobile) || {};
    $('chMobileLite').checked = Boolean(prefs.lite);
    $('chMobileCards').checked = prefs.cards !== false;
    $('chMobileScale').value = String(prefs.font_scale ?? 1);
    $('chMobileScaleValue').textContent = Number($('chMobileScale').value).toFixed(2);
    $('chMobileStamp').textContent = window.nexusMobile?.active()
      ? t('channels.mobile.on') : t('channels.mobile.off');
    $('chMobileState').textContent = t('channels.mobile.state', {
      width: window.innerWidth, scale: Number($('chMobileScale').value).toFixed(2)});
    const notes = $('chMobileNotes');
    notes.replaceChildren();
    for (const key of ['scope', 'tables', 'revert']) notes.append(el('li', '', t(`channels.mobile.notes.${key}`)));
  }

  async function loadMobile() {
    if (!state.preferences) state.preferences = await api('/api/channels/policy');
  }

  /* ── chip ───────────────────────────────────────────────────────────── */
  function paintChip() {
    const digest = state.inbox?.digest || {};
    const deliveryPolicy = state.delivery?.policy || {};
    const telegram = state.telegram?.runtime || {};
    $('chChip').textContent = t('channels.chip', {
      unread: digest.unread ?? 0,
      transport: t(`channels.transport.${deliveryPolicy.transport || 'local'}`),
      telegram: telegram.alive ? t('channels.telegram.shortPolling') : t(`channels.telegram.phase.${telegram.phase || 'stopped'}`),
    });
  }

  /* ── confirm sheet ──────────────────────────────────────────────────── */
  function closeAction() {
    host.hidden = true;
    host.replaceChildren();
  }
  function openAction({title, token, detail, onConfirm}) {
    host.hidden = false;
    host.innerHTML = `
      <section class="security-card security-confirm" role="group" aria-labelledby="chActionTitle">
        <div class="security-cardhead"><h2 id="chActionTitle">${esc(title)}</h2></div>
        <p class="security-note">${esc(detail || '')}</p>
        <p class="security-code" id="chActionExpected">${esc(token)}</p>
        <label class="security-field"><span data-i18n="channels.confirm.tokenLabel"></span>
          <input id="chActionToken" type="text" autocomplete="off" spellcheck="false" autocapitalize="characters"></label>
        <div class="security-confirmrow">
          <button class="btn" type="button" id="chActionGo" disabled><span class="btn-label" data-i18n="channels.confirm.go"></span></button>
          <button class="btn ghost" type="button" id="chActionCancel"><span class="btn-label" data-i18n="channels.confirm.cancel"></span></button>
        </div>
        <p class="security-note" id="chActionError" role="alert"></p>
      </section>`;
    i18n.apply(host);
    const input = $('chActionToken');
    const go = $('chActionGo');
    input.focus();
    input.oninput = () => { go.disabled = input.value.trim() !== token; };
    go.onclick = async () => {
      if (go.disabled) return;
      go.disabled = true;
      $('chActionError').textContent = '';
      try {
        await onConfirm({confirmation: token});
        closeAction();
      } catch (error) {
        $('chActionError').textContent = message(error);
        go.disabled = false;
      }
    };
    $('chActionCancel').onclick = closeAction;
    host.scrollIntoView({block: 'nearest'});
  }

  /* ── wiring ─────────────────────────────────────────────────────────── */
  for (const tab of TABS) {
    const button = $(`chTab${tab[0].toUpperCase()}${tab.slice(1)}`);
    if (button) button.onclick = () => switchTab(tab);
  }
  $('chRefresh').onclick = () => guard(async () => {
    state.inbox = state.delivery = state.telegram = state.preferences = null;
    await loadOverview();
    switchTab(state.tab);
  });
  $('chReadAll').onclick = () => guard(markAll);
  $('chPull').onclick = () => openAction({
    title: t('channels.inbox.pull'),
    token: TOKENS.pull,
    detail: t('channels.inbox.pullDetail'),
    onConfirm: async (payload) => {
      const result = await api('/api/channels/notify', payload);
      state.delivery = result.delivery || state.delivery;
      await loadInbox();
      window.nexusToast?.(result.suppressed
        ? t('channels.delivery.suppressedToast')
        : t('channels.inbox.pulled', {count: result.count ?? 0}), {tone: 'done'});
    },
  });
  $('chSaveDelivery').onclick = () => openAction({
    title: t('channels.delivery.saveTitle'),
    token: TOKENS.save,
    detail: t('channels.delivery.saveDetail', {
      transport: t(`channels.transport.${$('chTransport').value}`), dnd: $('chDnd').value || '0'}),
    onConfirm: async (payload) => {
      state.delivery = await api('/api/channels/delivery/save', {
        ...payload, transport: $('chTransport').value, quiet: $('chQuiet').checked,
        dnd_minutes: Math.max(0, Math.min(1440, Number($('chDnd').value) || 0)),
        revision: state.delivery?.revision || ''});
      paintDelivery();
      paintChip();
      window.nexusToast?.(t('channels.delivery.saved'), {tone: 'done'});
    },
  });
  $('chPush').onclick = () => openAction({
    title: t('channels.delivery.push'),
    token: TOKENS.push,
    detail: t('channels.delivery.pushDetail'),
    onConfirm: async (payload) => {
      const result = await api('/api/channels/push', payload);
      state.delivery = result.delivery || state.delivery;
      paintDelivery();
      const submitted = Boolean(result.result?.submitted);
      window.nexusToast?.(submitted ? t('channels.delivery.pushed')
        : t('channels.delivery.pushRefused', {reason: result.result?.error_type || t('channels.delivery.suppressedReason')}),
      {tone: submitted ? 'done' : 'warn'});
    },
  });
  $('chTelegramRefresh').onclick = () => guard(loadTelegram);
  for (const [operation, token, id] of [['telegram-start', 'START TELEGRAM POLLING', 'chTelegramStart'],
    ['telegram-stop', 'STOP TELEGRAM POLLING', 'chTelegramStop']]) {
    $(id).onclick = () => openAction({
      title: t(operation === 'telegram-start' ? 'channels.telegram.startTitle' : 'channels.telegram.stopTitle'),
      token,
      detail: t('channels.telegram.confirmDetail'),
      onConfirm: async (payload) => {
        state.telegram = await api('/api/channels/telegram', {...payload, operation,
          revision: state.telegram?.policy?.revision || ''});
        paintTelegram();
        paintChip();
      },
    });
  }
  $('chVoiceSpeak').onclick = () => guard(() => {
    const mode = $('chVoiceRead').value;
    if (mode === 'latest') {
      const latest = (state.inbox?.items || [])[0];
      speakText(latest ? `${latest.title}. ${latest.detail || ''}` : t('channels.voice.nothing'));
      return;
    }
    speakText(digestLine());
  });
  $('chVoiceStop').onclick = () => { speech()?.cancel(); $('chVoiceState').textContent = t('channels.voice.stopped'); };
  $('chSaveVoice').onclick = () => guard(async () => {
    state.preferences = await api('/api/channels/policy/save', {
      revision: state.preferences?.revision || '',
      voice: {enabled: $('chVoiceEnabled').checked, voice: $('chVoiceName').value,
        rate: Number($('chVoiceRate').value), pitch: Number($('chVoicePitch').value),
        read: $('chVoiceRead').value}});
    paintVoice();
    window.nexusToast?.(t('channels.voice.saved'), {tone: 'done'});
  });
  $('chSaveMobile').onclick = () => guard(async () => {
    state.preferences = await api('/api/channels/policy/save', {
      revision: state.preferences?.revision || '',
      mobile: {lite: $('chMobileLite').checked, cards: $('chMobileCards').checked,
        font_scale: Number($('chMobileScale').value)}});
    window.nexusMobile?.apply(state.preferences.policy.mobile);
    paintMobile();
    window.nexusToast?.(t('channels.mobile.saved'), {tone: 'done'});
  });
  const live = (input, output) => {
    const node = $(input);
    node?.addEventListener('input', () => { $(output).textContent = Number(node.value).toFixed(2); });
  };
  live('chVoiceRate', 'chVoiceRateValue');
  live('chVoicePitch', 'chVoicePitchValue');
  live('chMobileScale', 'chMobileScaleValue');
  $('chMobileScale')?.addEventListener('input', () => {
    $('chMobileState').textContent = t('channels.mobile.state', {
      width: window.innerWidth, scale: Number($('chMobileScale').value).toFixed(2)});
  });
  if (speech()) speech().addEventListener?.('voiceschanged', () => { loadVoices(); paintVoice(); });

  i18n.onChange(() => {
    i18n.apply(root);
    i18n.apply(host);
    paintInbox();
    paintDelivery();
    paintTelegram();
    paintVoice();
    paintMobile();
    paintChip();
  });

  window.addEventListener('nexus:view', (event) => {
    if (event.detail !== 'channels') { state.visible = false; return; }
    const first = !state.visible;
    state.visible = true;
    if (first || !state.overview) {
      loadOverview().catch(showError);
    } else {
      switchTab(state.tab);
    }
  });
  window.addEventListener('nexus:event', ({detail}) => {
    if (!state.visible || !['done', 'error', 'approval_request'].includes(detail?.type)) return;
    loadInbox().catch(() => {});
  });

  async function loadOverview() {
    const overview = await api('/api/channels/overview');
    state.overview = overview;
    state.inbox = overview.inbox;
    state.delivery = overview.delivery;
    state.telegram = overview.telegram;
    state.preferences = overview.preferences;
    $('chUpdated').textContent = t('channels.observed', {when: stampOf(overview.observed_at)});
    paintInbox();
    paintDelivery();
    paintTelegram();
    paintVoice();
    paintMobile();
    paintChip();
    switchTab(state.tab);
  }

  switchTab(TABS.includes(new URLSearchParams(location.search).get('tab'))
    ? new URLSearchParams(location.search).get('tab') : 'inbox');

  window.nexusChannels = {
    root,
    tab: switchTab,
    load: loadOverview,
    speak: speakText,
    digest: digestLine,
    state: () => ({
      tab: state.tab,
      unread: state.inbox?.digest?.unread ?? null,
      transport: state.delivery?.policy?.transport ?? null,
      telegram: state.telegram?.runtime?.phase ?? null,
      ready: state.telegram?.ready ?? null,
      voices: state.voices.length,
      lite: state.preferences?.policy?.mobile?.lite ?? null,
    }),
  };
})();
