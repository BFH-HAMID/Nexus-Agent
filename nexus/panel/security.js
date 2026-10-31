/* Security Center: the trail, the sessions, the vault and the policy.

   This page is an instrument, not an authority. It reads the same audit file
   the permission gate writes, lists the sessions the server actually issued,
   and edits the same permissions.yaml the gate loads. Every destructive action
   asks for its own typed token - and for a six-digit code once the operator has
   enrolled two-factor - so opening this page can never change a permission. */
(() => {
  'use strict';

  const gui = window.nexusGui;
  const i18n = window.nexusI18n;
  if (!gui || !i18n) return;
  const t = i18n.t;
  const $ = (id) => document.getElementById(id);

  const TABS = ['overview', 'audit', 'vault', 'access', 'sandbox'];
  const MODES = ['READ_ONLY', 'ASK_EVERY_TIME', 'AUTO_ALLOW'];
  const ACTION_GROUPS = ['security', 'permission', 'approval', 'workflow', 'computer', 'rolehub',
    'routerstudio', 'studio', 'palette', 'github'];

  const state = {
    tab: 'overview', overview: null, audit: null, chain: null, vault: null, sessions: null,
    twoFactor: null, policy: null, injection: null, findings: null, recent: [], showRevoked: false,
    snapshots: null, lastRead: 0,
    enrolment: null, recovery: null, busy: false,
  };

  const root = document.createElement('section');
  root.id = 'view-security';
  root.className = 'gui-modules security-page';
  root.dataset.guiView = 'security';
  root.hidden = gui.view !== 'security';
  root.innerHTML = `
    <header class="security-head">
      <div>
        <p class="security-eyebrow" data-i18n="security.eyebrow"></p>
        <h1 data-i18n="security.title"></h1>
        <p class="security-sub" data-i18n="security.subtitle"></p>
      </div>
      <div class="security-quick" role="group" aria-label="Security actions">
        <button class="btn" type="button" id="secRefresh"><span class="btn-label" data-i18n="security.refresh"></span></button>
        <button class="btn ghost" type="button" id="secOperations"><span class="btn-label" data-i18n="security.openOperations"></span></button>
        <span class="security-stamp" id="secUpdated" data-i18n="security.never"></span>
      </div>
    </header>

    <p class="security-banner" id="secError" role="alert" hidden></p>

    <div class="security-toolbar">
      <div class="security-tabs" role="tablist" aria-label="Security sections">
        <button type="button" role="tab" id="secTabOverview" data-sec-tab="overview" aria-selected="true" aria-controls="secPanelOverview" data-i18n="security.tabOverview"></button>
        <button type="button" role="tab" id="secTabAudit" data-sec-tab="audit" aria-selected="false" aria-controls="secPanelAudit" data-i18n="security.tabAudit"></button>
        <button type="button" role="tab" id="secTabVault" data-sec-tab="vault" aria-selected="false" aria-controls="secPanelVault" data-i18n="security.tabVault"></button>
        <button type="button" role="tab" id="secTabAccess" data-sec-tab="access" aria-selected="false" aria-controls="secPanelAccess" data-i18n="security.tabAccess"></button>
        <button type="button" role="tab" id="secTabSandbox" data-sec-tab="sandbox" aria-selected="false" aria-controls="secPanelSandbox" data-i18n="security.tabSandbox"></button>
      </div>
      <span class="security-chip" id="secChip" role="status" aria-live="polite">—</span>
    </div>

    <div class="security-body">
      <section class="security-panel" id="secPanelOverview" role="tabpanel" aria-labelledby="secTabOverview" data-sec-panel="overview">
        <div class="security-kpis" id="secKpis"></div>
        <div class="security-split">
          <section class="security-card">
            <div class="security-cardhead">
              <h2 data-i18n="security.anomalies.title"></h2>
              <span class="security-chip" id="secAnomalyCount"></span>
            </div>
            <p class="security-note" data-i18n="security.anomalies.intro"></p>
            <ul class="security-anomalies" id="secAnomalies"></ul>
          </section>
          <section class="security-card">
            <div class="security-cardhead"><h2 data-i18n="security.injection.title"></h2></div>
            <p class="security-injection" data-i18n="security.injection.banner"></p>
            <div class="security-row">
              <label class="security-field"><span data-i18n="security.injection.pick"></span>
                <select id="secScanPick"></select></label>
              <button class="btn" type="button" id="secScan"><span class="btn-label" data-i18n="security.injection.scan"></span></button>
            </div>
            <p class="security-note" id="secScanStatus" role="status"></p>
            <ul class="security-findings" id="secFindings"></ul>
            <p class="security-note" data-i18n="security.injection.caveat"></p>
          </section>
        </div>
        <section class="security-card">
          <div class="security-cardhead"><h2 data-i18n="security.notes.title"></h2></div>
          <ul class="security-notes" id="secNotes"></ul>
        </section>
      </section>

      <section class="security-panel" id="secPanelAudit" role="tabpanel" aria-labelledby="secTabAudit" data-sec-panel="audit" hidden>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="security.audit.title"></h2>
            <div class="security-row">
              <label class="security-field"><span data-i18n="security.audit.filter"></span>
                <select id="secAuditFilter">
                  <option value="" data-i18n="security.audit.all"></option>
                  <option value="security." data-i18n="security.audit.security"></option>
                  <option value="security.auth." data-i18n="security.audit.auth"></option>
                  <option value="security.session." data-i18n="security.audit.session"></option>
                  <option value="permission." data-i18n="security.audit.permission"></option>
                  <option value="security.vault." data-i18n="security.audit.vault"></option>
                  <option value="security.policy." data-i18n="security.audit.policy"></option>
                </select></label>
              <button class="btn" type="button" id="secVerify"><span class="btn-label" data-i18n="security.audit.verify"></span></button>
              <button class="btn ghost" type="button" id="secExport"><span class="btn-label" data-i18n="security.audit.export"></span></button>
            </div>
          </div>
          <div class="security-chain" id="secChain"></div>
          <div class="security-tablewrap">
            <table class="security-table">
              <thead><tr>
                <th data-i18n="security.audit.colTime"></th>
                <th data-i18n="security.audit.colKind"></th>
                <th data-i18n="security.audit.colAction"></th>
                <th data-i18n="security.audit.colTarget"></th>
                <th data-i18n="security.audit.colResult"></th>
                <th data-i18n="security.audit.colRisk"></th>
                <th data-i18n="security.audit.colSeq"></th>
              </tr></thead>
              <tbody id="secAuditRows"></tbody>
            </table>
          </div>
          <p class="security-note" data-i18n="security.audit.limits"></p>
        </section>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="security.snapshots.title"></h2>
            <div class="security-row">
              <button class="btn" type="button" id="secSnapshotTake"><span class="btn-label" data-i18n="security.snapshots.take"></span></button>
            </div>
          </div>
          <p class="security-note" data-i18n="security.snapshots.intro"></p>
          <div class="security-tablewrap security-tablewrap--snapshot">
            <table class="security-table">
              <thead><tr>
                <th data-i18n="security.snapshots.colWhen"></th>
                <th data-i18n="security.snapshots.colLines"></th>
                <th data-i18n="security.snapshots.colHead"></th>
                <th data-i18n="security.snapshots.colAction"></th>
              </tr></thead>
              <tbody id="secSnapshotRows"></tbody>
            </table>
          </div>
          <p class="security-note" id="secSnapshotNote"></p>
        </section>
      </section>

      <section class="security-panel" id="secPanelVault" role="tabpanel" aria-labelledby="secTabVault" data-sec-panel="vault" hidden>
        <div class="security-split security-split--vault">
          <section class="security-card">
            <div class="security-cardhead"><h2 data-i18n="security.vault.title"></h2></div>
            <p class="security-note" data-i18n="security.vault.intro"></p>
            <div class="security-tablewrap">
              <table class="security-table">
                <thead><tr>
                  <th data-i18n="security.vault.colName"></th>
                  <th data-i18n="security.vault.colSource"></th>
                  <th data-i18n="security.vault.colFingerprint"></th>
                  <th data-i18n="security.vault.colState"></th>
                  <th data-i18n="security.vault.colAction"></th>
                </tr></thead>
                <tbody id="secVaultRows"></tbody>
              </table>
            </div>
            <p class="security-note" data-i18n="security.vault.readOnlyNote"></p>
            <p class="security-note" id="secStoreLine"></p>
          </section>
          <section class="security-card">
            <div class="security-cardhead"><h2 data-i18n="security.vault.writeTitle"></h2></div>
            <p class="security-note" data-i18n="security.vault.writeIntro"></p>
            <label class="security-field"><span data-i18n="security.vault.provider"></span>
              <select id="secSecretName"></select></label>
            <label class="security-field"><span data-i18n="security.vault.value"></span>
              <input id="secSecretValue" type="password" autocomplete="off" spellcheck="false" maxlength="512"></label>
            <div class="security-row">
              <button class="btn" type="button" id="secSecretSave"><span class="btn-label" data-i18n="security.vault.save"></span></button>
            </div>
            <ul class="security-notes" id="secVaultNotes"></ul>
          </section>
        </div>
      </section>

      <section class="security-panel" id="secPanelAccess" role="tabpanel" aria-labelledby="secTabAccess" data-sec-panel="access" hidden>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="security.sessions.title"></h2>
            <div class="security-row">
              <label class="security-switch"><input type="checkbox" id="secShowRevoked"><span id="secShowRevokedLabel" data-i18n="security.sessions.showRevoked"></span></label>
              <button class="btn ghost" type="button" id="secRevokeOthers"><span class="btn-label" data-i18n="security.sessions.revokeOthers"></span></button>
              <button class="btn ghost" type="button" id="secRevokeAll"><span class="btn-label" data-i18n="security.sessions.revokeAll"></span></button>
            </div>
          </div>
          <p class="security-note" data-i18n="security.sessions.intro"></p>
          <div class="security-tablewrap">
            <table class="security-table">
              <thead><tr>
                <th data-i18n="security.sessions.colBrowser"></th>
                <th data-i18n="security.sessions.colHost"></th>
                <th data-i18n="security.sessions.colCreated"></th>
                <th data-i18n="security.sessions.colLast"></th>
                <th data-i18n="security.sessions.colExpires"></th>
                <th data-i18n="security.sessions.colState"></th>
                <th data-i18n="security.sessions.colAction"></th>
              </tr></thead>
              <tbody id="secSessionRows"></tbody>
            </table>
          </div>
          <p class="security-note" data-i18n="security.sessions.note"></p>
        </section>

        <section class="security-card" id="secTotpCard">
          <div class="security-cardhead">
            <h2 data-i18n="security.totp.title"></h2>
            <span class="security-chip" id="secTotpState"></span>
          </div>
          <div id="secTotpBody"></div>
        </section>
      </section>

      <section class="security-panel" id="secPanelSandbox" role="tabpanel" aria-labelledby="secTabSandbox" data-sec-panel="sandbox" hidden>
        <section class="security-card">
          <div class="security-cardhead">
            <h2 data-i18n="security.policy.title"></h2>
            <span class="security-chip" id="secLatch"></span>
          </div>
          <p class="security-injection" data-i18n="security.policy.warning"></p>
          <p class="security-note" id="secPolicyFile"></p>
          <fieldset class="security-modes">
            <legend data-i18n="security.policy.defaultLegend"></legend>
            <div class="security-radios" id="secDefaultModes"></div>
          </fieldset>
        </section>
        <section class="security-card">
          <div class="security-cardhead"><h2 data-i18n="security.policy.toolsTitle"></h2></div>
          <p class="security-note" data-i18n="security.policy.toolsIntro"></p>
          <div class="security-tablewrap security-tablewrap--policy">
            <table class="security-table security-table--policy">
              <thead><tr>
                <th data-i18n="security.policy.colTool"></th>
                <th data-i18n="security.policy.colMode"></th>
                <th data-i18n="security.policy.colForced"></th>
              </tr></thead>
              <tbody id="secToolRows"></tbody>
            </table>
          </div>
        </section>
        <section class="security-card">
          <div class="security-cardhead"><h2 data-i18n="security.policy.adaptersTitle"></h2></div>
          <p class="security-note" data-i18n="security.policy.adaptersIntro"></p>
          <div class="security-adapters" id="secAdapters"></div>
          <div class="security-row security-formpair">
            <label class="security-field"><span data-i18n="security.policy.backend"></span>
              <select id="secShellBackend">
                <option value="host">host</option>
                <option value="docker">docker</option>
              </select></label>
            <label class="security-field security-grow"><span data-i18n="security.policy.image"></span>
              <input id="secShellImage" type="text" autocomplete="off" spellcheck="false" maxlength="160"></label>
          </div>
          <div class="security-row">
            <button class="btn" type="button" id="secPolicySave"><span class="btn-label" data-i18n="security.policy.save"></span></button>
            <span class="security-stamp" id="secPolicyRevision"></span>
          </div>
          <ul class="security-notes">
            <li data-i18n="security.policy.noteSandbox"></li>
            <li data-i18n="security.policy.noteForced"></li>
            <li data-i18n="security.policy.noteOwner"></li>
          </ul>
        </section>
      </section>

      <div id="secActionHost" hidden></div>
    </div>`;
  (document.querySelector('main.deck') || document.body).after(root);

  /* ── small helpers ──────────────────────────────────────────────────── */
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  const message = (error) => error?.payload?.error?.message || error?.message || String(error);
  const clock = (value) => {
    if (!value) return '—';
    const date = new Date(value * 1000);
    return Number.isNaN(date.getTime()) ? '—'
      : date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  };
  const stamp = (value) => {
    if (!value) return '—';
    const date = new Date(value * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    const options = {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'};
    // A cookie can outlive the year; without this "Dec 5" reads as next month.
    if (date.getFullYear() !== new Date().getFullYear()) options.year = 'numeric';
    return date.toLocaleString([], options);
  };
  const iso = (value) => (value ? new Date(value * 1000).toISOString() : '');
  const shortHash = (value) => (value ? String(value).slice(0, 12) : '—');
  const bytes = (size) => {
    const n = Number(size) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1048576).toFixed(2)} MB`;
  };
  const group = (action) => {
    const name = String(action || '');
    const prefix = name.split('.')[0];
    return ACTION_GROUPS.includes(prefix) ? prefix : '';
  };
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  function showError(error) {
    const banner = $('secError');
    banner.textContent = error ? message(error) : '';
    banner.hidden = !error;
  }
  async function guard(work) {
    try { showError(null); await work(); } catch (error) { showError(error); }
  }

  /* ── tabs ───────────────────────────────────────────────────────────── */
  function switchTab(tab) {
    if (!TABS.includes(tab)) return;
    state.tab = tab;
    for (const name of TABS) {
      const panel = $(`secPanel${name[0].toUpperCase()}${name.slice(1)}`);
      const button = $(`secTab${name[0].toUpperCase()}${name.slice(1)}`);
      const on = name === tab;
      if (panel) panel.hidden = !on;
      if (button) button.setAttribute('aria-selected', String(on));
    }
    closeAction();
    paintChip();
    if (tab === 'audit') loadAudit().catch(showError);
    if (tab === 'vault') loadVault().catch(showError);
    if (tab === 'access') loadAccess().catch(showError);
    if (tab === 'sandbox') loadPolicy().catch(showError);
    if (tab === 'overview') refreshOverview().catch(showError);
  }

  /* ── the confirm sheet ──────────────────────────────────────────────── */
  function closeAction() {
    const host = $('secActionHost');
    host.hidden = true;
    host.replaceChildren();
  }

  /* Every write on this page goes through here: an exact typed token and,
     once enrolled, a live six-digit code. Nothing is sent before both agree. */
  function openAction({title, token, detail, needsCode, onConfirm, extraFields = ''}) {
    const host = $('secActionHost');
    const enrolled = Boolean(state.twoFactor?.enrolled) || needsCode;
    host.hidden = false;
    host.innerHTML = `
      <section class="security-card security-confirm" role="group" aria-labelledby="secActionTitle">
        <div class="security-cardhead"><h2 id="secActionTitle">${esc(title)}</h2></div>
        <p class="security-note">${esc(detail || '')}</p>
        ${extraFields}
        <p class="security-code" id="secActionExpected">${esc(token)}</p>
        <label class="security-field"><span data-i18n="security.confirm.tokenLabel"></span>
          <input id="secActionToken" type="text" autocomplete="off" spellcheck="false" autocapitalize="characters"></label>
        ${enrolled ? `<label class="security-field"><span data-i18n="security.confirm.codeLabel"></span>
          <input id="secActionCode" type="text" inputmode="numeric" autocomplete="one-time-code" spellcheck="false" maxlength="12"></label>
          <p class="security-note" data-i18n="security.confirm.codeNote"></p>` : ''}
        <div class="security-confirmrow">
          <button class="btn" type="button" id="secActionGo" disabled><span class="btn-label" data-i18n="security.confirm.go"></span></button>
          <button class="btn ghost" type="button" id="secActionCancel"><span class="btn-label" data-i18n="security.confirm.cancel"></span></button>
        </div>
        <p class="security-note" id="secActionError" role="alert"></p>
      </section>`;
    i18n.apply(host);
    const input = $('secActionToken');
    const go = $('secActionGo');
    const code = $('secActionCode');
    input.focus();
    const sync = () => {
      const typed = input.value.trim();
      go.disabled = typed !== token || (enrolled && !(code?.value || '').trim());
    };
    input.oninput = sync;
    if (code) code.oninput = sync;
    go.onclick = async () => {
      if (go.disabled) return;
      go.disabled = true;
      $('secActionError').textContent = '';
      try {
        await onConfirm({confirmation: token, totp: (code?.value || '').trim()});
        closeAction();
      } catch (error) {
        $('secActionError').textContent = message(error);
        go.disabled = false;
      }
    };
    $('secActionCancel').onclick = closeAction;
    host.scrollIntoView({block: 'nearest'});
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
    const audit = data.audit || {};
    const sessions = data.sessions || {};
    const two = data.two_factor || {};
    const vault = data.vault || {};
    const policy = data.policy || {};
    const chainOk = audit.chain === 'valid';
    $('secKpis').replaceChildren(
      kpi(t('security.kpi.chain'), chainOk ? t('security.value.intact') : t('security.value.broken'),
        chainOk ? 'ok' : 'error'),
      kpi(t('security.kpi.lines'), String(audit.counted ?? 0), chainOk ? '' : 'error'),
      kpi(t('security.kpi.sessions'), `${sessions.active ?? 0}`, (sessions.active ? 'ok' : '')),
      kpi(t('security.kpi.twoFactor'), two.enrolled ? t('security.value.on') : t('security.value.off'),
        two.enrolled ? 'ok' : 'warn'),
      kpi(t('security.kpi.vault'), `${vault.configured ?? 0}/${vault.total ?? 0}`,
        vault.configured ? '' : 'warn'),
      kpi(t('security.kpi.policy'), t(`security.mode.${policy.default || 'ASK_EVERY_TIME'}`),
        (policy.default === 'AUTO_ALLOW' ? 'warn' : '')),
    );
    paintAnomalies();
    paintNotes();
    paintInjection();
    $('secUpdated').textContent = t('security.updated', {time: clock(data.observed_at)});
    state.lastRead = data.observed_at;
    paintChip();
  }

  function paintAnomalies() {
    const rows = state.overview?.anomalies || [];
    const list = $('secAnomalies');
    list.replaceChildren();
    $('secAnomalyCount').textContent = t('security.anomalies.count', {count: rows.length});
    if (!rows.length) {
      list.append(el('li', 'security-empty', t('security.anomalies.none')));
      return;
    }
    for (const row of rows) {
      const item = el('li', 'security-anomaly');
      item.dataset.level = row.level;
      const head = el('div', 'security-anomalyhead');
      head.append(el('span', 'security-chip', t(`security.level.${row.level}`)),
        el('b', '', t(`security.anomaly.${row.id}.title`)));
      item.append(head, el('p', '', t(`security.anomaly.${row.id}.detail`, row.params || {})));
      if (row.evidence?.length) {
        const evidence = el('p', 'security-evidence', row.evidence.map((value) => String(value)).join(' · '));
        item.append(evidence);
      }
      list.append(item);
    }
  }

  function paintNotes() {
    const list = $('secNotes');
    list.replaceChildren();
    for (const note of state.overview?.notes || []) {
      list.append(el('li', '', t(`security.note.${note}`)));
    }
  }

  function paintInjection() {
    const sessions = state.injection?.sessions || [];
    const pick = $('secScanPick');
    const previous = pick.value;
    pick.replaceChildren();
    if (!sessions.length) {
      const option = new Option(t('security.injection.none'), '');
      pick.append(option);
      pick.disabled = true;
      $('secScan').disabled = true;
      return;
    }
    pick.disabled = false;
    $('secScan').disabled = false;
    for (const row of sessions) {
      const label = `${stamp(row.created)} · ${row.role || 'role'} · ${String(row.task || '').slice(0, 60)}`;
      pick.append(new Option(label, row.id));
    }
    if (previous && sessions.some((row) => row.id === previous)) pick.value = previous;
    paintFindings();
  }

  function paintFindings() {
    const result = state.findings;
    const list = $('secFindings');
    list.replaceChildren();
    const status = $('secScanStatus');
    if (!result) { status.textContent = ''; return; }
    if (!result.session) {
      status.textContent = result.note || t('security.injection.empty');
      return;
    }
    status.textContent = t('security.injection.scanned', {
      id: result.session.id, count: result.findings.length});
    if (!result.findings.length) {
      list.append(el('li', 'security-empty', t('security.injection.clean')));
      return;
    }
    for (const row of result.findings) {
      const item = el('li', 'security-finding');
      item.append(el('span', 'security-chip', t(`security.rule.${row.rule}`)),
        el('span', 'security-findingline', t('security.injection.line', {line: row.line})),
        el('code', 'security-snippet', row.snippet));
      list.append(item);
    }
  }

  /* ── audit ──────────────────────────────────────────────────────────── */
  function paintChain(chain, target) {
    const node = target;
    node.replaceChildren();
    if (!chain) { node.textContent = ''; return; }
    const ok = chain.ok;
    const head = el('div', 'security-chainhead');
    head.append(el('span', 'security-chip', ok ? t('security.value.intact') : t('security.value.broken')),
      el('b', '', t('security.chain.checked', {count: chain.checked})),
      el('span', 'security-stamp', t('security.chain.at', {time: stamp(chain.at)})));
    head.lastChild.dataset.tone = ok ? 'ok' : 'error';
    node.append(head);
    const facts = el('dl', 'security-facts');
    const add = (label, value, mono) => {
      const dt = el('dt', '', label);
      const dd = el('dd', mono ? 'security-mono' : '', value);
      facts.append(dt, dd);
    };
    add(t('security.chain.head'), shortHash(chain.head), true);
    add(t('security.chain.size'), bytes(chain.size), true);
    if (chain.legacy) add(t('security.chain.legacy'), String(chain.legacy), true);
    if (chain.first_bad) {
      add(t('security.chain.firstBad'),
        t('security.chain.badLine', {line: chain.first_bad.line, reason: chain.first_bad.reason}), true);
    }
    node.append(facts);
  }

  function paintAudit() {
    const body = state.audit;
    if (!body) return;
    paintChain(body.chain, $('secChain'));
    const rows = body.events || [];
    const tbody = $('secAuditRows');
    tbody.replaceChildren();
    if (!rows.length) {
      const tr = el('tr');
      const td = el('td', 'security-empty', t('security.audit.empty'));
      td.colSpan = 7;
      tr.append(td);
      tbody.append(tr);
      return;
    }
    for (const row of rows) {
      const tr = el('tr');
      if (row.risk === 'high') tr.dataset.risk = 'high';
      const when = el('td', 'security-mono', clock(row.at));
      when.title = iso(row.at);
      const kind = el('td');
      const prefix = group(row.action);
      kind.append(el('span', 'security-chip', prefix ? t(`security.kind.${prefix}`) : t('security.kind.other')));
      tr.append(when, kind,
        el('td', 'security-mono', row.action),
        el('td', 'security-target', row.target || '—'),
        tdChip(row.result || '—', row.result === 'denied' ? 'error' : row.result === 'ok' ? 'ok' : ''),
        tdChip(row.risk || 'low', row.risk === 'high' ? 'warn' : ''),
        el('td', 'security-mono', row.seq === null || row.seq === undefined ? '—' : String(row.seq)));
      tbody.append(tr);
    }
  }

  function tdChip(text, tone) {
    const td = el('td');
    const chip = el('span', 'security-chip', String(text));
    if (tone) chip.dataset.tone = tone;
    td.append(chip);
    return td;
  }

  function paintChip() {
    const chip = $('secChip');
    const chain = state.chain || (state.overview ? {
      ok: state.overview.audit?.chain === 'valid', checked: state.overview.audit?.counted || 0} : null);
    if (!chain) { chip.textContent = '—'; return; }
    chip.textContent = t('security.chip.chain', {count: chain.checked || 0});
    chip.dataset.tone = chain.ok ? 'ok' : 'error';
  }

  /* ── vault ──────────────────────────────────────────────────────────── */
  function paintVault() {
    const body = state.vault;
    if (!body) return;
    const tbody = $('secVaultRows');
    tbody.replaceChildren();
    for (const item of body.items || []) {
      const tr = el('tr');
      const name = el('td');
      name.append(el('b', '', item.label || item.name), el('span', 'security-subline', ` ${item.name}`));
      const source = el('td', 'security-mono', item.source || '—');
      const fingerprint = el('td', 'security-mono', item.fingerprint || '—');
      fingerprint.title = item.fingerprint ? t('security.vault.fingerprintHint') : '';
      const stateCell = tdChip(item.set ? t('security.value.set') : t('security.value.absent'),
        item.set ? 'ok' : 'warn');
      const action = el('td');
      if (item.editable && item.set) {
        const button = el('button', 'btn ghost security-small', t('security.vault.remove'));
        button.type = 'button';
        button.onclick = () => askDeleteSecret(item);
        action.append(button);
      } else {
        action.textContent = '—';
        action.title = t('security.vault.readOnly');
      }
      tr.append(name, source, fingerprint, stateCell, action);
      tbody.append(tr);
    }
    const select = $('secSecretName');
    const previous = select.value;
    select.replaceChildren();
    for (const item of body.items || []) {
      if (!item.editable) continue;
      select.append(new Option(item.label || item.name, item.name));
    }
    if (previous && [...select.options].some((option) => option.value === previous)) select.value = previous;
    const store = body.store || {};
    $('secStoreLine').textContent = store.exists
      ? t('security.vault.storeLine', {path: store.path || '—', mode: store.mode || '—',
        state: store.private ? t('security.vault.private') : t('security.vault.exposed')})
      : t('security.vault.storeLineMissing', {path: store.path || '—'});
    const notes = $('secVaultNotes');
    notes.replaceChildren();
    for (const note of body.never || []) notes.append(el('li', '', note));
  }

  function askSaveSecret() {
    const family = $('secSecretName').value;
    const value = $('secSecretValue').value.trim();
    const item = (state.vault?.items || []).find((row) => row.name === family);
    if (!value) { showError(t('security.vault.valueRequired')); return; }
    openAction({
      title: t('security.vault.confirmTitle', {name: item?.label || family}),
      token: `SAVE SECRET ${family}`,
      detail: t('security.vault.confirmDetail'),
      onConfirm: async (payload) => {
        const body = await gui.api('/api/security/vault/set',
          {name: family, value, ...payload}).then((reply) => reply.data);
        state.vault = body;
        $('secSecretValue').value = '';
        paintVault();
        window.nexusToast?.(t('security.vault.saved', {name: item?.label || family}), {tone: 'done'});
      },
    });
  }

  function askDeleteSecret(item) {
    openAction({
      title: t('security.vault.deleteTitle', {name: item.label || item.name}),
      token: `DELETE SECRET ${item.name}`,
      detail: t('security.vault.deleteDetail'),
      onConfirm: async (payload) => {
        state.vault = await gui.api('/api/security/vault/delete',
          {name: item.name, ...payload}).then((reply) => reply.data);
        paintVault();
        window.nexusToast?.(t('security.vault.deleted', {name: item.label || item.name}), {tone: 'done'});
      },
    });
  }

  /* ── access: sessions and two-factor ────────────────────────────────── */
  function paintSessions() {
    const body = state.sessions;
    if (!body) return;
    const tbody = $('secSessionRows');
    tbody.replaceChildren();
    const all = body.sessions || [];
    const revoked = all.filter((row) => row.revoked).length;
    const label = $('secShowRevokedLabel');
    if (label) label.textContent = t('security.sessions.showRevokedCount', {count: revoked});
    const rows = state.showRevoked ? all : all.filter((row) => !row.revoked);
    if (!rows.length) {
      const tr = el('tr');
      const td = el('td', 'security-empty', body.error || t('security.sessions.empty'));
      td.colSpan = 7;
      tr.append(td);
      tbody.append(tr);
      return;
    }
    for (const row of rows) {
      const tr = el('tr');
      if (row.current) tr.dataset.current = 'true';
      const browser = el('td');
      browser.append(el('b', '', row.current ? t('security.sessions.thisBrowser') : t('security.sessions.otherBrowser')),
        el('span', 'security-subline', ` ${row.id}`),
        el('span', 'security-subline', row.ua ? ` · ${String(row.ua).slice(0, 48)}` : ''));
      const stateCell = tdChip(row.revoked ? t('security.value.revoked') : (row.active ? t('security.value.active') : t('security.value.expired')),
        row.revoked ? 'error' : row.active ? 'ok' : 'warn');
      const action = el('td');
      if (!row.revoked && row.active) {
        const button = el('button', 'btn ghost security-small', t('security.sessions.revoke'));
        button.type = 'button';
        button.onclick = () => askRevoke(row);
        action.append(button);
      }
      tr.append(browser, el('td', 'security-mono', row.host || '—'),
        el('td', 'security-mono', stamp(row.created)),
        el('td', 'security-mono', stamp(row.last)),
        el('td', 'security-mono', stamp(row.expires)),
        stateCell, action);
      tbody.append(tr);
    }
  }

  function askRevoke(row) {
    openAction({
      title: t('security.sessions.revokeTitle'),
      token: `REVOKE SESSION ${row.id}`,
      detail: t('security.sessions.revokeDetail', {id: row.id}),
      onConfirm: async (payload) => {
        state.sessions = await gui.api('/api/security/session/revoke',
          {scope: 'one', id: row.id, ...payload}).then((reply) => reply.data);
        paintSessions();
        window.nexusToast?.(t('security.sessions.revoked'), {tone: 'done'});
      },
    });
  }

  function askRevokeScope(scope) {
    const token = scope === 'all' ? 'REVOKE ALL SESSIONS' : 'REVOKE OTHER SESSIONS';
    openAction({
      title: t(scope === 'all' ? 'security.sessions.allTitle' : 'security.sessions.othersTitle'),
      token,
      detail: t(scope === 'all' ? 'security.sessions.allDetail' : 'security.sessions.othersDetail'),
      onConfirm: async (payload) => {
        state.sessions = await gui.api('/api/security/session/revoke',
          {scope, ...payload}).then((reply) => reply.data);
        paintSessions();
        window.nexusToast?.(t('security.sessions.revokedMany'), {tone: 'done'});
      },
    });
  }

  function paintTotp() {
    const status = state.twoFactor;
    const body = $('secTotpBody');
    if (!status) { body.replaceChildren(); return; }
    const chip = $('secTotpState');
    chip.textContent = status.enrolled ? t('security.value.on') : t('security.value.off');
    chip.dataset.tone = status.enrolled ? 'ok' : 'warn';
    body.replaceChildren();
    if (status.error) {
      body.append(el('p', 'security-banner', status.error));
      return;
    }
    if (state.recovery) {
      const list = el('ul', 'security-recovery');
      for (const code of state.recovery) list.append(el('li', 'security-mono', code));
      const done = el('button', 'btn', t('security.totp.recoveryStored'));
      done.type = 'button';
      done.onclick = () => { state.recovery = null; state.enrolment = null; paintTotp(); };
      body.append(el('h3', '', t('security.totp.recoveryTitle')), list,
        el('p', 'security-note', t('security.totp.recoveryNote')), done);
      return;
    }
    if (status.enrolled) {
      const facts = el('dl', 'security-facts');
      const add = (label, value, mono) => {
        facts.append(el('dt', '', label), el('dd', mono ? 'security-mono' : '', value));
      };
      add(t('security.totp.enrolledAt'), stamp(status.created));
      add(t('security.totp.recoveryLeft'), String(status.recovery_left));
      add(t('security.totp.period'), t('security.totp.periodValue', {seconds: status.period, digits: status.digits}));
      body.append(facts, el('p', 'security-note', t('security.totp.enrolledNote')));
      const disable = el('button', 'btn ghost', t('security.totp.disable'));
      disable.type = 'button';
      disable.onclick = askDisable;
      body.append(disable);
      return;
    }
    body.append(el('p', 'security-note', t('security.totp.intro')));
    if (!state.enrolment) {
      const start = el('button', 'btn', t('security.totp.begin'));
      start.type = 'button';
      start.onclick = () => guard(async () => {
        state.enrolment = await gui.api('/api/security/two-factor/begin', {}).then((reply) => reply.data);
        paintTotp();
      });
      body.append(start);
      return;
    }
    const enrolment = state.enrolment;
    body.append(
      el('p', 'security-note', t('security.totp.secretNote')),
      el('code', 'security-secret', enrolment.secret),
      el('p', 'security-note', t('security.totp.uriNote')),
      el('code', 'security-uri', enrolment.uri));
    const form = el('div', 'security-row');
    const code = el('input');
    code.id = 'secEnableCode';
    code.type = 'text';
    code.inputMode = 'numeric';
    code.autocomplete = 'one-time-code';
    code.maxLength = 8;
    code.setAttribute('aria-label', t('security.totp.codeLabel'));
    const go = el('button', 'btn', t('security.totp.confirm'));
    go.type = 'button';
    go.onclick = () => askEnable(code.value);
    const cancel = el('button', 'btn ghost', t('security.totp.cancel'));
    cancel.type = 'button';
    cancel.onclick = () => { state.enrolment = null; paintTotp(); };
    form.append(code, go, cancel);
    body.append(form);
  }

  function askEnable(code) {
    openAction({
      title: t('security.totp.enableTitle'),
      token: 'ENABLE 2FA',
      detail: t('security.totp.enableDetail'),
      onConfirm: async (payload) => {
        const done = await gui.api('/api/security/two-factor/confirm',
          {confirmation: payload.confirmation, code}).then((reply) => reply.data);
        state.twoFactor = done;
        state.recovery = done.recovery_codes || [];
        state.enrolment = null;
        paintTotp();
        window.nexusToast?.(t('security.totp.enabled'), {tone: 'done'});
      },
    });
  }

  function askDisable() {
    openAction({
      title: t('security.totp.disableTitle'),
      token: 'DISABLE 2FA',
      detail: t('security.totp.disableDetail'),
      needsCode: true,
      onConfirm: async (payload) => {
        state.twoFactor = await gui.api('/api/security/two-factor/disable',
          {confirmation: payload.confirmation, code: payload.totp}).then((reply) => reply.data);
        state.recovery = null;
        paintTotp();
        window.nexusToast?.(t('security.totp.disabled'), {tone: 'info'});
      },
    });
  }

  /* ── sandbox: the real policy file ──────────────────────────────────── */
  function paintPolicy() {
    const policy = state.policy;
    if (!policy) return;
    $('secPolicyFile').textContent = t('security.policy.fileLine', {
      path: policy.file?.path || 'permissions.yaml', mode: policy.file?.mode || '—',
      revision: shortHash(policy.file?.revision)});
    if (policy.error) {
      $('secPolicyFile').textContent = t('security.policy.broken', {error: policy.error});
    }
    const latch = $('secLatch');
    if (policy.latch === true) { latch.textContent = t('security.policy.latched'); latch.dataset.tone = 'error'; }
    else if (policy.latch === false) { latch.textContent = t('security.policy.clear'); latch.dataset.tone = 'ok'; }
    else { latch.textContent = t('security.policy.latchUnknown'); latch.dataset.tone = 'warn'; }

    const modes = $('secDefaultModes');
    modes.replaceChildren();
    for (const mode of MODES) {
      const label = el('label', 'security-radio');
      const input = el('input');
      input.type = 'radio';
      input.name = 'secDefaultMode';
      input.value = mode;
      input.checked = policy.default === mode;
      label.append(input, el('span', '', t(`security.mode.${mode}`)), el('small', '', t(`security.modeHint.${mode}`)));
      modes.append(label);
    }
    const tbody = $('secToolRows');
    tbody.replaceChildren();
    for (const tool of policy.risky_mutating || []) {
      const tr = el('tr');
      const modeCell = el('td');
      const select = el('select', 'security-select');
      select.dataset.tool = tool;
      select.setAttribute('aria-label', t('security.policy.modeFor', {tool}));
      select.append(new Option(t('security.policy.followDefault'), ''));
      for (const mode of MODES) {
        const option = new Option(t(`security.mode.${mode}`), mode);
        select.append(option);
      }
      select.value = policy.tools?.[tool] || '';
      modeCell.append(select);
      const forced = (policy.forced || []).includes(tool);
      tr.append(el('td', 'security-mono', tool), modeCell,
        tdChip(forced ? t('security.value.forced') : t('security.value.policy'), forced ? 'warn' : ''));
      tbody.append(tr);
    }
    const adapters = $('secAdapters');
    adapters.replaceChildren();
    for (const name of ['browser', 'editor', 'office']) {
      const label = el('label', 'security-switch');
      const input = el('input');
      input.type = 'checkbox';
      input.dataset.adapter = name;
      input.checked = Boolean(policy.computer?.[name]);
      label.append(input, el('span', '', t(`security.adapter.${name}`)));
      adapters.append(label);
    }
    $('secShellBackend').value = policy.computer?.shell_backend === 'docker' ? 'docker' : 'host';
    $('secShellImage').value = policy.computer?.image || '';
    $('secPolicyRevision').textContent = t('security.policy.revision', {rev: shortHash(policy.file?.revision)});
  }

  function collectPolicy() {
    const tools = {};
    document.querySelectorAll('#secToolRows select[data-tool]').forEach((select) => {
      if (select.value) tools[select.dataset.tool] = select.value;
    });
    const computer = {};
    document.querySelectorAll('#secAdapters input[data-adapter]').forEach((input) => {
      computer[input.dataset.adapter] = input.checked;
    });
    computer.shell_backend = $('secShellBackend').value;
    computer.image = $('secShellImage').value.trim();
    const chosen = document.querySelector('input[name="secDefaultMode"]:checked');
    return {default: chosen?.value || state.policy?.default, tools, computer};
  }

  function askSavePolicy() {
    const policy = state.policy;
    if (!policy) return;
    const payload = collectPolicy();
    openAction({
      title: t('security.policy.saveTitle'),
      token: 'SAVE CAPABILITY POLICY',
      detail: t('security.policy.saveDetail', {mode: t(`security.mode.${payload.default}`)}),
      onConfirm: async (confirmation) => {
        const body = await gui.api('/api/security/policy/save', {
          ...payload, revision: policy.file?.revision, ...confirmation,
        }).then((reply) => reply.data);
        state.policy = body;
        paintPolicy();
        window.nexusToast?.(t('security.policy.saved'), {tone: 'done'});
        loadOverview().catch(() => {});
      },
    });
  }

  /* ── loaders ────────────────────────────────────────────────────────── */
  async function refreshOverview() {
    state.overview = await gui.api('/api/security/overview').then((reply) => reply.data);
    state.chain = null;
    paintOverview();
    await loadInjection();
  }

  async function loadOverview() {
    state.overview = await gui.api('/api/security/overview').then((reply) => reply.data);
    paintOverview();
  }

  async function loadInjection() {
    state.injection = await gui.api('/api/security/injection').then((reply) => reply.data);
    paintInjection();
  }

  async function loadAudit() {
    const filter = encodeURIComponent($('secAuditFilter').value || '');
    const query = filter ? `?limit=120&action=${filter}` : '?limit=120';
    const [audit, snapshots] = await Promise.all([
      gui.api(`/api/security/audit${query}`).then((reply) => reply.data),
      gui.api('/api/security/snapshots').then((reply) => reply.data),
    ]);
    state.audit = audit;
    state.snapshots = snapshots;
    state.chain = state.audit.chain;
    paintAudit();
    paintSnapshots();
    paintChip();
  }

  async function loadVault() {
    state.vault = await gui.api('/api/security/vault').then((reply) => reply.data);
    paintVault();
  }

  async function loadAccess() {
    const [sessions, twoFactor] = await Promise.all([
      gui.api('/api/security/sessions').then((reply) => reply.data),
      gui.api('/api/security/two-factor').then((reply) => reply.data),
    ]);
    state.sessions = sessions;
    state.twoFactor = twoFactor;
    state.enrolment = null;
    paintSessions();
    paintTotp();
  }

  async function loadPolicy() {
    state.policy = await gui.api('/api/security/policy').then((reply) => reply.data);
    paintPolicy();
  }

  async function verifyChain() {
    const body = await gui.api('/api/security/audit/verify', {}).then((reply) => reply.data);
    state.chain = body.chain;
    paintChain(body.chain, $('secChain'));
    paintChip();
    window.nexusToast?.(body.chain.ok
      ? t('security.chain.verified', {count: body.chain.checked})
      : t('security.chain.failed', {line: body.chain.first_bad?.line || 0}),
    {tone: body.chain.ok ? 'done' : 'error'});
  }

  function exportAudit() {
    const rows = state.audit?.events || [];
    if (!rows.length) { showError(t('security.audit.nothing')); return; }
    const head = state.chain ? `# chain head ${state.chain.head} checked ${state.chain.checked}\n` : '';
    const text = head + rows.slice().reverse().map((row) => JSON.stringify(row)).join('\n') + '\n';
    const blob = new Blob([text], {type: 'application/x-ndjson'});
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'nexus-security-audit.jsonl';
    link.click();
    URL.revokeObjectURL(link.href);
  }

  function paintSnapshots() {
    const body = state.snapshots;
    const tbody = $('secSnapshotRows');
    if (!body || !tbody) return;
    tbody.replaceChildren();
    const rows = body.snapshots || [];
    if (!rows.length) {
      const tr = el('tr');
      const td = el('td', 'security-empty', t('security.snapshots.empty'));
      td.colSpan = 4;
      tr.append(td);
      tbody.append(tr);
    }
    for (const row of rows) {
      const tr = el('tr');
      const when = el('td');
      when.append(el('b', '', stamp(row.at)), el('span', 'security-subline security-mono', ` ${row.name}`));
      const lines = el('td', '', String(row.checked ?? 0));
      const head = el('td', 'security-mono security-target', shortHash(row.head));
      head.title = row.head || '';
      const action = el('td');
      const box = el('div', 'security-row');
      const download = el('button', 'btn ghost security-small', t('security.snapshots.download'));
      download.type = 'button';
      download.onclick = () => downloadSnapshot(row);
      const restore = el('button', 'btn ghost security-small', t('security.snapshots.restore'));
      restore.type = 'button';
      restore.onclick = () => askRestore(row);
      box.append(download, restore);
      action.append(box);
      tr.append(when, lines, head, action);
      tbody.append(tr);
    }
    $('secSnapshotNote').textContent = t('security.snapshots.store', {
      dir: body.dir || '—', limit: body.limit || 0});
  }

  async function downloadSnapshot(row) {
    try {
      const reply = await gui.api(`/api/security/snapshot?name=${encodeURIComponent(row.name)}`);
      const blob = new Blob([JSON.stringify(reply.data, null, 1)], {type: 'application/json'});
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `${row.name}.json`;
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (error) {
      showError(message(error));
    }
  }

  function askTakeSnapshot() {
    openAction({
      title: t('security.snapshots.takeTitle'),
      token: 'TAKE SECURITY SNAPSHOT',
      detail: t('security.snapshots.takeDetail'),
      onConfirm: async (payload) => {
        const body = await gui.api('/api/security/snapshot', payload).then((reply) => reply.data);
        state.snapshots = body;
        paintSnapshots();
        await loadAudit();
        window.nexusToast?.(t('security.snapshots.taken', {name: body.taken}), {tone: 'done'});
      },
    });
  }

  function askRestore(row) {
    openAction({
      title: t('security.snapshots.restoreTitle'),
      token: `RESTORE SECURITY SNAPSHOT ${row.name}`,
      detail: t('security.snapshots.restoreDetail', {name: row.name, lines: row.checked ?? 0}),
      onConfirm: async (payload) => {
        const body = await gui.api('/api/security/snapshot/restore',
          {...payload, name: row.name}).then((reply) => reply.data);
        state.snapshots = body;
        paintSnapshots();
        await loadAudit();
        window.nexusToast?.(t('security.snapshots.restored', {name: body.restored}), {tone: 'done'});
      },
    });
  }

  async function scan() {
    const id = $('secScanPick').value;
    $('secScanStatus').textContent = t('security.injection.scanning');
    state.findings = await gui.api('/api/security/injection/scan', {session_id: id}).then((reply) => reply.data);
    paintFindings();
  }

  /* ── wiring ─────────────────────────────────────────────────────────── */
  document.querySelectorAll('[data-sec-tab]').forEach((button, index, all) => {
    button.addEventListener('click', () => switchTab(button.dataset.secTab));
    button.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? all.length - 1
        : (index + (event.key === 'ArrowRight' ? 1 : -1) + all.length) % all.length;
      all[next].focus();
      switchTab(all[next].dataset.secTab);
    });
  });

  $('secRefresh').onclick = () => guard(async () => {
    if (state.tab === 'overview') await refreshOverview();
    if (state.tab === 'audit') await loadAudit();
    if (state.tab === 'vault') await loadVault();
    if (state.tab === 'access') await loadAccess();
    if (state.tab === 'sandbox') await loadPolicy();
  });
  $('secOperations').onclick = () => gui.show('operations');
  $('secAuditFilter').onchange = () => guard(loadAudit);
  $('secVerify').onclick = () => guard(verifyChain);
  $('secExport').onclick = exportAudit;
  $('secSnapshotTake').onclick = askTakeSnapshot;
  $('secScan').onclick = () => guard(scan);
  $('secSecretSave').onclick = () => askSaveSecret();
  $('secShowRevoked').checked = state.showRevoked;
  $('secShowRevoked').onchange = (event) => { state.showRevoked = event.target.checked; paintSessions(); };
  $('secRevokeOthers').onclick = () => askRevokeScope('others');
  $('secRevokeAll').onclick = () => askRevokeScope('all');
  $('secPolicySave').onclick = () => askSavePolicy();

  window.addEventListener('nexus:view', (event) => {
    if (event.detail === 'security' && !state.overview) refreshOverview().catch(showError);
  });

  i18n.onChange(() => {
    i18n.apply(root);
    paintOverview();
    paintAudit();
    paintVault();
    paintSessions();
    paintTotp();
    paintPolicy();
    paintFindings();
    paintChip();
  });

  const initial = new URLSearchParams(location.search).get('tab');
  switchTab(TABS.includes(initial) ? initial : 'overview');

  window.nexusSecurity = {
    root,
    tab: switchTab,
    load: refreshOverview,
    state: () => ({tab: state.tab, chain: state.chain?.ok ?? null, sessions: state.sessions?.sessions?.length ?? null,
      enrolled: state.twoFactor?.enrolled ?? null, policy: state.policy?.default || null}),
  };
})();
