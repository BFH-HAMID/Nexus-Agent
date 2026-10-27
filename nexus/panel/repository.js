/* Real repository controls; no mutation on load, selection, refresh, or preview. */
(() => {
  'use strict';
  const token = new URLSearchParams(location.search).get('token');
  const bar = document.createElement('section'); bar.id = 'repoBar'; bar.setAttribute('aria-label','Repository controls');
  const branchIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="6" cy="5" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="19" r="3"/><path d="M6 8v8m12-7a9 9 0 0 1-9 9"/></svg>';
  const repositoryIcon = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 19c-4.3 1.3-4.3-2.2-6-2.7m12 5v-3.8c.1-1-.3-1.8-.8-2.3 2.7-.3 5.5-1.3 5.5-6A4.7 4.7 0 0 0 18.4 6c.1-.3.6-1.6-.1-3.3 0 0-1-.3-3.3 1.2a11.5 11.5 0 0 0-6 0C6.7 2.4 5.7 2.7 5.7 2.7 5 4.4 5.5 5.7 5.6 6A4.7 4.7 0 0 0 4.3 9c0 4.7 2.8 5.7 5.5 6-.4.4-.8 1.1-.8 2.2V21"/></svg>';
  bar.innerHTML = `<div class="repo-identity">${repositoryIcon}<div class="repo-picker"><input id="repoSearch" type="search" aria-label="Search GitHub repositories" placeholder="Search GitHub repos…" autocomplete="off"><select id="repoSelect" aria-label="GitHub repository"><option value="">Loading GitHub repositories…</option></select></div><span class="repo-divider" aria-hidden="true"></span><span class="repo-branch-wrap">${branchIcon}<span id="repoBranch" title="Current branch (read-only)">Branch unavailable</span></span></div><div class="repo-controls"><button id="repoCounts" type="button" aria-label="Review changed files" title="Review changed files before committing"><span id="repoAdded">Diff unavailable</span> <span id="repoDeleted"></span><span id="repoBinary"></span><span aria-hidden="true"> ›</span></button><button data-repo-action="create-pr" type="button">${branchIcon} Create PR</button><button id="repoMore" type="button" aria-label="Repository actions" aria-controls="repoActions" aria-expanded="false">⋯</button></div><span id="repoScope" hidden>Git controls only · task workspace unchanged</span><span id="repoBarError" role="status"></span>`;
  document.querySelector('.composer .main-row').after(bar);
  const menu = document.createElement('div'); menu.id='repoActions';menu.setAttribute('popover','auto');menu.setAttribute('aria-label','Repository actions');
  menu.innerHTML=`<button id="repoRefresh" type="button">↻ Refresh GitHub repositories</button><button data-repo-action="commit" type="button">Commit selected files…</button><button data-repo-action="push" type="button">↑ Push branch…</button><button id="repoPRs" type="button">View pull requests…</button><button data-repo-action="merge" type="button">Merge PR…</button>`;
  document.body.append(menu);
  const dialog = document.createElement('dialog'); dialog.id = 'repoDialog';
  dialog.innerHTML = `<header><h2>Repository review</h2><button id="repoClose" type="button">Close</button></header><p id="repoContext"></p><p class="repo-warning">Git controls only: selecting a repository never switches the agent workspace or checks out a branch. Review secrets and hook behavior before confirming. Stop active tasks/workflows/dev apps before mutations.</p><div id="repoForm"></div><div class="repo-actions"><button id="repoPreview" type="button">Preview action</button></div><pre id="repoOutput">No action previewed. Nothing executed.</pre><label>Type the exact confirmation displayed in the preview<input id="repoConfirmation" autocomplete="off"></label><button id="repoExecute" type="button" disabled>Execute reviewed action</button><p id="repoError" role="alert"></p>`;
  document.body.append(dialog);
  const $ = id => document.getElementById(id);
  let status = null, preview = null, action = null, pending = false, generation = 0;
  let repositories = [], loadGeneration = 0, statusGeneration = 0;
  async function api(path, body) {
    const response = await fetch('/api/workbench/repository/' + path, {method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json',...(token?{'X-Nexus-Token':token}:{})},...(body===undefined?{}:{body:JSON.stringify(body)})});
    const result = await response.json(); if(!response.ok) throw new Error(result.error || 'Repository operation failed'); return result.data;
  }
  function error(id, e) { $(id).textContent = e.message || String(e); }
  function output(value) { $('repoOutput').textContent = typeof value === 'string' ? value : JSON.stringify(value,null,2); }
  function invalidate() { generation++; preview=null; $('repoExecute').disabled=true; $('repoConfirmation').value=''; }
  function input(label,id,multiline=false,value='') { const wrap=document.createElement('label');wrap.textContent=label; const node=document.createElement(multiline?'textarea':'input');node.id=id;node.value=value;wrap.append(node);$('repoForm').append(wrap);node.addEventListener('input',invalidate);return node; }
  function controls() {
    const local = status?.local_available === true;
    const writable = status?.can_push === true && !status?.archived;
    $('repoCounts').disabled = pending || !local;
    for (const button of document.querySelectorAll('#repoBar [data-repo-action], #repoActions [data-repo-action]')) {
      const localAction = ['commit','push'].includes(button.dataset.repoAction);
      button.disabled = pending || !status || !writable || (localAction && !local);
    }
    $('repoPRs').disabled = pending || !status;
    $('repoSelect').disabled = pending;
    $('repoSearch').disabled = pending;
    $('repoRefresh').disabled = pending;
  }
  function renderOptions(selected) {
    const query = $('repoSearch').value.trim().toLowerCase();
    const matches = repositories.filter(repo => repo.name.toLowerCase().includes(query));
    const current = repositories.find(repo => repo.id === selected);
    if(current && !matches.includes(current)) matches.unshift(current);
    $('repoSelect').replaceChildren();
    const placeholder = document.createElement('option');placeholder.value='';placeholder.textContent=repositories.length?'Choose a GitHub repository':'No accessible GitHub repositories';$('repoSelect').append(placeholder);
    for(const repo of matches){const option=document.createElement('option');option.value=repo.id;option.textContent=repo.name+(repo.private?' · private':'')+(repo.archived?' · archived':'');option.dataset.agentWorkspace=String(Boolean(repo.agent_workspace));option.title='GitHub: '+repo.name;$('repoSelect').append(option);}
    if(!matches.length && repositories.length){const option=document.createElement('option');option.disabled=true;option.textContent='No matching repositories';$('repoSelect').append(option);}
    $('repoSelect').value=current?current.id:'';
  }
  async function refresh() {
    const id=$('repoSelect').value; if(!id){status=null;$('repoBranch').textContent='Select a GitHub repository';$('repoAdded').textContent='Local diff unavailable';$('repoDeleted').textContent='';$('repoBinary').textContent='';$('repoScope').hidden=true;controls();return;}
    const epoch=++statusGeneration;
    const measured=await api('status?repo='+encodeURIComponent(id));
    if(id!==$('repoSelect').value||epoch!==statusGeneration)return;status=measured;
    const local=status.local_available===true;
    $('repoBranch').textContent=(status.branch||'empty repository')+(local?'':' · remote');
    $('repoBranch').title=(status.branch||'No default branch')+' · read-only; no checkout';
    $('repoScope').hidden=local;
    $('repoScope').textContent='GitHub PR target · task workspace unchanged; no local checkout';
    $('repoAdded').textContent=local?'+'+status.additions:'Local diff unavailable';$('repoDeleted').textContent=local?'−'+status.deletions:'';$('repoBinary').textContent=status.binary_changes?' · binary changes':'';
    $('repoCounts').title=local?'Actual changes in the matching server workspace':'No matching server workspace. Remote PRs are available; local commit/push are disabled.';
    $('repoBarError').textContent='';controls();
  }
  async function load(force=false) {
    const epoch=++loadGeneration;
    const records=await api('list'+(force?'?refresh=1':''));if(epoch!==loadGeneration)return;
    const selected=$('repoSelect').value;
    repositories=records;
    renderOptions(records.some(repo=>repo.id===selected)?selected:(records.find(repo=>repo.agent_workspace)?.id||''));
    await refresh();
  }
  function branchSelect(label,id,branches,value) {
    const wrap=document.createElement('label');wrap.textContent=label;
    const select=document.createElement('select');select.id=id;
    const empty=document.createElement('option');empty.value='';empty.textContent='Choose a remote branch';select.append(empty);
    for(const branch of branches){const option=document.createElement('option');option.value=branch;option.textContent=branch;select.append(option);}
    select.value=branches.includes(value)?value:'';select.onchange=invalidate;wrap.append(select);$('repoForm').append(wrap);
  }
  async function show(kind) {
    if(pending) return;menu.hidePopover();action=kind;invalidate();const revision=generation;$('repoForm').replaceChildren();$('repoError').textContent='';output('Review this operation. Preview does not commit, push, create or merge.');
    if(!dialog.open)dialog.showModal();
    await refresh(); if(revision!==generation||action!==kind)return; if(!status)throw new Error('Choose a repository first');
    $('repoContext').textContent=status.name+' · '+(status.branch||'empty repository')+(status.local_available?' · matching workspace '+status.path:' · GitHub remote; agent workspace unchanged');
    $('repoPreview').hidden=false;$('repoExecute').hidden=false;$('repoConfirmation').parentElement.hidden=false;
    if(['commit','push'].includes(kind)&&status.local_available!==true)throw new Error('No matching server workspace; remote selection cannot commit or push another project.');
    if(kind==='commit') {
      const note=document.createElement('p');note.textContent='Select exact files. Unrelated staged files are not committed. Signing/hooks may run; failures may leave selected files staged.';$('repoForm').append(note);
      for(const path of status.files){const label=document.createElement('label');label.className='repo-file';const check=document.createElement('input');check.type='checkbox';check.value=path;check.name='repoPath';check.onchange=invalidate;label.append(check,document.createTextNode(path));$('repoForm').append(label);}
      if(!status.files.length) output('No selectable changes. Private/runtime paths are excluded.');
      input('Commit message','repoMessage',true);
    } else if(kind==='create-pr') {
      const branches=await api('branches?repo='+encodeURIComponent($('repoSelect').value));if(revision!==generation||action!==kind)return;
      const base=status.default_branch||'main';
      branchSelect('Remote base branch','repoBase',branches,base);
      branchSelect('Remote head branch (already pushed)','repoHead',branches,status.branch!==base?status.branch:branches.find(branch=>branch!==base));
      input('PR title','repoTitle');input('Reviewed PR body','repoBody',true);
    }
    else if(kind==='merge'){input('Explicit PR number','repoNumber');const select=document.createElement('select');select.id='repoMethod';select.setAttribute('aria-label','Merge method');for(const method of ['merge','squash','rebase']){const option=document.createElement('option');option.value=method;option.textContent=method;select.append(option);}select.onchange=invalidate;$('repoForm').append(select);}
    else if(kind==='push'){output('Push only already committed HEAD to the selected origin branch. No force push, reset, implicit commit or branch switch.');}
    else if(kind==='prs'){
      $('repoPreview').hidden=true;$('repoExecute').hidden=true;$('repoConfirmation').parentElement.hidden=true;
      const prs=await api('prs?repo='+encodeURIComponent($('repoSelect').value));if(revision!==generation)return;output(prs.length?prs:'No open PRs found for this GitHub repository.');
      for(const pr of prs){const row=document.createElement('div');const link=document.createElement('a');const url=new URL(pr.url);if(url.protocol==='https:'&&url.hostname==='github.com'){link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';link.textContent='#'+pr.number+' '+pr.title;row.append(link);}const merge=document.createElement('button');merge.textContent='Review merge #'+pr.number;merge.onclick=async()=>{try{const opening=show('merge'),epoch=generation;await opening;if(epoch===generation&&action==='merge'&&dialog.open&&$('repoNumber'))$('repoNumber').value=String(pr.number);}catch(e){error('repoError',e);}};row.append(merge);$('repoForm').append(row);}
    }
  }
  $('repoSearch').oninput=()=>renderOptions($('repoSelect').value);
  $('repoSelect').onchange=()=>{invalidate();status=null;controls();$('repoBranch').textContent='Reading Git status…';$('repoAdded').textContent='…';$('repoDeleted').textContent='';$('repoBinary').textContent='';if(dialog.open)dialog.close();refresh().catch(e=>error('repoBarError',e));};
  $('repoMore').onclick=()=>menu.togglePopover();
  menu.addEventListener('beforetoggle',event=>{if(event.newState==='open'){const rect=$('repoMore').getBoundingClientRect();menu.style.bottom=Math.max(8,innerHeight-rect.top+6)+'px';menu.style.left=Math.max(8,Math.min(rect.right-224,innerWidth-232))+'px';}});
  menu.addEventListener('toggle',()=>{$('repoMore').setAttribute('aria-expanded',String(menu.matches(':popover-open')));});
  $('repoCounts').onclick=()=>show('commit').catch(e=>error('repoError',e));
  $('repoRefresh').onclick=()=>{menu.hidePopover();invalidate();if(dialog.open)dialog.close();load(true).catch(e=>error('repoBarError',e));};
  document.querySelectorAll('#repoBar [data-repo-action], #repoActions [data-repo-action]').forEach(button=>{button.onclick=()=>show(button.dataset.repoAction).catch(e=>error('repoError',e));});
  $('repoPRs').onclick=()=>show('prs').catch(e=>error('repoError',e));$('repoClose').onclick=()=>{if(!pending)dialog.close();};
  $('repoPreview').onclick=async()=>{if(pending)return;invalidate();$('repoError').textContent='';$('repoPreview').disabled=true;try{const revision=generation;const payload={repo:$('repoSelect').value,action};if(action==='commit'){payload.paths=[...dialog.querySelectorAll('input[name=repoPath]:checked')].map(n=>n.value);payload.message=$('repoMessage').value;}if(action==='create-pr'){payload.base=$('repoBase').value;payload.head=$('repoHead').value;payload.title=$('repoTitle').value;payload.body=$('repoBody').value;}if(action==='merge'){payload.number=Number($('repoNumber').value);payload.method=$('repoMethod').value;}const reviewed=await api('preview',payload);if(revision!==generation||payload.repo!==$('repoSelect').value||payload.action!==action)return;preview=reviewed;output(preview);}catch(e){error('repoError',e);}finally{$('repoPreview').disabled=false;}};
  $('repoConfirmation').oninput=()=>{$('repoExecute').disabled=pending||!preview||$('repoConfirmation').value!==preview.confirmation;};
  $('repoExecute').onclick=async()=>{if(pending||!preview)return;pending=true;controls();$('repoExecute').disabled=true;$('repoClose').disabled=true;$('repoPreview').disabled=true;try{const result=await api('execute',{preview_id:preview.id,confirm:$('repoConfirmation').value});output(result);invalidate();await refresh();}catch(e){invalidate();error('repoError',e);}finally{pending=false;controls();$('repoClose').disabled=false;$('repoPreview').disabled=false;}};
  dialog.addEventListener('cancel',event=>{if(pending)event.preventDefault();});
  window.addEventListener('resize',()=>menu.hidePopover());
  window.addEventListener('nexus:event',({detail:e})=>{if(e.type==='approval_request'&&dialog.open&&!pending)dialog.close();if(e.type==='done'){invalidate();refresh().catch(err=>error('repoBarError',err));}});
  controls();
  load().catch(e=>{status=null;controls();$('repoSelect').replaceChildren(new Option('GitHub repositories unavailable',''));error('repoBarError',e);});
})();
