/* One GUI, one identity, one origin. Existing workbench/agent APIs are reused. */
(() => {
  'use strict';
  let credential = new URLSearchParams(location.search).get('token') || '';
  const clean = new URL(location.href); clean.searchParams.delete('token');
  history.replaceState(null, '', clean);
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });
  const login = document.createElement('dialog'); login.id = 'guiLogin';
  login.innerHTML = '<form><h2>Nexus dashboard access</h2><p>Use the GUI access token or a paired device token, not a model/GitHub key. Paired devices open the scoped Shared HUD only.</p><label>Private access token<input id="guiToken" type="password" autocomplete="off" required></label><label><input id="guiRemember" type="checkbox" checked> Remember this trusted browser</label><button class="btn" type="submit">Connect</button><p id="guiLoginError" role="alert"></p></form>';
  document.body.append(login);
  async function authenticate() {
    const headers = credential ? {'X-Nexus-Token': credential} : {};
    const response = await fetch('/api/session', {headers});
    if (!response.ok) { if (!login.open) login.showModal(); return; }
    const info=await response.json();
    gui.principal=info.principal;
    document.body.classList.toggle('gui-device',info.principal?.kind==='device');
    if(info.principal?.kind==='device' && gui.view!=='presence')show('presence');
    const logout=document.getElementById('guiLogout');logout.disabled=!info.required;logout.title=info.required?'Forget this browser login':'Local GUI access: no separate login is configured';
    if (credential) {
      const saved = await fetch('/api/session', {method:'POST', headers:{...headers,'Content-Type':'application/json'},body:JSON.stringify({remember:document.getElementById('guiRemember').checked})});
      if (saved.ok) credential = '';
    }
    if (login.open) login.close();
    resolveReady();
    window.dispatchEvent(new Event('nexus:authenticated'));
  }
  login.querySelector('form').onsubmit = async e => {
    e.preventDefault(); credential = document.getElementById('guiToken').value; document.getElementById('guiToken').value = '';
    try { await authenticate(); if(login.open) document.getElementById('guiLoginError').textContent='Access refused. Check the GUI token used at launch.'; }
    catch { document.getElementById('guiLoginError').textContent='Server unavailable; retry.'; }
  };
  const gui = window.nexusGui = {
    ready,
    async fetch(path, options={}) {
      await ready;
      const headers = {...options.headers, ...(credential ? {'X-Nexus-Token':credential} : {})};
      const response = await fetch(path, {...options, headers});
      if(response.status===401 && !login.open) login.showModal();
      return response;
    },
    async api(path, body) {
      const r = await gui.fetch(path, {method:body===undefined?'GET':'POST', headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});
      const value = await r.json();
      if(!r.ok) throw Error(value?.error?.message || value?.error || value?.error_message || value?.detail || 'Request refused');
      return value;
    },
    view: 'run',
    /* EventSource cannot send headers: use the remembered cookie normally, and
       fall back to the query token only for a session that chose not to be
       remembered. Referrer-Policy keeps it out of Referer headers. */
    streamUrl(path) {
      return credential ? path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(credential) : path;
    }
  };
  /* Every page the shell can show. One list, so a new view cannot be
     reachable from the sidebar but silently ignored by the router. */
  const VIEWS = ['presence','mission','assistant','roles','router','computer','approvals','workflows','security','memory','cost','channels','run','operations','world','control','settings'];
  let worldLoaded = false, worldImport = null, worldMount = 0;
  const worldHome = document.getElementById('view-world');
  // The same widget instance can live in World or in the shared HUD. The HUD
  // supplies a cache-only API: no new publisher request or parallel dashboard.
  gui.mountWorld=async(host,request)=>{
    const widget=document.getElementById('world-widget');
    if(!widget || !host)return false;
    const generation=++worldMount, view=gui.view;
    // Retire the old adapter before moving a live widget between surfaces.
    window.dispatchEvent(new Event('hud:disconnected'));
    host.append(widget); widget.classList.remove('world-hidden');
    worldImport ||= import('/world/world.js'); await worldImport;
    if(generation!==worldMount || gui.view!==view || widget.parentElement!==host)return false;
    worldLoaded=true;
    window.dispatchEvent(new CustomEvent('hud:connected',{detail:{api:request}}));
    window.dispatchEvent(new Event('hud:layout'));
    return true;
  };
  async function show(view) {
    if (!VIEWS.includes(view)) return;
    if(gui.principal?.kind==='device' && view!=='presence')view='presence';
    if(gui.view!==view){++worldMount;window.dispatchEvent(new Event('hud:disconnected'));}
    gui.view = view;
    document.body.classList.toggle('gui-module-view',view!=='run');
    document.querySelectorAll('[data-gui-view]').forEach(n => n.hidden = n.dataset.guiView !== view);
    document.querySelectorAll('[data-gui-tab]').forEach(n => {const active=n.dataset.guiTab===view;n.setAttribute('aria-selected',String(active));});
    history.replaceState(null,'','#'+view); window.scrollTo(0,0);
    window.dispatchEvent(new CustomEvent('nexus:view',{detail:view}));
    if(view!=='presence' && worldHome && document.getElementById('world-widget')?.parentElement!==worldHome){
      worldHome.append(document.getElementById('world-widget'));
      window.dispatchEvent(new Event('hud:layout'));
    }
    if(view==='world') {
      try {
        await ready;
        if(gui.view==='world')await gui.mountWorld(worldHome,(path,body)=>gui.api('/api/'+path,body));
      } catch {worldLoaded=false;worldImport=null;document.getElementById('world-status').textContent='World assets unavailable; check local installation.';}
    }
  }
  window.addEventListener('nexus:world-settings',()=>{if(worldLoaded && gui.view==='world')window.dispatchEvent(new CustomEvent('hud:connected',{detail:{api:(path,body)=>gui.api('/api/'+path,body)}}));});
  const tabs=[...document.querySelectorAll('[data-gui-tab]')];
  tabs.forEach((n,index)=>{n.onclick=()=>show(n.dataset.guiTab);n.onkeydown=e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(index+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;tabs[next].focus();show(tabs[next].dataset.guiTab);};});
  document.addEventListener('click', e => {
    const a=e.target.closest('a[href^="#"]');if(!a)return;
    const target=document.getElementById(a.getAttribute('href').slice(1));
    const view=target?.closest('[data-gui-view]')?.dataset.guiView;
    if(view && view!==gui.view)show(view);
  });
  gui.show=show;
  // Hash deep links (#run, #mission, …) follow the address bar without a reload.
  window.addEventListener('hashchange',()=>{const next=location.hash.slice(1);if(VIEWS.includes(next)&&next!==gui.view)show(next);});
  document.getElementById('guiSettings').onclick=()=>show('settings');
  // Shared label helper: feature scripts write into .btn-label so icons survive language switches.
  gui.label=(node,text)=>{if(!node)return;const label=node.querySelector('.btn-label');if(label)label.textContent=text;else node.textContent=text;};
  document.getElementById('guiLogout').onclick=async()=>{
    await gui.api('/api/session',{forget:true});credential='';location.reload();
  };
  authenticate().catch(()=>{document.getElementById('guiLoginError').textContent='Server unavailable; retry.';if(!login.open)login.showModal();});
  window.addEventListener('nexus:event',({detail})=>{
    if(detail.type==='approval_request') {show('run');queueMicrotask(()=>document.getElementById('approvalCard')?.scrollIntoView({block:'nearest'}));}
  });
  const attention=document.createElement('div');attention.id='guiAttention';attention.hidden=true;attention.setAttribute('role','status');
  const text=document.createElement('span'),review=document.createElement('button');review.className='btn';review.textContent='Review background actions';review.onclick=()=>show('operations');attention.append(text,review);document.body.append(attention);
  setInterval(async()=>{if(document.hidden||login.open||gui.principal?.kind==='device'||gui.view==='operations')return;try{const state=await gui.api('/api/operations/state');const count=state.pending_actions_total||0;text.textContent=count+' background action(s) await your approval.';attention.hidden=count===0;}catch{/* No invented alert when status is unavailable. */}},10000);
  window.addEventListener('nexus:view',e=>{if(e.detail==='operations')attention.hidden=true;});
  const hash=location.hash.slice(1);const bookmarked=document.getElementById(hash)?.closest('[data-gui-view]')?.dataset.guiView;
  show(bookmarked||(VIEWS.includes(hash)?hash:'mission'));
})();

/* Desktop presentation: reuse native controls; never execute or grant permissions. */
document.addEventListener('DOMContentLoaded', () => {
  'use strict';
  const gui = window.nexusGui;
  /* English stays the in-markup default; i18n.js answers when it is loaded. */
  const tr = (key, vars, fallback) => {
    const value = window.nexusI18n?.t?.(key, vars);
    if (value && value !== key) return value;
    return vars ? fallback.replace(/\{(\w+)\}/g, (_, name) => (vars[name] ?? '')) : fallback;
  };
  const make = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; };
  for (const [view, title, description, titleKey, descKey] of [
    ['run', 'Run & chat', 'Compose tasks, follow actual tool activity, and review permission requests before actions execute.',
      'gui.heading.run', 'gui.desc.run'],
    ['operations', 'Your work, in focus', 'Track queued work, inspect stored evidence, and approve only the exact actions you intend.',
      'gui.heading.operations', 'gui.desc.operations'],
    ['world', 'A wider perspective', 'Explore the globe, public headlines and weather. Source availability and freshness remain visible.',
      'gui.heading.world', 'gui.desc.world']
  ]) {
    const header = make('header', undefined, 'ui-section-heading'), text = make('div');
    const kicker = make('small', view === 'world' ? 'World intelligence' : 'Desktop workspace');
    kicker.dataset.i18n = view === 'world' ? 'gui.kicker.world' : 'gui.kicker.desktop';
    const heading = make('h2', title); heading.dataset.i18n = titleKey;
    const note = make('p', description); note.dataset.i18n = descKey;
    text.append(kicker, heading, note);
    const shortcut = make('span', 'Ctrl + Shift + P · Find controls', 'ui-shortcut');
    shortcut.dataset.i18n = 'gui.shortcut';
    header.append(text, shortcut);
    document.querySelector(`[data-gui-view="${view}"]`)?.prepend(header);
  }
  const preferences = make('div', undefined, 'ui-preferences'); preferences.setAttribute('role', 'group');
  preferences.setAttribute('data-i18n-aria', 'settings.appearance');
  const preferencesTitle = make('span', 'Display', 'ui-preferences-title'); preferencesTitle.dataset.i18n = 'group.display';
  preferences.append(preferencesTitle);
  /* The state lives in shell.js (window.nexusAppearance) so these buttons and
     the Settings page always agree; this file only draws and forwards clicks.
     `data-pref` is a stable hook: the labels themselves change with language. */
  const appearance = window.nexusAppearance;
  const drawn = [];
  for (const [key, label, i18nKey] of [['compact', 'Compact layout', 'settings.compact'], ['still', 'Reduce effects', 'settings.still']]) {
    const button = make('button', label, 'btn'); button.type = 'button';
    button.dataset.pref = key; button.dataset.i18n = i18nKey;
    drawn.push([key, button]);
    button.onclick = () => appearance?.toggle(key);
    preferences.append(button);
  }
  const paintPreferences = () => { for (const [key, button] of drawn) button.setAttribute('aria-pressed', String(Boolean(appearance?.get(key)))); };
  paintPreferences();
  window.addEventListener('nexus:appearance', paintPreferences);
  document.querySelector('.gui-navigation')?.append(preferences);
  /* i18n.js may already have painted the static shell; translate the row we just built. */
  window.nexusI18n?.apply?.(preferences);
  const grid = document.getElementById('dcGrid'), search = document.getElementById('dcSearch');
  if (grid && search) {
    /* `title`/`categoryOf` are the stable English identity used by favourites and
       filters; `shown` is what the card currently displays, in either language. */
    const cards = [...grid.querySelectorAll('.dc-card')];
    const title = card => card.dataset.controlName || card.querySelector('h2').textContent;
    const shown = card => card.querySelector('h2').textContent;
    const categoryOf = card => card.dataset.categoryName || card.querySelector('.dc-category').textContent.trim();
    let saved = [];
    try { const data = JSON.parse(localStorage.getItem('nexus.ui.favourites') || '[]'); if (Array.isArray(data)) saved = data.slice(0, 100).filter(value => typeof value === 'string'); } catch { /* Malformed or blocked storage. */ }
    const favourites = new Set(saved.filter(value => cards.some(card => title(card) === value)));
    let category = 'All controls';
    const bar = make('div', undefined, 'ui-filter-bar'), filters = make('div', undefined, 'ui-category-filters');
    filters.setAttribute('role', 'group'); filters.setAttribute('aria-label', 'Filter controls by category');
    filters.dataset.i18nAria = 'gui.filterLabel';
    const result = make('p', undefined, 'ui-result-count'); result.setAttribute('role', 'status');
    const clear = make('button', 'Clear filters', 'btn ghost'); clear.type = 'button'; clear.dataset.i18n = 'gui.clearFilters';
    function filter() {
      let count = 0; const query = search.value.trim().toLowerCase();
      for (const card of cards) {
        const match = category === 'All controls' || (category === 'Favourites' ? favourites.has(title(card)) : categoryOf(card) === category);
        card.hidden = !match || ![title(card), shown(card), categoryOf(card), card.querySelector('p').textContent].join(' ').toLowerCase().includes(query);
        if (!card.hidden) count++;
      }
      filters.querySelectorAll('button').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.category === category)));
      const countText = tr('gui.count', {count, total: cards.length}, '{count} of {total} controls');
      result.textContent = countText + (category === 'Favourites'
        ? tr('gui.savedHere', {}, ' · saved in this browser') : '');
      const empty = document.getElementById('dcEmpty'); empty.hidden = count > 0;
      empty.textContent = category === 'Favourites' && !favourites.size
        ? tr('gui.emptyFavourites', {}, 'No favourites yet. Use the star on a control to keep it here.')
        : tr('gui.emptyFilter', {}, 'No controls match these filters. Clear filters or try another feature name.');
      clear.hidden = category === 'All controls' && !query;
    }
    /* `dataset.category` keeps the English identity; only the label is translated. */
    for (const name of ['All controls', 'Favourites', ...new Set(cards.map(categoryOf))]) {
      const button = make('button', name); button.type = 'button'; button.dataset.category = name;
      button.dataset.categoryKey = name === 'All controls' ? 'gui.allControls'
        : name === 'Favourites' ? 'gui.favourites'
        : (cards.find(card => categoryOf(card) === name)?.dataset.categoryKey || 'gui.allControls');
      button.onclick = () => { category = name; filter(); }; filters.append(button);
    }
    const paintFilterLabels = () => {
      for (const button of filters.querySelectorAll('button')) button.textContent = tr(button.dataset.categoryKey, {}, button.dataset.category);
      filter();
    };
    const icons = {Execution:'M5 4l14 8-14 8z',Intelligence:'M8 3h8v4h4v10h-4v4H8v-4H4V7h4z M9 10h6v4H9z',Security:'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6z M8 12l3 3 5-6',Privacy:'M7 10V7a5 5 0 0110 0v3 M5 10h14v11H5z M12 14v3',World:'M3 12h18 M12 3c-6 5-6 13 0 18 6-5 6-13 0-18 M3 12a9 9 0 1018 0 9 9 0 10-18 0'};
    for (const card of cards) {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true'); svg.classList.add('ui-card-icon');
      const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', icons[categoryOf(card)] || 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z'); svg.append(path); card.prepend(svg);
      const star = make('button', undefined, 'ui-favourite'); star.type = 'button';
      const sync = () => { const active = favourites.has(title(card)); star.textContent = active ? '★' : '☆'; star.setAttribute('aria-label', tr('gui.favourite', {title: shown(card)}, 'Favourite {title}')); star.setAttribute('aria-pressed', String(active)); card.classList.toggle('ui-saved', active); };
      star.onclick = () => { const name = title(card); if (favourites.has(name)) favourites.delete(name); else favourites.add(name); sync(); filter(); try { localStorage.setItem('nexus.ui.favourites', JSON.stringify([...favourites])); } catch { /* Session-only fallback. */ } };
      sync(); card.append(star);
    }
    search.oninput = filter; clear.onclick = () => { search.value = ''; category = 'All controls'; filter(); search.focus(); };
    bar.append(filters, clear); document.querySelector('.dc-toolbar').after(bar, result);
    window.nexusI18n?.apply?.(bar); window.nexusI18n?.apply?.(result);
    paintFilterLabels();
    document.addEventListener('nexus:language', () => { paintFilterLabels(); for (const card of cards) { const star = card.querySelector('.ui-favourite'); if (star) star.setAttribute('aria-label', tr('gui.favourite', {title: shown(card)}, 'Favourite {title}')); } });
    const actions = make('div', undefined, 'ui-quick-actions'); actions.setAttribute('role', 'group');
    actions.setAttribute('aria-label', 'Quick navigation'); actions.dataset.i18nAria = 'gui.quickNav';
    for (const [label, view, focus, key] of [['Compose a task', 'run', 'taskInput', 'gui.compose'],
      ['Review approvals', 'operations', 'reviews', 'gui.review']]) {
      const button = make('button', label, 'btn'); button.type = 'button'; button.dataset.i18n = key; button.onclick = () => { gui.show(view); const target = document.getElementById(focus); target?.scrollIntoView({block:'center'}); if (focus === 'taskInput') target?.focus(); }; actions.append(button);
    }
    document.querySelector('.dc-hero > div')?.append(actions);
  }
  const strip = make('aside', undefined, 'ui-live-strip'); strip.setAttribute('aria-label', 'Configuration snapshot');
  for (const [id, label] of [['metaUplink', 'Connection'], ['metaRoles', 'Registered roles'], ['metaAgents', 'Configured agents']]) {
    const item = make('span', label + ' '), value = make('b'), source = document.getElementById(id); item.append(value); strip.append(item);
    if (source) { const sync = () => { const next = source.textContent.trim() || 'Unavailable'; if (next === value.textContent) return; value.textContent = next; value.classList.remove('ui-updated'); void value.offsetWidth; value.classList.add('ui-updated'); }; sync(); new MutationObserver(sync).observe(source, {childList:true, characterData:true, subtree:true}); }
  }
  strip.append(make('span', 'Live GUI state · configuration is not endpoint health', 'ui-source')); document.querySelector('.dc-hero')?.after(strip);
  function enter(view) { const host = document.querySelector(`[data-gui-view="${view}"]`); if (host) { host.classList.remove('ui-enter'); void host.offsetWidth; host.classList.add('ui-enter'); } }
  window.addEventListener('nexus:view', event => enter(event.detail)); enter(gui.view);
}, {once:true});
