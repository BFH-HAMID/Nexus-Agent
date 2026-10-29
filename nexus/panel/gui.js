/* One GUI, one identity, one origin. Existing workbench/agent APIs are reused. */
(() => {
  'use strict';
  let credential = new URLSearchParams(location.search).get('token') || '';
  const clean = new URL(location.href); clean.searchParams.delete('token');
  history.replaceState(null, '', clean);
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });
  const login = document.createElement('dialog'); login.id = 'guiLogin';
  login.innerHTML = '<form><h2>Nexus dashboard access</h2><p>Use the existing GUI access token, not a model/GitHub key. This login covers every section.</p><label>Private access token<input id="guiToken" type="password" autocomplete="off" required></label><label><input id="guiRemember" type="checkbox" checked> Remember this trusted browser</label><button class="btn" type="submit">Connect</button><p id="guiLoginError" role="alert"></p></form>';
  document.body.append(login);
  async function authenticate() {
    const headers = credential ? {'X-Nexus-Token': credential} : {};
    const response = await fetch('/api/session', {headers});
    if (!response.ok) { if (!login.open) login.showModal(); return; }
    const info=await response.json();
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
      const value = await r.json(); if(!r.ok) throw Error(value.error || value.detail || 'Request refused'); return value;
    },
    view: 'run'
  };
  let worldLoaded = false;
  async function show(view) {
    if (!['run','operations','world'].includes(view)) return;
    gui.view = view;
    document.body.classList.toggle('gui-module-view',view!=='run');
    document.querySelectorAll('[data-gui-view]').forEach(n => n.hidden = n.dataset.guiView !== view);
    document.querySelectorAll('[data-gui-tab]').forEach(n => {const active=n.dataset.guiTab===view;n.setAttribute('aria-selected',String(active));});
    history.replaceState(null,'','#'+view); window.scrollTo(0,0);
    window.dispatchEvent(new CustomEvent('nexus:view',{detail:view}));
    if(view==='world' && !worldLoaded) {
      worldLoaded = true;
      try {
        await ready;
        await import('/world/world.js');
        window.dispatchEvent(new CustomEvent('hud:connected',{detail:{api:(path,body)=>gui.api('/api/'+path,body)}}));
      } catch {worldLoaded=false;document.getElementById('world-status').textContent='World assets unavailable; check local installation.';}
    }
  }
  window.addEventListener('nexus:world-settings',()=>{if(worldLoaded)window.dispatchEvent(new CustomEvent('hud:connected',{detail:{api:(path,body)=>gui.api('/api/'+path,body)}}));});
  const tabs=[...document.querySelectorAll('[data-gui-tab]')];
  tabs.forEach((n,index)=>{n.onclick=()=>show(n.dataset.guiTab);n.onkeydown=e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(index+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;tabs[next].focus();show(tabs[next].dataset.guiTab);};});
  document.addEventListener('click', e => {
    const a=e.target.closest('a[href^="#"]');if(!a)return;
    const target=document.getElementById(a.getAttribute('href').slice(1));
    const view=target?.closest('[data-gui-view]')?.dataset.guiView;
    if(view && view!==gui.view)show(view);
  });
  gui.show=show;
  document.getElementById('guiSettings').onclick=()=>document.getElementById('wbOpen')?.click();
  document.getElementById('guiLogout').onclick=async()=>{
    await gui.api('/api/session',{forget:true});credential='';location.reload();
  };
  authenticate().catch(()=>{document.getElementById('guiLoginError').textContent='Server unavailable; retry.';if(!login.open)login.showModal();});
  window.addEventListener('nexus:event',({detail})=>{
    if(detail.type==='approval_request') {show('run');queueMicrotask(()=>document.getElementById('approvalCard')?.scrollIntoView({block:'nearest'}));}
  });
  const attention=document.createElement('div');attention.id='guiAttention';attention.hidden=true;attention.setAttribute('role','status');
  const text=document.createElement('span'),review=document.createElement('button');review.className='btn';review.textContent='Review background actions';review.onclick=()=>show('operations');attention.append(text,review);document.querySelector('.gui-navigation').after(attention);
  setInterval(async()=>{if(document.hidden||login.open||gui.view==='operations')return;try{const state=await gui.api('/api/operations/state');const count=state.pending_actions_total||0;text.textContent=count+' background action(s) await your approval.';attention.hidden=count===0;}catch{/* No invented alert when status is unavailable. */}},10000);
  window.addEventListener('nexus:view',e=>{if(e.detail==='operations')attention.hidden=true;});
  const hash=location.hash.slice(1);const bookmarked=document.getElementById(hash)?.closest('[data-gui-view]')?.dataset.guiView;
  show(bookmarked||(['operations','world'].includes(hash)?hash:'run'));
})();
