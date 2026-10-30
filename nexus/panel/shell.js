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
  function setNav(open) {
    body.classList.toggle('nav-open', open);
    if (scrim) scrim.hidden = !open;
    if (navToggle) navToggle.setAttribute('aria-expanded', String(open));
  }
  navToggle?.addEventListener('click', () => setNav(!body.classList.contains('nav-open')));
  scrim?.addEventListener('click', () => setNav(false));
  window.addEventListener('keydown', event => {
    if (event.key === 'Escape' && body.classList.contains('nav-open')) setNav(false);
  });

  /* ── page title ────────────────────────────────────────────────────── */
  const titles = {
    control: 'Control center',
    run: 'Run & chat',
    operations: 'Operations & approvals',
    world: 'World, news & weather',
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

  /* ── friendly hint until the first event arrives ───────────────────── */
  syncTitle(window.nexusGui?.view || 'control');
})();
