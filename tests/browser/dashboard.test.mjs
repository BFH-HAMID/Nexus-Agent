/** Real headless browser, scripted test-only APIs; never generates code or calls a model. */
import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import bundledChromium from '@sparticuz/chromium';

let browser;
const base = process.env.NEXUS_TEST_URL || 'http://127.0.0.1:8787';
before(async () => {
  browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundledChromium.executablePath(), args: bundledChromium.args.filter(a => a !== "--single-process"), headless: true});
});
after(async () => { await browser?.close(); });

async function setup({demo = false, restore = false, width = 1568, height = 770} = {}) {
  const context = await browser.newContext({viewport: {width, height}});
  const page = await context.newPage();
  const errors = [];
  const requests = [];
  const stats = {demo, workspace: 'C:\\Nexus', roles: 1, version: 'test', agents: {
    local: {model: 'test-model:8b', provider: 'openai-compatible', tool_mode: 'text'}
  }, keys: {}, pipelines: {check: {steps: ['general']}, build: {steps: ['general']}}, settings: {max_turns: 24}};
  let failApproval = false, failCancel = false, failRecall = false;
  let preferences = {local_only:false,default_local_model:'',role_models:{},write_paths:[],language:'en',layout:{},pricing:{}};
  const envelope = data => ({data,source:'test-only fixture',checked_at:Date.now()/1000});
  page.on('pageerror', e => errors.push(e.message));
  await page.addInitScript(({restore}) => {
    window.matchMedia = () => ({matches: true});
    window.CSS.escape ||= v => v;
    window._streams = [];
    window._intervals = [];
    window.setInterval = (fn, ms) => { window._intervals.push({fn, ms}); return window._intervals.length; };
    window.EventSource = class {
      static CLOSED = 2;
      constructor(url) { this.url = url; window._streams.push(this); }
      close() { this.readyState = 2; }
    };
    window.sendEvent = event => window._streams.at(-1).onmessage({data: JSON.stringify(event)});
    if (restore) sessionStorage.setItem('nexus.activeTask', 'test-task');
  }, {restore});
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const payload = route.request().postDataJSON();
    requests.push({path, payload});
    let status = 200, body = {};
    if (path === '/api/stats') body = stats;
    else if (path === '/api/agents') body = stats.agents;
    else if (path === '/api/workbench/repository/list') body=envelope([{id:'repo-fixture',name:'Nexus fixture',path:'/test-only/repo',branch:'feature-test',agent_workspace:true}]);
    else if (path === '/api/workbench/repository/status') body=envelope({id:'repo-fixture',name:'Nexus fixture',path:'/test-only/repo',branch:'feature-test',head:'test-only-head',files:['hello.txt'],additions:1,deletions:0,ahead:null,behind:null});
    else if (path === '/api/workbench/settings') {if(payload)preferences={...preferences,...payload};body=envelope(preferences);}
    else if (path === '/api/workbench/metrics') body=envelope({cpu_percent:12,ram:{total:1000,used:200},gpu:null});
    else if (path === '/api/workbench/health') body=envelope({reachable:true,scope:'model-list test fixture'});
    else if (path === '/api/workbench/queue') body=envelope({paused:payload?.action==='pause',active:null,waiting:[]});
    else if (path === '/api/workbench/history') body=envelope([{task_id:'test-task',task:'Inspect hello.txt',status:'done',result:{text:'Create hello.txt'},options:{mode:'plan'}}]);
    else if (path === '/api/workbench/files') body=envelope(['hello.txt']);
    else if (path === '/api/workbench/file') body=envelope({path:'hello.txt',content:'Hello Nexus',diff:'+Hello Nexus'});
    else if (path === '/api/workbench/errors') body=envelope([{message:'quota 429',hint:'Choose a local model'}]);
    else if (path === '/api/workbench/jobs') body=envelope([]);
    else if (path === '/api/workbench/capability') body=envelope({capabilities:['tools'],native_tools:true});
    else if (path === '/api/workbench/ollama') body=envelope({installed:[{name:'test-model:8b'}],loaded:[]});
    else if (path === '/api/workbench/git') body=envelope({branch:'feature-test',status:' M hello.txt'});
    else if (path === '/api/workbench/github') body=envelope([{number:1,title:'untrusted issue'}]);
    else if (path === '/api/roles') body = [{name: 'general', category: 'general', agent: 'local'}];
    else if (path === '/api/ollama') body = {connected: true, models: ['test-model:8b'], url: 'http://localhost:11434'};
    else if (path === '/api/github') body = {connected: false, message: 'Test fixture: not connected'};
    else if (path === '/api/task') { status = 202; body = {task_id: 'test-task'}; }
    else if (path === '/api/task/test-task') body = {task_id: 'test-task', task: 'Create hello.txt', created: Date.now()/1000, status: 'awaiting_approval'};
    else if (path.startsWith('/api/approve/')) { status = failApproval ? 503 : 200; body = failApproval ? {error: 'Approval not received'} : {ok: true}; }
    else if (path.startsWith('/api/cancel/')) { status = failCancel ? 503 : 200; body = failCancel ? {error: 'Stop failed'} : {ok: true}; }
    else if (path === '/api/recall') { status = failRecall ? 503 : 200; body = failRecall ? {error: 'Recall unavailable'} : []; }
    else if (path.startsWith('/api/memory/')) body = {text: '', enabled: true};
    else if (path === '/api/brain') body = {skills: {}, sessions: {}, memory: {}, learning: {enabled: false}};
    await route.fulfill({status, json: body});
  });
  await page.goto(base + '/dashboard');
  await page.waitForFunction(() => document.querySelector('#liveNotice').textContent !== 'Connecting to live server…');
  return {page, context, errors, requests, setFailure: (which, value) => {if (which === 'approval') failApproval = value; if (which === 'cancel') failCancel = value; if (which === 'recall') failRecall = value;}};
}

async function start(page) {
  await page.fill('#taskInput', 'Create hello.txt containing Hello Nexus');
  await page.click('#runBtn');
  await page.waitForFunction(() => window._streams.length > 0);
}
async function approval(page, event_id = 1) {
  await page.evaluate(event_id => window.sendEvent({type: 'approval_request', event_id, approval_id: 'approval-1', tool: 'write_file', risk: 'write', preview: 'hello.txt (11 characters)'}), event_id);
}

test('approval controls stay visible and click failures keep the request retryable', async () => {
  const {page, context, errors, requests, setFailure} = await setup();
  try {
    await start(page);
    await approval(page);
    for (const id of ['apYes', 'apAlways', 'apNo']) {
      const box = await page.locator('#' + id).boundingBox();
      assert.ok(box && box.width > 50 && box.height >= 30 && box.y >= 0 && box.y + box.height <= 770, id);
    }
    setFailure('approval', true);
    await page.click('#apYes');
    await page.waitForFunction(() => document.querySelector('#actionError').textContent.includes('not received'));
    assert.equal(await page.locator('#approvalCard').isVisible(), true);
    assert.equal(await page.locator('#apYes').isEnabled(), true);
    setFailure('approval', false);
    await page.click('#apAlways');
    await page.waitForFunction(() => document.querySelector('#approvalCard').classList.contains('hidden'));
    assert.deepEqual(requests.filter(r => r.path.startsWith('/api/approve/')).at(-1).payload,
      {approved: true, scope: 'session', approval_id: 'approval-1'});
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('side cards do not collapse, pipeline survives stats refresh, recall errors are shown', async () => {
  const {page, context, errors, setFailure} = await setup();
  try {
    for (const id of ['telTools', 'brainQuery', 'refreshLocal', 'refreshGithub']) {
      const box = await page.locator('#' + id).boundingBox();
      assert.ok(box?.height > 0, id);
    }
    await page.selectOption('#pipelineSelect', 'check');
    await page.evaluate(() => window._intervals.find(t => t.ms === 15000).fn());
    assert.equal(await page.inputValue('#pipelineSelect'), 'check');
    await page.locator('[data-pipe="build"]').click();
    assert.equal(await page.inputValue('#pipelineSelect'), 'build');
    setFailure('recall', true);
    await page.locator('#brainGo').click();
    await page.waitForFunction(() => document.querySelector('#brainList').textContent.includes('Recall unavailable'));
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('clear cannot erase an active run; stop errors retry; duplicate events ignored', async () => {
  const {page, context, setFailure, errors} = await setup();
  try {
    await start(page);
    assert.equal(await page.locator('#clearBtn').isDisabled(), true);
    await approval(page);
    await approval(page);
    assert.equal(await page.locator('#stream .step').count(), 2); // task + one approval
    setFailure('cancel', true);
    await page.click('#cancelBtn');
    await page.waitForFunction(() => document.querySelector('#actionError').textContent.includes('Stop failed'));
    assert.equal(await page.locator('#cancelBtn').isEnabled(), true);
    setFailure('cancel', false);
    await page.click('#cancelBtn');
    await page.evaluate(() => window.sendEvent({type: 'done', event_id: 2, status: 'cancelled', result: {ok: false}}));
    assert.equal(await page.locator('#clearBtn').isEnabled(), true);
    assert.equal(await page.locator('#phaseText').textContent().then(s => s.includes('cancelled')), true);
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('live dashboard refuses demo mode; no fabricated globe data', async () => {
  const {page, context, errors} = await setup({demo: true});
  try {
    assert.equal(await page.locator('#runBtn').isDisabled(), true);
    assert.equal(await page.locator('#globe').count(), 0);
    assert.equal(await page.locator('#statRegions').textContent(), 'Unavailable');
    assert.match(await page.locator('#liveNotice').textContent(), /disabled/);
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('refresh restores active task and approval; narrow screen actions remain accessible', async () => {
  const {page, context, errors} = await setup({restore: true, width: 390, height: 844});
  try {
    await page.waitForFunction(() => window._streams.length > 0);
    await approval(page);
    await page.locator('#apNo').scrollIntoViewIfNeeded();
    await page.click('#apNo');
    await page.waitForFunction(() => document.querySelector('#approvalCard').classList.contains('hidden'));
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('late approval HTTP acknowledgement cannot hide the next pending request', async () => {
  const {page, context, errors} = await setup();
  try {
    await start(page);
    await approval(page);
    await page.route('**/api/approve/**', async route => {
      await page.evaluate(() => {
        window.sendEvent({type: 'approval_response', event_id: 2, approved: true});
        window.sendEvent({type: 'approval_request', event_id: 3, approval_id: 'approval-2', tool: 'write_file', preview: 'second.txt'});
      });
      await route.fulfill({json: {ok: true}});
    });
    await page.click('#apYes');
    await page.waitForFunction(() => document.querySelector('#apPreview').textContent.startsWith('second.txt'));
    assert.equal(await page.locator('#approvalCard').isVisible(), true);
    assert.equal(await page.locator('#apYes').isEnabled(), true);
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('model picker, refresh, filters, map selection and reset are wired to real actions', async () => {
  const {page, context, errors, requests} = await setup();
  try {
    await page.selectOption('#localModel', 'test-model:8b');
    assert.equal(await page.locator('#metaRouting').textContent(), 'selected local');
    await page.locator('#refreshLocal').click();
    assert.equal(await page.inputValue('#localModel'), 'test-model:8b');
    const githubChecks = requests.filter(r => r.path === '/api/github').length;
    await page.locator('#refreshGithub').click();
    assert.ok(requests.filter(r => r.path === '/api/github').length > githubChecks);
    await page.locator('#map .node').first().focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#ndName').textContent(), 'local');
    await page.locator('#resetMap').click();
    await page.locator('label.toggle').filter({has: page.locator('#readOnly')}).click();
    await page.locator('label.toggle').filter({has: page.locator('#autoYes')}).click();
    await start(page);
    const payload = requests.find(r => r.path === '/api/task').payload;
    assert.equal(payload.local_model, 'test-model:8b');
    assert.equal(payload.read_only, true);
    assert.equal(payload.auto_yes, true);
    await page.evaluate(() => window.sendEvent({type: 'think', event_id: 1, agent: 'local', model: 'test-model:8b', turn: 1}));
    await page.locator('[data-f="tools"]').click();
    assert.ok(await page.locator('#stream .step.hidden-filter').count() > 0);
    await page.locator('[data-f="all"]').click();
    assert.equal(await page.locator('#stream .step.hidden-filter').count(), 0);
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('large code previews and short windows keep approval and stop reachable', async () => {
  const {page, context, errors} = await setup({width: 1366, height: 640});
  try {
    await start(page);
    await page.evaluate(() => window.sendEvent({type: 'approval_request', event_id: 1, approval_id: 'large-preview', tool: 'write_file', preview: 'hello.txt\n' + 'Hello Nexus\n'.repeat(100)}));
    await page.locator('#apNo').scrollIntoViewIfNeeded();
    await page.click('#apNo');
    await page.waitForFunction(() => document.querySelector('#approvalCard').classList.contains('hidden'));
    await page.locator('#cancelBtn').click();
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('workbench health, metrics, policy, history and files are functional', async () => {
  const {page,context,requests,errors}=await setup();
  try {
    await page.click('#wbOpen');
    await page.waitForFunction(()=>document.querySelector('#wbAgent').options.length>0);
    await page.click('#wbProbe');
    await page.waitForFunction(()=>document.querySelector('#wbHealth').textContent.includes('reachable'));
    await page.click('#wbMetrics');
    await page.waitForFunction(()=>document.querySelector('#wbMetricData').textContent.includes('cpu_percent'));
    await page.click('[data-pane="policy"]');
    await page.check('#wbLocalOnly');
    await page.selectOption('#wbDefaultModel','test-model:8b');
    await page.fill('#wbPaths','src\ntests');
    await page.selectOption('#wbRole','general');
    await page.selectOption('#wbRoleTarget','ollama:test-model:8b');
    await page.click('#wbMapRole');
    await page.click('#wbSavePolicy');
    await page.waitForFunction(()=>document.querySelector('#wbPolicyResult').textContent.includes('saved')||document.querySelector('#wbPolicyResult').textContent.includes('local_only'));
    const policy=requests.filter(r=>r.path==='/api/workbench/settings'&&r.payload?.write_paths).at(-1).payload;
    assert.equal(policy.local_only,true);assert.deepEqual(policy.write_paths,['src','tests']);
    assert.equal(policy.role_models.general,'ollama:test-model:8b');
    await page.click('[data-pane="runs"]');await page.click('#wbHistory');
    await page.locator('#wbRunList button').first().click();
    await page.waitForFunction(()=>document.querySelector('#wbReviewedPlan').value==='Create hello.txt');
    assert.equal(await page.inputValue('#wbReviewedPlan'),'Create hello.txt');
    await page.click('[data-pane="files"]');await page.click('#wbFiles');
    await page.locator('#wbFileList button').first().click();
    await page.waitForFunction(()=>document.querySelector('#wbFileData').textContent.includes('+Hello Nexus'));
    await page.click('[data-pane="queue"]');await page.click('#wbQueuePause');
    await page.waitForFunction(()=>document.querySelector('#wbQueueState').textContent.includes('paused'));
    await page.click('[data-pane="errors"]');await page.click('#wbErrors');
    await page.waitForFunction(()=>document.querySelector('#wbErrorData').textContent.includes('429'));
    assert.deepEqual(errors,[]);
  } finally {await context.close();}
});

test('workbench model controls, Bengali layout and keyboard palette work on mobile',async()=>{
  const {page,context,requests,errors}=await setup({width:390,height:844});
  try {
    await page.click('#wbOpen');await page.waitForFunction(()=>document.querySelector('#wbModel').options.length>0);
    await page.click('[data-pane="models"]');await page.click('#wbCapabilities');
    await page.waitForFunction(()=>document.querySelector('#wbModelData').textContent.includes('native_tools'));
    page.on('dialog', d=>d.dismiss());
    await page.click('#wbDelete');
    assert.equal(requests.filter(r=>r.path==='/api/workbench/delete-model').length,0);
    await page.click('[data-pane="layout"]');await page.selectOption('#wbLanguage','bn');
    await page.check('#wbFocus');await page.click('#wbSaveLayout');
    await page.waitForFunction(()=>document.querySelector('#wbClose').textContent==='বন্ধ করুন');
    assert.equal(await page.locator('body').evaluate(el=>el.classList.contains('wb-focus')),true);
    await page.click('#wbClose');await page.keyboard.press('Control+k');
    assert.equal(await page.locator('#wbPalette').isVisible(),true);
    await page.fill('#wbSearch','ত্রুটি');await page.keyboard.press('Enter');
    await page.waitForFunction(()=>!document.querySelector('#wbPane-errors').hidden&&document.querySelector('#workbench').open);
    assert.equal(await page.locator('#wbPane-errors').isVisible(),true);
    assert.deepEqual(errors,[]);
  } finally {await context.close();}
});

test('Studio sessions, immutable branching and review pages render untrusted text safely', async () => {
  const {page,context,errors} = await setup();
  const conversations = [{id:'c1',title:'Session one',messages:[{role:'user',text:'<img src=x onerror="window.studioInjected=true">'}]}];
  await page.route('**/api/workbench/studio/**', async route => {
    const url = new URL(route.request().url()); const path = url.pathname.split('/studio/')[1];
    const payload = route.request().method() === 'POST' ? route.request().postDataJSON() : {};
    let data = [];
    if (path === 'items') data = url.searchParams.get('kind') === 'conversation' ? conversations : [];
    else if (path === 'conversation') { const c = {id:'branched',title:'Session one / branch',messages:[{role:'user',text:payload.text}]}; conversations.push(c); data = c; }
    else if (path === 'settings') data = {notifications:false,max_tools:100};
    else if (path === 'availability') data = {encrypted_live_state:'Not implemented',network_domain_enforcement:'Unavailable'};
    await route.fulfill({json:{data,source:'test fixture only'}});
  });
  try {
    await page.click('#studioOpen');
    await page.getByRole('button',{name:'Session one',exact:true}).click();
    await page.getByLabel('Task',{exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>window.studioInjected),undefined);
    page.once('dialog',d=>d.accept('edited user task'));
    await page.getByRole('button',{name:'Edit / branch message',exact:true}).click();
    await page.getByRole('button',{name:'Session one / branch',exact:true}).waitFor();
    assert.equal(conversations[0].messages[0].text.startsWith('<img'),true);
    assert.equal(conversations[1].messages[0].text,'edited user task');
    await page.locator('#stNav').getByRole('button',{name:'Policy',exact:true}).click();
    await page.getByText(/Not implemented/).waitFor();
    await page.locator('#stNav').getByRole('button',{name:'Data',exact:true}).click();
    await page.getByRole('button',{name:'Download portable backup',exact:true}).waitFor();
    assert.deepEqual(errors,[]);
  } finally { await context.close(); }
});

test('Studio visual workflow builder validates, versions and exposes live approvals on mobile', async () => {
  const {page,context,errors} = await setup({width:430,height:780});
  const versions=[]; const calls=[];
  await page.route('**/api/workbench/studio/**',async route=>{
    const u=new URL(route.request().url());const path=u.pathname.split('/studio/')[1];const p=route.request().method()==='POST'?route.request().postDataJSON():{};calls.push({path,p});let data=[];
    if(path==='settings') data={notifications:false};
    if(path==='workflow-versions') data=versions;
    if(path==='workflow-dry-run') data={valid:true,order:p.definition.nodes.map(n=>n.id)};
    if(path==='workflow-save'){data={id:'version-1',family:'family-1',version:1,definition:p.definition};versions.push(data);}
    if(path==='items'&&u.searchParams.get('kind')==='workflow-live') data=[{id:'live-fixture',run_id:'wf',node_id:'inspect',task_id:'test-task',status:'running'}];
    await route.fulfill({json:{data,source:'browser test-only fixture'}});
  });
  try{
    await page.click('#studioOpen');await page.locator('#stNav').getByRole('button',{name:'Workflows',exact:true}).click();
    await page.getByLabel('Workflow name',{exact:true}).fill('Reviewed build');
    await page.getByLabel('Node ID',{exact:true}).fill('inspect');
    await page.getByLabel('Node task / approval question',{exact:true}).fill('Read the current code');
    await page.getByRole('button',{name:'Add visual node',exact:true}).click();
    await page.locator('svg[aria-label="Workflow graph"]').getByText('inspect · research',{exact:true}).waitFor();
    await page.getByRole('button',{name:'Dry-run validate (no model/commands)',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#stPane').textContent.includes('"valid": true'));
    await page.getByRole('button',{name:'Save new immutable version',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#stPane').textContent.includes('version-1'));
    assert.equal(calls.find(c=>c.path==='workflow-save').p.definition.nodes[0].kind,'research');
    await page.getByRole('button',{name:'Open live node test-task',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('#nexusStudio').open);
    assert.equal(await page.locator('#nexusStudio').evaluate(d=>d.open),false);
    assert.deepEqual(errors,[]);
  } finally {await context.close();}
});

test('Studio inbox, runtime errors and new approvals never bypass or obscure consent', async()=>{
  const {page,context,errors}=await setup();let answer=null;
  await page.route('**/api/workbench/studio/**',async route=>{
    const u=new URL(route.request().url());const path=u.pathname.split('/studio/')[1];const p=route.request().method()==='POST'?route.request().postDataJSON():{};let data=[];
    if(path==='settings')data={notifications:false};
    if(path==='items'&&u.searchParams.get('kind')==='clarification')data=[{id:'question1',question:'Which endpoint?',status:'pending',approval:false}];
    if(path==='clarification-answer'){answer=p.answer;data={status:'answered'};}
    if(path==='dev-start')return route.fulfill({status:400,json:{error:'Stop managed apps before enforced execution'}});
    await route.fulfill({json:{data,source:'browser test-only fixture'}});
  });
  try{
    await page.click('#studioOpen');await page.locator('#stNav').getByRole('button',{name:'Inbox',exact:true}).click();
    await page.getByLabel('Answer question1',{exact:true}).fill('Use the local endpoint');await page.getByRole('button',{name:'Send answer question1',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#stPane').textContent.includes('answered'));assert.equal(answer,'Use the local endpoint');
    await page.locator('#stNav').getByRole('button',{name:'Runtime',exact:true}).click();
    await page.getByLabel('Dev app argv JSON (not a shell string)',{exact:true}).fill('["example-command"]');
    page.once('dialog',d=>d.accept('START TRUSTED DEV APP'));await page.getByRole('button',{name:'Start trusted app…',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#stError').textContent.includes('Stop managed apps'));
    await page.click('#stClose');await start(page);await page.click('#studioOpen');await approval(page);
    await page.waitForFunction(()=>!document.querySelector('#nexusStudio').open);
    assert.equal(await page.locator('#apYes').isVisible(),true);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('Repository toolbar requires reviewed files and typed commit confirmation',async()=>{
  const {page,context,errors}=await setup();const calls=[];
  await page.route('**/api/workbench/repository/preview',async route=>{const p=route.request().postDataJSON();calls.push({action:'preview',p});await route.fulfill({json:{data:{id:'commit-preview',action:p.action,repo:p.repo,confirmation:'COMMIT feature-test',changes:[{path:'hello.txt',diff:'+Hello Nexus'}]}}});});
  await page.route('**/api/workbench/repository/execute',async route=>{calls.push({action:'execute',p:route.request().postDataJSON()});await route.fulfill({json:{data:{exit_code:0,action:'commit',output:'actual test-fixture commit result'}}});});
  try{
    await page.click('#repoMore');await page.locator('#repoActions [data-repo-action="commit"]').click();
    await page.locator('#repoForm input[name="repoPath"]').check();await page.fill('#repoMessage','feat: reviewed file');await page.click('#repoPreview');
    await page.waitForFunction(()=>document.querySelector('#repoOutput').textContent.includes('COMMIT feature-test'));
    assert.equal(calls[0].p.paths[0],'hello.txt');assert.equal(calls.filter(c=>c.action==='execute').length,0);
    assert.equal(await page.locator('#repoExecute').isEnabled(),false);
    await page.fill('#repoConfirmation','COMMIT wrong');assert.equal(await page.locator('#repoExecute').isEnabled(),false);
    await page.fill('#repoConfirmation','COMMIT feature-test');await page.click('#repoExecute');
    await page.waitForFunction(()=>document.querySelector('#repoOutput').textContent.includes('commit result'));
    assert.equal(calls.at(-1).p.preview_id,'commit-preview');assert.equal(await page.locator('#repoExecute').isEnabled(),false);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('Repository selection invalidates late previews; blocked merges remain disabled',async()=>{
  const {page,context,errors}=await setup({width:430,height:780});let release=null;
  await page.route('**/api/workbench/repository/status?*',async route=>{const id=new URL(route.request().url()).searchParams.get('repo');await route.fulfill({json:{data:{id,name:id,path:'/test/'+id,branch:'feature-test',head:'fixture',files:['hello.txt'],additions:1,deletions:0,ahead:null,behind:null}}});});
  await page.route('**/api/workbench/repository/preview',async route=>{
    const p=route.request().postDataJSON();if(p.action==='merge')return route.fulfill({status:400,json:{error:'PR has pending/failed checks; no admin bypass is offered'}});
    await new Promise(resolve=>{release=resolve;});await route.fulfill({json:{data:{id:'late',confirmation:'PUSH feature-test',repo:p.repo}}});
  });
  try{
    await page.click('#repoMore');await page.locator('#repoActions [data-repo-action="push"]').click();await page.click('#repoPreview');
    await page.waitForTimeout(100);assert.ok(release);await page.click('#repoClose');
    await page.locator('#repoSelect').evaluate(node=>{const o=document.createElement('option');o.value='repo-two';o.textContent='Repo two · Git only';node.append(o);});
    await page.selectOption('#repoSelect','repo-two');release();await page.waitForTimeout(100);
    await page.click('#repoMore');await page.locator('#repoActions [data-repo-action="merge"]').click();await page.fill('#repoNumber','42');await page.click('#repoPreview');
    await page.waitForFunction(()=>document.querySelector('#repoError').textContent.includes('pending/failed'));
    assert.equal(await page.locator('#repoExecute').isEnabled(),false);assert.deepEqual(errors,[]);
  }finally{if(release)release();await context.close();}
});

for(const viewport of [{width:1568,height:770},{width:430,height:780}]){
  test(`Composer repository strip placement and PR review flow (${viewport.width}px)`,async()=>{
    const {page,context,errors}=await setup(viewport);const calls=[];
    await page.route('**/api/workbench/repository/preview',async route=>{
      const p=route.request().postDataJSON();calls.push(p);
      await route.fulfill({json:{data:{id:'pr-review',confirmation:'CREATE PR feature-test',repo:p.repo,action:p.action}}});
    });
    await page.route('**/api/workbench/repository/prs?*',async route=>route.fulfill({json:{data:[{number:42,title:'Reviewed fixture PR',url:'https://github.com/example/repo/pull/42'}]}}));
    try{
      await page.waitForFunction(()=>document.querySelector('#repoBranch').textContent.includes('feature-test'));
      await page.locator('#repoBar').scrollIntoViewIfNeeded();
      const placement=await page.evaluate(()=>{
        const bar=document.querySelector('#repoBar'), input=document.querySelector('#taskInput');
        const r=bar.getBoundingClientRect(),t=input.getBoundingClientRect();
        return {inside:bar.closest('.composer')!==null,next:bar.previousElementSibling?.classList.contains('main-row'),below:r.top>=t.bottom,left:r.left,right:r.right,width:innerWidth};
      });
      assert.equal(placement.inside,true);assert.equal(placement.next,true);assert.equal(placement.below,true);
      assert.ok(placement.left>=0&&placement.right<=placement.width);
      await page.click('#repoMore');assert.equal(await page.locator('#repoActions').isVisible(),true);
      await page.keyboard.press('Escape');await page.waitForFunction(()=>!document.querySelector('#repoActions').matches(':popover-open'));
      await page.click('#repoCounts');await page.locator('#repoMessage').waitFor({state:'visible'});
      await page.click('#repoClose');
      await page.locator('#repoBar [data-repo-action="create-pr"]').click();
      await page.fill('#repoBase','main');await page.fill('#repoTitle','Reviewed PR');await page.fill('#repoBody','Changes and test evidence');await page.click('#repoPreview');
      await page.waitForFunction(()=>document.querySelector('#repoOutput').textContent.includes('CREATE PR feature-test'));
      assert.equal(calls[0].action,'create-pr');assert.equal(calls[0].title,'Reviewed PR');
      assert.equal(await page.locator('#repoExecute').isEnabled(),false);await page.click('#repoClose');
      await page.click('#repoMore');await page.click('#repoPRs');
      await page.getByRole('button',{name:'Review merge #42',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('#repoNumber')?.value==='42');
      assert.equal(await page.locator('#repoExecute').isEnabled(),false);
      assert.equal(await page.locator('#repoMethod').inputValue(),'merge');
      assert.equal(calls.length,1);assert.deepEqual(errors,[]);
    }finally{await context.close();}
  });
}
