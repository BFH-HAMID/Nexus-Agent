/* Review-first Studio. All server-derived text is rendered with textContent. */
(() => {
  'use strict';
  const token = ''; // Shared HttpOnly GUI session; no URL credentials.
  const dialog = document.createElement('dialog'); dialog.id = 'nexusStudio';
  dialog.innerHTML = `<div class="wb-shell"><header class="wb-header"><h2>Nexus Studio · Review & evidence</h2><button id="stClose">Close</button></header><nav class="wb-nav" id="stNav"></nav><main class="wb-main"><p id="stError" role="status"></p><div id="stPane"></div></main></div>`;
  document.body.append(dialog);
  const $ = id => dialog.querySelector('#' + id);
  const open = document.createElement('button'); open.className = 'btn'; open.id = 'studioOpen'; open.textContent = 'Studio';
  document.querySelector('.topbar').append(open);
  let selected = null, activePane = 'Sessions', attachments = [], notify = false;
  async function api(path, body) {
    const response = await (window.nexusGui?.fetch || fetch)('/api/workbench/studio/' + path, {method: body === undefined ? 'GET' : 'POST',
      headers: {'Content-Type':'application/json', ...(token ? {'X-Nexus-Token':token} : {})},
      ...(body === undefined ? {} : {body:JSON.stringify(body)})});
    const result = await response.json(); if (!response.ok) throw new Error(result.error || 'Request failed');
    return result.data === undefined ? result : result.data;
  }
  const action = fn => async () => { $('stError').textContent = ''; try { await fn(); } catch (e) { $('stError').textContent = e.message; } };
  function button(label, fn, parent = $('stPane')) { const b = document.createElement('button'); b.textContent = label; b.onclick = action(fn); parent.append(b); return b; }
  function pre(value, parent = $('stPane')) { const node = document.createElement('pre'); node.className = 'wb-output'; node.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2); parent.append(node); return node; }
  function input(label, multiline = false, value = '') { const wrap = document.createElement('label'); wrap.textContent = label; const node = document.createElement(multiline ? 'textarea' : 'input'); node.value = value; wrap.append(node); $('stPane').append(wrap); return node; }
  const confirm = expected => { const value = prompt('Type exactly: ' + expected); if (value !== expected) throw new Error('Confirmation cancelled'); return value; };
  function download(value, filename, type = 'application/json') { const url = URL.createObjectURL(new Blob([typeof value === 'string' ? value : JSON.stringify(value, null, 2)], {type})); const a = document.createElement('a'); a.href = url; a.download = filename; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
  async function records(kind) { return api('items?kind=' + encodeURIComponent(kind)); }
  async function sessions() {
    attachments = [];
    const search = input('Search conversations'); const tabs = document.createElement('div'); tabs.className = 'wb-row'; $('stPane').append(tabs);
    async function list() { tabs.replaceChildren(); for (const c of await api('items?kind=conversation&q=' + encodeURIComponent(search.value))) button(c.title, async () => { selected = c.id; await show('Sessions'); }, tabs); }
    button('Search', list); button('New session', async () => { const title = prompt('Session title'); if (!title) return; selected = (await api('conversation', {title})).id; await show('Sessions'); }); await list();
    if (!selected) return;
    const c = (await records('conversation')).find(x => x.id === selected); if (!c) { selected = null; return; }
    pre('Session: ' + c.title + ' · original messages are immutable; editing creates a branch.');
    c.messages.forEach((m, index) => { pre(m.role + ': ' + m.text); if (m.task_id) button('Open run ' + m.task_id, async () => { await window.nexusAttachTask(m.task_id); dialog.close(); }); if (m.role === 'user') button('Edit / branch message', async () => { const text = prompt('New user message (original is preserved)', m.text); if (text === null) return; selected = (await api('conversation', {branch_from:c.id, index, text, title:c.title + ' / branch'})).id; await show('Sessions'); }); });
    const task = input('Task', true); const criteria = input('Acceptance criteria (one per line; no automatic pass claim)', true);
    const templates = await records('template'); for (const t of templates) button('Use template: ' + t.title, async () => { task.value = t.text; });
    button('Save task as template', async () => { const title = prompt('Template title'); if (title) pre(await api('template', {title, text:task.value})); });
    const file = input('Attach up to five text/code files (300 KB total)'); file.type = 'file'; file.multiple = true;
    file.onchange = action(async () => { attachments = []; let size = 0; for (const f of file.files) { size += f.size; if (file.files.length > 5 || size > 300000) throw new Error('Attachment limit exceeded'); const text = await f.text(); if (text.includes('\0') || text.includes('\uFFFD')) throw new Error('Only UTF-8 text files are accepted'); attachments.push({name:f.name, text}); } pre(attachments.map(a => ({name:a.name, characters:a.text.length}))); });
    const profiles = document.createElement('select'); const option = document.createElement('option'); option.value = ''; option.textContent = 'Default model settings'; profiles.append(option); for (const p of await records('profile')) { const o = document.createElement('option'); o.value = p.id; o.textContent = p.name; profiles.append(o); } $('stPane').append(profiles);
    button('Scan task + attachments for secrets', async () => pre(await api('secret-scan', {text:task.value + '\n' + JSON.stringify(attachments)})));
    for (const mode of ['plan', 'execute']) button(mode === 'plan' ? 'Send read-only plan' : 'Send task (normal approvals)', async () => { if (file.files.length && attachments.length !== file.files.length) throw new Error('Wait for valid text attachments to finish loading, or clear the selection'); const r = await api('send', {id:c.id, text:task.value, criteria:criteria.value.split('\n').filter(Boolean), attachments, profile_id:profiles.value, mode}); attachments = []; await window.nexusAttachTask(r.task_id); dialog.close(); });
  }
  async function review() {
    pre('Drafts are inactive until approval. Skills require actual linked repository checks plus human coverage review. Active memory expires after 30 days for future runs; exact entries can be revoked.');
    const memory = input('New memory draft', true); button('Stage memory', async () => { await api('memory-draft', {text:memory.value}); await show('Review'); });
    for (const d of await records('memory-draft')) { pre(d); if (d.status === 'approve') button('Revoke active memory ' + d.id, async () => pre(await api('memory-revoke', {id:d.id, confirm:confirm('REVOKE ' + d.id)}))); if (d.status !== 'draft') continue; button('Approve / edit ' + d.id, async () => { const text = prompt('Review active memory text', d.text); if (text === null) return; pre(await api('memory-review', {id:d.id, action:'approve', text})); }); button('Reject ' + d.id, async () => pre(await api('memory-review', {id:d.id, action:'reject'}))); }
    const skill = input('Skill draft JSON: name, description, body, tags', true); button('Stage inactive skill', async () => { await api('skill-draft', {proposal:JSON.parse(skill.value)}); await show('Review'); });
    for (const d of await records('skill-draft')) { pre(d); button('Lint ' + d.id, async () => pre(await api('skill-check', {id:d.id}))); button('Runtime test ' + d.id, async () => pre(await api('skill-runtime-test', {id:d.id, confirm:confirm('RUN DRAFT SKILL TEST')}))); button('Review skill change ' + d.id, async () => pre(await api('skill-preview', {id:d.id}))); button('Reject skill ' + d.id, async () => pre(await api('skill-reject', {id:d.id}))); if (d.status === 'draft') button('Promote reviewed skill ' + d.id, async () => pre(await api('skill-promote', {id:d.id, confirm:confirm('PROMOTE REVIEWED SKILL')}))); }
  }
  async function evidence() {
    pre('Only recorded subprocess results are verification evidence. Commands execute repository tooling on this host, NOT in an OS sandbox.');
    for (const kind of ['tests', 'lint', 'types']) button('Run ' + kind, async () => pre(await api('verify', {kind, confirm:confirm('RUN TRUSTED CHECK')})));
    button('Refresh verification jobs', async () => { const r = await (window.nexusGui?.fetch || fetch)('/api/workbench/jobs', {headers:token ? {'X-Nexus-Token':token} : {}}); pre(await r.json()); });
    for (const kind of ['verification', 'evidence', 'context']) button('Inspect ' + kind, async () => pre(await records(kind)));
    for (const evidence of await records('evidence')) {
      if (!evidence.criteria.length) continue;
      pre('Acceptance checklist · run ' + evidence.task_id + ' (human review, not test proof)');
      const checks = evidence.criteria.map((text, index) => { const label = document.createElement('label'); const check = document.createElement('input'); check.type = 'checkbox'; check.checked = (evidence.human_checked || []).includes(index); label.append(check, document.createTextNode(text)); $('stPane').append(label); return check; });
      button('Save human checklist ' + evidence.task_id, async () => pre(await api('criteria-review', {id:evidence.id, checked:checks.flatMap((c, i) => c.checked ? [i] : [])})));
    }
    button('Review dependency changes', async () => pre(await api('dependencies')));
    for (const c of await records('checkpoint')) { pre({id:c.id, path:c.path, status:c.status, task_id:c.task_id}); button('Preview rollback ' + c.id, async () => pre(await api('rollback-preview', {id:c.id}))); button('Restore reviewed checkpoint ' + c.id, async () => pre(await api('rollback', {id:c.id, confirm:confirm('RESTORE ' + c.id)}))); }
    const id = input('Pending task ID (from live run)'); button('Inspect selectable diff hunks', async () => {
      const response = await (window.nexusGui?.fetch || fetch)('/api/task/' + encodeURIComponent(id.value), {headers:token ? {'X-Nexus-Token':token} : {}}); const run = await response.json(); if (!response.ok) throw new Error(run.error || 'Cannot read task');
      const approval = run.pending; if (!approval) throw new Error('No pending approval');
      const h = await api('hunks', {task_id:id.value, approval_id:approval.approval_id}); const chosen = [];
      for (const change of h.changes) { pre(change); const label = document.createElement('label'); const check = document.createElement('input'); check.type = 'checkbox'; check.checked = true; chosen.push({id:change.id, check}); label.append(check, document.createTextNode('Include hunk ' + change.id)); $('stPane').append(label); }
      button('Update proposal only — still requires normal approval', async () => pre(await api('hunks', {task_id:id.value, approval_id:approval.approval_id, selected:chosen.filter(c => c.check.checked).map(c => c.id)})));
    });
  }
  async function policy() {
    const settings = await api('settings'); notify = settings.notifications; pre('Policy applies to new runs. Tool permissions are not OS isolation. Time limit blocks subsequent requests/tools; it cannot cancel an already in-flight provider request. Context inspection includes actual provider JSON bodies after conversion, never authorization headers.');
    const edit = input('Studio settings JSON (role_permissions, fallback map, budgets, review gates)', true, JSON.stringify(settings, null, 2));
    button('Save validated policy', async () => { const s = await api('settings', JSON.parse(edit.value)); notify = s.notifications; pre(s); });
    button('Enable browser completion notifications (opt-in)', async () => { if (!('Notification' in window)) throw new Error('Browser notifications unavailable'); if (await Notification.requestPermission() !== 'granted') throw new Error('Notification permission not granted'); await api('settings', {notifications:true}); notify = true; pre('Enabled for runs observed by this page. Disable via notifications:false in settings.'); });
    const parameters = input('Model parameters (billions)', false, '8'); const quant = input('Weight quantization bits', false, '4'); const windowSize = input('Context for fit estimate', false, '8192');
    button('Inspect provider circuit states', async () => pre(await api('circuits'))); button('Check actual hardware/inventory', async () => pre(await api('hardware-fit?parameters_b=' + encodeURIComponent(parameters.value) + '&bits=' + encodeURIComponent(quant.value) + '&context=' + encodeURIComponent(windowSize.value))));
    pre(await api('availability'));
    const name = input('Profile name'); const temp = input('Temperature 0–2', false, '0.2'); const max = input('Max output tokens', false, '2048'); const context = input('Documented model context tokens (user supplied)', false, '8192');
    button('Save model profile', async () => pre(await api('profile', {name:name.value, temperature:Number(temp.value), max_tokens:Number(max.value), context_tokens:Number(context.value)})));
    pre(await records('profile')); const model = input('Installed Ollama model'); for (const op of ['load', 'unload']) button(op + ' model…', async () => pre(await api('model-' + op, {model:model.value, confirm:confirm(op.toUpperCase() + ' ' + model.value)})));
  }
  async function data() {
    pre('Exports use heuristic redaction, not a guarantee of secret removal. Inspect before sharing. Backup excludes credentials, live processes, checkpoints and OS keys.');
    button('Download redacted audit JSON', async () => download(await api('audit'), 'nexus-audit.json'));
    button('Download redacted audit CSV', async () => download(await api('audit?format=csv'), 'nexus-audit.csv', 'text/csv'));
    button('Download portable backup', async () => download(await api('backup', {}), 'nexus-backup.json'));
    const restore = input('Reviewed backup JSON', true); button('Import as new inactive records', async () => pre(await api('restore', {backup:JSON.parse(restore.value), confirm:confirm('IMPORT REVIEWED BACKUP')})));
    button('Delete runs older than retention setting…', async () => pre(await api('retention', {confirm:confirm('DELETE OLD HISTORY')})));
    button('Create detached committed checkout…', async () => pre(await api('checkout', {confirm:confirm('CREATE ISOLATED CHECKOUT')})));
    button('Migrate reviewed backup schema 0/1/2', async () => pre(await api('backup-migrate', {backup:JSON.parse(restore.value)})));
    button('Apply Studio-wide history retention…', async () => pre(await api('retention-all', {confirm:confirm('DELETE EXPIRED STUDIO HISTORY')})));
    const kind = input('Delete collection (conversation/template/context/evidence/workflow/etc.)'); const ident = input('Record ID to delete');
    button('Delete reviewed record…', async () => pre(await api('record-delete', {kind:kind.value,id:ident.value,confirm:confirm('DELETE ' + ident.value)})));
    pre('Encryption covers the full dashboard SQLite file at rest. Project code, CLI stores, active-memory files and old filesystem backups are separate. No key/password is accepted in this form. Configure a supported OS key store on the server first; missing/locked stores fail closed. Stop other Nexus servers before activation.');
    button('Enable OS-keystore encryption…', async () => { const single_server = window.confirm('Are all OTHER Nexus servers using this shared state stopped?'); if (!single_server) return; pre(await api('encryption-enable', {confirm:confirm('ENCRYPT DASHBOARD STATE'),single_server})); });
  }
  let draftWorkflow = {name:'',nodes:[]}, workflowFamily = '';
  async function workflow() {
    pre('Build a DAG. Research/reviewer nodes are strictly non-executing reads and can run in parallel (up to four). All other nodes serialize. Conditions evaluate actual upstream results. Run requires explicit confirmation and ordinary write approvals.');
    const name = input('Workflow name', false, draftWorkflow.name); name.oninput = () => { draftWorkflow.name = name.value; };
    const canvas = document.createElementNS('http://www.w3.org/2000/svg','svg'); canvas.setAttribute('viewBox','0 0 780 360'); canvas.style.width = '100%'; canvas.style.minHeight = '220px'; canvas.setAttribute('aria-label','Workflow graph'); $('stPane').append(canvas);
    function graph() {
      canvas.replaceChildren();
      const pos = new Map(draftWorkflow.nodes.map((n,i)=>[n.id,{x:15+(i%3)*255,y:15+Math.floor(i/3)*90}]));
      canvas.setAttribute('viewBox','0 0 780 ' + Math.max(360,Math.ceil(draftWorkflow.nodes.length/3)*90));
      for(const n of draftWorkflow.nodes) for(const d of n.depends_on) { if(!pos.has(d)) continue; const a=pos.get(d),b=pos.get(n.id); const line=document.createElementNS(canvas.namespaceURI,'line'); for(const [k,v] of Object.entries({x1:a.x+110,y1:a.y+50,x2:b.x+110,y2:b.y,stroke:'#26c4bd','stroke-width':2})) line.setAttribute(k,v); canvas.append(line); }
      for(const n of draftWorkflow.nodes) { const p=pos.get(n.id); const g=document.createElementNS(canvas.namespaceURI,'g'); const rect=document.createElementNS(canvas.namespaceURI,'rect'); for(const[k,v]of Object.entries({x:p.x,y:p.y,width:230,height:55,rx:8,fill:'#12312d',stroke:'#42d9bb'})) rect.setAttribute(k,v); const text=document.createElementNS(canvas.namespaceURI,'text'); text.setAttribute('x',p.x+8);text.setAttribute('y',p.y+22);text.setAttribute('fill','#fff');text.textContent=n.id+' · '+n.kind; const sub=document.createElementNS(canvas.namespaceURI,'text');sub.setAttribute('x',p.x+8);sub.setAttribute('y',p.y+43);sub.setAttribute('fill','#9ecfc2');sub.textContent=n.condition+' ← '+n.depends_on.join(',');g.append(rect,text,sub);g.style.cursor='pointer';g.onclick=action(async()=>{const updated=prompt('Edit node JSON (or enter DELETE)',JSON.stringify(n,null,2));if(updated===null)return;if(updated==='DELETE')draftWorkflow.nodes=draftWorkflow.nodes.filter(x=>x.id!==n.id);else {const parsed=JSON.parse(updated);if(!parsed||typeof parsed!=='object'||typeof parsed.id!=='string'||!Array.isArray(parsed.depends_on))throw new Error('Node requires an id and depends_on array');draftWorkflow.nodes[draftWorkflow.nodes.indexOf(n)]=parsed;}await show('Workflows');});canvas.append(g); }
    }
    graph();
    const id=input('Node ID');const kind=input('Kind: agent / research / reviewer / verification / approval',false,'research');const role=input('Role',false,'general');const task=input('Node task / approval question',true);const dependencies=input('Depends on (comma-separated IDs)');const condition=input('Condition: success / failure / always',false,'success');const budgets=input('Step budgets JSON',true,'{"turns":24,"tools":100,"tokens":32000,"seconds":300}');const check=input('Verification kind: tests / lint / types',false,'tests');
    button('Add visual node',async()=>{draftWorkflow.nodes.push({id:id.value,kind:kind.value,role:role.value,task:task.value,depends_on:dependencies.value.split(',').map(x=>x.trim()).filter(Boolean),condition:condition.value,budgets:JSON.parse(budgets.value),check:check.value});await show('Workflows');});
    button('Dry-run validate (no model/commands)',async()=>pre(await api('workflow-dry-run',{definition:draftWorkflow})));
    button('Save new immutable version',async()=>{const version=await api('workflow-save',{definition:draftWorkflow,family:workflowFamily});workflowFamily=version.family;pre(version);});
    button('New workflow family',async()=>{workflowFamily='';draftWorkflow={name:'',nodes:[]};await show('Workflows');});
    for(const version of await api('workflow-versions')) { pre({id:version.id,family:version.family,version:version.version,name:version.definition.name});button('Load version '+version.id,async()=>{workflowFamily=version.family;draftWorkflow=structuredClone(version.definition);await show('Workflows');});button('Run reviewed version '+version.id,async()=>pre(await api('workflow-run',{id:version.id,confirm:confirm('RUN REVIEWED WORKFLOW')}))); }
    button('Open clarification / approval inbox',()=>show('Inbox'));
    const liveBox=document.createElement('div');$('stPane').append(liveBox);
    const renderLive=async()=>{const recordsLive=await records('workflow-live');liveBox.replaceChildren();for(const live of recordsLive){const line=pre(live,liveBox);line.style.maxHeight='100px';button('Open live node '+live.task_id,async()=>{await window.nexusAttachTask(live.task_id);dialog.close();},liveBox);}};
    await renderLive();
    button('Refresh live node approvals',renderLive);
    button('Refresh workflow runs/handoffs',async()=>{for(const run of await records('workflow-run')){pre(run);button('Cancel workflow '+run.id,async()=>pre(await api('workflow-cancel',{id:run.id})));for(const result of Object.values(run.nodes)) if(result.task_id)button('Open node run '+result.task_id,async()=>{await window.nexusAttachTask(result.task_id);dialog.close();});}});
  }
  async function inbox() {
    pre('Agent clarification requests and workflow approvals. Questions are never silently auto-answered. Restarted/expired questions cannot be answered.');
    button('Refresh inbox',()=>show('Inbox'));
    for(const question of await records('clarification')){pre(question);if(question.status!=='pending')continue;const answer=input('Answer '+question.id+(question.approval?' (APPROVE or DENY)':''),true);button('Send answer '+question.id,async()=>{pre(await api('clarification-answer',{id:question.id,answer:answer.value}));});}
  }
  function previewUrl(port) { const url=new URL(location.href); if(/^\d+-.*\.e2b\.app$/.test(url.hostname)){url.hostname=url.hostname.replace(/^\d+-/,port+'-');url.port='';}else{url.port=String(port);}url.pathname='/';url.search='';url.hash='';return url.href; }
  async function runtime() {
    pre('Host dev apps execute trusted repository code. Bind to 0.0.0.0 and permit your preview host/origin. Environment credentials are not passed. Under enforced isolation, host apps are refused. Logs are bounded and redacted.');
    const argv=input('Dev app argv JSON (not a shell string)',true,'[]');const port=input('Preview port',false,'3000');button('Start trusted app…',async()=>pre(await api('dev-start',{argv:JSON.parse(argv.value),port:Number(port.value),confirm:confirm('START TRUSTED DEV APP')})));
    button('Refresh apps / logs',()=>show('Runtime'));
    for(const app of await api('dev-apps')) {pre(app);if(app.running){button('Stop '+app.id,async()=>{await api('dev-stop',{id:app.id});await show('Runtime');});button('Preview '+app.id,async()=>{const url=previewUrl(app.port);const link=document.createElement('a');link.href=url;link.target='_blank';link.rel='noopener noreferrer';link.textContent='Open app preview: '+url;$('stPane').append(link);const frame=document.createElement('iframe');frame.src=url;frame.title='Managed dev app preview';frame.setAttribute('sandbox','allow-scripts allow-forms allow-same-origin');frame.style.width='100%';frame.style.height='450px';$('stPane').append(frame);});}}
    pre('Enforced mode: Docker --network=none, no host sockets/secrets, capabilities dropped, read-only container root, resource limits. Only registered detached checkout is mounted. HTTP egress is a separate exact-host HTTPS proxy with DNS pinning/private-address rejection and no redirect following. Requires an installed trusted image and running Docker; never falls back to host.');
    const checkout=input('Registered checkout ID');const cmd=input('Isolated checkout command',true);button('Execute in real container…',async()=>pre(await api('sandbox-run',{checkout_id:checkout.value,command:cmd.value,confirm:confirm('RUN IN ISOLATED CHECKOUT')})));
    const url=input('Permitted public HTTPS URL');button('Fetch via enforced domain proxy',async()=>pre(await api('domain-fetch',{url:url.value})));
    pre(await records('checkout'));
  }
  const panes = {Sessions:sessions, Review:review, Evidence:evidence, Policy:policy, Data:data, Workflows:workflow, Inbox:inbox, Runtime:runtime};
  async function show(name) { activePane = name; $('stPane').replaceChildren(); await panes[name](); }
  for (const name of Object.keys(panes)) button(name, () => show(name), $('stNav'));
  open.onclick = action(async () => { dialog.showModal(); notify = (await api('settings')).notifications; await show(activePane); }); $('stClose').onclick = () => dialog.close();
  window.addEventListener('nexus:event', event => { if (event.detail.type === 'approval_request' && dialog.open) dialog.close(); if (event.detail.type === 'done' && notify && 'Notification' in window && Notification.permission === 'granted') new Notification('Nexus run finished', {body:'Open dashboard to review status and verification evidence.'}); });
})();
