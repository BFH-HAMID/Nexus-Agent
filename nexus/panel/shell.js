/* Shell behaviour: theme, mobile navigation and sidebar tool placement.
   Presentation only — no task, approval or permission is ever started here. */
(() => {
  'use strict';

  /* ── theme ─────────────────────────────────────────────────────────── */
  const root = document.documentElement;
  const stored = (() => { try { return localStorage.getItem('nexus.ui.theme'); } catch { return null; } })();
  root.dataset.theme = stored === 'light' ? 'light' : 'dark';

  const toggle = document.getElementById('themeToggle');
  function paintThemeButton() {
    if (!toggle) return;
    const light = root.dataset.theme === 'light';
    toggle.innerHTML = '<svg class="ic" aria-hidden="true"><use href="#i-' + (light ? 'moon' : 'sun') + '"/></svg>';
    toggle.setAttribute('aria-label', light ? 'Switch to dark theme' : 'Switch to light theme');
    toggle.title = light ? 'Switch to dark theme' : 'Switch to light theme';
  }
  paintThemeButton();
  if (toggle) toggle.onclick = () => {
    root.dataset.theme = root.dataset.theme === 'light' ? 'dark' : 'light';
    try { localStorage.setItem('nexus.ui.theme', root.dataset.theme); } catch { /* session only */ }
    paintThemeButton();
    window.dispatchEvent(new CustomEvent('nexus:theme', {detail: root.dataset.theme}));
  };

  /* ── mobile navigation drawer ──────────────────────────────────────── */
  const body = document.body;
  const navToggle = document.getElementById('navToggle');
  const scrim = document.getElementById('navScrim');
  const drawerQuery = window.matchMedia('(max-width: 1080px)');
  const sidebar = document.getElementById('sidebar');
  function setNav(open) {
    body.classList.toggle('nav-open', open);
    if (scrim) scrim.hidden = !open;
    if (navToggle) navToggle.setAttribute('aria-expanded', String(open));
    syncInert();
  }
  /* When the sidebar is an off-canvas drawer it must leave the accessibility
     tree while closed, otherwise screen readers and role queries see two
     copies of every section. */
  function syncInert() {
    if (!sidebar) return;
    const asDrawer = drawerQuery.matches;
    sidebar.toggleAttribute('inert', asDrawer && !body.classList.contains('nav-open'));
  }
  drawerQuery.addEventListener?.('change', syncInert);
  syncInert();
  navToggle?.addEventListener('click', () => setNav(!body.classList.contains('nav-open')));
  scrim?.addEventListener('click', () => setNav(false));
  window.addEventListener('keydown', event => {
    if (event.key === 'Escape' && body.classList.contains('nav-open')) setNav(false);
  });

  window.nexusShell = {
    setNav,
    closeNav: () => setNav(false),
  };

  /* ── page title ────────────────────────────────────────────────────── */
  const titles = {
    mission: 'Mission Control',
    control: 'All controls',
    run: 'Run & chat',
    operations: 'Approvals',
    world: 'World & news',
  };
  function syncTitle(view) {
    const node = document.getElementById('pageTitle');
    if (node && titles[view]) node.textContent = titles[view];
  }
  window.addEventListener('nexus:view', event => {
    syncTitle(event.detail);
    setNav(false);
  });

  /* ── workspace tools: one consistent icon-plus-label group in the topbar ── */
  const actions = document.querySelector('.topbar-actions');
  const divider = actions?.querySelector('.topbar-divider');
  const tools = {
    wbOpen: { icon: 'i-terminal', label: 'Workbench', hint: 'Ctrl+K' },
    studioOpen: { icon: 'i-layers', label: 'Studio', hint: 'review · evidence' },
    guiManagement: { icon: 'i-users', label: 'Roles', hint: 'roles, agents & teams' },
    guiSettings: { icon: 'i-sliders', label: 'Settings', hint: 'models, policy, integrations' },
  };
  for (const [id, meta] of Object.entries(tools)) {
    const button = document.getElementById(id);
    if (!button || !actions) continue;
    button.type = 'button';
    button.classList.add('btn', 'tool-btn');
    if (!button.querySelector('svg')) {
      const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      icon.setAttribute('class', 'ic');
      icon.setAttribute('aria-hidden', 'true');
      const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
      use.setAttribute('href', '#' + meta.icon);
      icon.append(use);
      button.prepend(icon);
    }
    let label = button.querySelector('.btn-label');
    if (!label) {
      label = document.createElement('span');
      label.className = 'btn-label';
      // Feature scripts may have set plain text; keep it instead of duplicating.
      const nodes = Array.from(button.childNodes).filter((node) => node.nodeType === Node.TEXT_NODE);
      const text = nodes.map((node) => node.nodeValue.trim()).join(' ').trim();
      nodes.forEach((node) => node.remove());
      label.textContent = text && text.length <= 14 ? text : meta.label;
      button.append(label);
    }
    button.dataset.hint = meta.hint;
    button.setAttribute('aria-label', label.textContent + ' — ' + meta.hint);
    if (!button.title) button.title = label.textContent + ' — ' + meta.hint;
    if (divider) actions.insertBefore(button, divider);
    else actions.insertBefore(button, document.getElementById('themeToggle'));
  }
  const logout = document.getElementById('guiLogout');
  if (logout && !logout.querySelector('svg')) {
    logout.insertAdjacentHTML('afterbegin', '<svg class="ic" aria-hidden="true"><use href="#i-logout"/></svg>');
  }

  /* ── toasts ────────────────────────────────────────────────────────── */
  const toastHost = document.getElementById('toasts');
  function toast(message, options = {}) {
    if (!toastHost || !message) return null;
    const tone = options.tone || 'done';
    const node = document.createElement('div');
    node.className = 'toast';
    node.dataset.tone = tone;
    node.setAttribute('role', tone === 'error' ? 'alert' : 'status');
    const mark = document.createElement('span');
    mark.setAttribute('aria-hidden', 'true');
    mark.textContent = tone === 'error' ? '!' : tone === 'info' ? 'i' : '✓';
    const body = document.createElement('div');
    const title = document.createElement('b');
    title.textContent = options.title || (tone === 'error' ? 'Error' : tone === 'info' ? 'Info' : 'Done');
    const text = document.createElement('div');
    text.textContent = String(message);
    body.append(title, text);
    const close = document.createElement('button');
    close.type = 'button';
    close.setAttribute('aria-label', 'Dismiss notification');
    close.textContent = '✕';
    close.onclick = () => node.remove();
    node.append(mark, body, close);
    toastHost.append(node);
    const timeout = Number.isFinite(options.timeout) ? options.timeout : tone === 'error' ? 12000 : 5200;
    if (timeout > 0) window.setTimeout(() => node.remove(), timeout);
    while (toastHost.children.length > 4) toastHost.firstElementChild?.remove();
    return node;
  }
  window.nexusToast = toast;

  /* ── emergency kill: two confirmations, then a reviewed daemon request ── */
  const killButton = document.getElementById('emergencyKill');
  let killDialog = null;
  let killBusy = false;

  function buildKillDialog() {
    const dialog = document.createElement('dialog');
    dialog.id = 'killDialog';
    dialog.className = 'kill-dialog';
    dialog.setAttribute('aria-labelledby', 'killTitle');
    dialog.innerHTML = `
      <form method="dialog" class="kill-box">
        <header class="kill-head">
          <span class="kill-mark" aria-hidden="true">!</span>
          <div><h2 id="killTitle">Emergency kill</h2>
          <p class="kill-lead">This stops the worker loop and cancels runs that are still active. It cannot undo work that already ran.</p></div>
        </header>
        <ol class="kill-steps" aria-label="Confirmation steps">
          <li data-step="1" aria-current="step">1</li><li data-step="2">2</li>
        </ol>
        <section class="kill-step" data-kill-step="one">
          <p class="kill-note">Nothing is sent to the server yet. Only the reviewed daemon request runs, no OS process is signalled.</p>
          <div class="kill-actions">
            <button class="btn danger" type="button" id="killContinue">Continue to the typed confirmation</button>
            <button class="btn ghost" type="button" id="killCancel1">Cancel</button>
          </div>
        </section>
        <section class="kill-step" data-kill-step="two" hidden>
          <label class="kill-field">Confirmation word
            <input id="killWord" type="text" autocomplete="off" spellcheck="false" placeholder="KILL" aria-describedby="killWordHint" />
          </label>
          <p class="kill-hint" id="killWordHint">Type KILL exactly. The confirm button stays disabled until it matches.</p>
          <p class="kill-error" id="killError" role="alert"></p>
          <div class="kill-actions">
            <button class="btn danger" type="button" id="killConfirm" disabled>Kill now</button>
            <button class="btn ghost" type="button" id="killCancel2">Cancel</button>
          </div>
        </section>
      </form>`;
    document.body.append(dialog);
    const word = dialog.querySelector('#killWord');
    const confirm = dialog.querySelector('#killConfirm');
    const error = dialog.querySelector('#killError');
    const steps = [...dialog.querySelectorAll('[data-step]')];

    const showStep = (step) => {
      dialog.querySelector('[data-kill-step="one"]').hidden = step !== 1;
      dialog.querySelector('[data-kill-step="two"]').hidden = step !== 2;
      steps.forEach((node) => node.toggleAttribute('aria-current', Number(node.dataset.step) === step));
      if (step === 2) word.focus();
      else dialog.querySelector('#killContinue').focus();
    };
    dialog.querySelector('#killContinue').onclick = () => showStep(2);
    dialog.querySelector('#killCancel1').onclick = () => dialog.close();
    dialog.querySelector('#killCancel2').onclick = () => dialog.close();
    word.oninput = () => { confirm.disabled = word.value.trim() !== 'KILL' || killBusy; error.textContent = ''; };
    word.onkeydown = (event) => {
      if (event.key === 'Enter') { event.preventDefault(); if (!confirm.disabled) confirm.click(); }
    };
    confirm.onclick = async () => {
      if (killBusy || word.value.trim() !== 'KILL') return;
      killBusy = true;
      confirm.disabled = true;
      error.textContent = 'Requesting the reviewed kill…';
      try {
        const gui = window.nexusGui;
        const result = await gui.api('/api/operations/action/control', {action: 'kill'});
        let cancelled = 0;
        try {
          const tasks = await gui.api('/api/tasks');
          for (const task of Array.isArray(tasks) ? tasks : []) {
            if (['queued', 'running', 'awaiting_approval'].includes(task.status)) {
              try { await gui.api('/api/cancel/' + encodeURIComponent(task.task_id), {}); cancelled += 1; } catch { /* one stubborn run must not hide the rest */ }
            }
          }
        } catch { /* task list is best effort; the daemon request already stands */ }
        const desired = result?.control?.desired || result?.state || 'requested';
        const note = result?.note ? ' ' + result.note : '';
        dialog.close();
        toast(`Kill requested (${desired}).${cancelled ? ' ' + cancelled + ' run(s) cancelled.' : ''}${note}`,
          {tone: 'done', title: 'Emergency kill'});
        window.dispatchEvent(new CustomEvent('nexus:kill', {detail: result}));
      } catch (failure) {
        error.textContent = 'Kill failed: ' + (failure?.message || 'request refused');
        toast('Kill failed: ' + (failure?.message || 'request refused'), {tone: 'error'});
      } finally {
        killBusy = false;
        confirm.disabled = word.value.trim() !== 'KILL';
      }
    };
    dialog.addEventListener('close', () => {
      word.value = '';
      confirm.disabled = true;
      error.textContent = '';
      showStep(1);
    });
    return dialog;
  }

  if (killButton) {
    killButton.addEventListener('click', () => {
      if (!killDialog) killDialog = buildKillDialog();
      if (!killDialog.open) killDialog.showModal();
    });
  }

  /* ── notification bell ─────────────────────────────────────────────── */
  const bell = document.getElementById('notifyBell');
  const popover = document.getElementById('bellPopover');
  const bellBody = document.getElementById('bellBody');
  const bellOpen = document.getElementById('bellOpen');
  let pendingState = null;
  let seen = 0;
  try { seen = Number(localStorage.getItem('nexus.ui.bellSeen')) || 0; } catch { /* session only */ }

  function paintBell() {
    const count = Number(pendingState?.pending_actions_total) || 0;
    const badgeNode = document.getElementById('bellCount');
    if (badgeNode) {
      badgeNode.textContent = String(Math.min(count, 99));
      badgeNode.hidden = !count;
    }
    if (!bellBody) return;
    bellBody.replaceChildren();
    const items = Array.isArray(pendingState?.pending_actions) ? pendingState.pending_actions : [];
    if (!items.length) {
      const empty = document.createElement('p');
      empty.className = 'muted';
      empty.textContent = 'Nothing is waiting for you.';
      bellBody.append(empty);
      return;
    }
    for (const action of items.slice(0, 8)) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'popover-item';
      row.setAttribute('data-risk', 'medium');
      const dot = document.createElement('span');
      dot.className = 'dot';
      const body = document.createElement('span');
      const title = document.createElement('b');
      title.textContent = String(action.tool || 'action');
      const meta = document.createElement('small');
      meta.textContent = [action.job_id, action.expires ? 'expires ' + new Date(action.expires * 1000).toLocaleTimeString() : ''].filter(Boolean).join(' · ');
      body.append(title, meta);
      const arrow = document.createElement('span');
      arrow.textContent = '›';
      row.append(dot, body, arrow);
      row.onclick = () => { closePopover(); window.nexusNav?.go('approvals'); };
      bellBody.append(row);
    }
  }

  function openPopover() {
    if (!popover) return;
    popover.hidden = false;
    bell?.setAttribute('aria-expanded', 'true');
    paintBell();
  }
  function closePopover() {
    if (!popover) return;
    popover.hidden = true;
    bell?.setAttribute('aria-expanded', 'false');
  }
  bell?.addEventListener('click', () => { if (popover?.hidden) openPopover(); else closePopover(); });
  document.getElementById('bellClose')?.addEventListener('click', closePopover);
  bellOpen?.addEventListener('click', () => { closePopover(); window.nexusNav?.go('approvals'); });
  document.addEventListener('click', (event) => {
    if (!popover || popover.hidden) return;
    if (popover.contains(event.target) || bell?.contains(event.target)) return;
    closePopover();
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closePopover(); });

  window.addEventListener('nexus:pending', (event) => {
    pendingState = event.detail;
    paintBell();
  });

  /* ── friendly hint until the first event arrives ───────────────────── */
  syncTitle(window.nexusGui?.view || 'mission');
})();
