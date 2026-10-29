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
    else if (path === '/api/workbench/repository/branches') body=envelope(['main','feature-test']);
    else if (path === '/api/workbench/repository/status') body=envelope({local_available:true,can_push:true,default_branch:'main',id:'repo-fixture',name:'Nexus fixture',path:'/test-only/repo',branch:'feature-test',head:'test-only-head',files:['hello.txt'],additions:1,deletions:0,ahead:null,behind:null});
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
    for (const id of ['telTools', 'brainQuery', 'refreshLocal', 'refreshHardware']) {
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
    assert.equal(await page.locator('#statRegions').count(), 0);
    assert.equal(await page.locator('#statToolCalls').textContent(), '0');
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
    const measurements = requests.filter(r => r.path === '/api/workbench/metrics').length;
    await page.locator('#refreshHardware').click();
    assert.ok(requests.filter(r => r.path === '/api/workbench/metrics').length > measurements);
    assert.equal(await page.locator('#refreshGithub').count(), 0);
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
  await page.route('**/api/workbench/repository/status?*',async route=>{const id=new URL(route.request().url()).searchParams.get('repo');await route.fulfill({json:{data:{local_available:true,can_push:true,default_branch:'main',id,name:id,path:'/test/'+id,branch:'feature-test',head:'fixture',files:['hello.txt'],additions:1,deletions:0,ahead:null,behind:null}}});});
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
      await page.selectOption('#repoBase','main');await page.fill('#repoTitle','Reviewed PR');await page.fill('#repoBody','Changes and test evidence');await page.click('#repoPreview');
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

test('setup instructions are removed; server hardware uses real readings and clears stale failures',async()=>{
  const {page,context,errors,requests}=await setup();
  try{
    await page.waitForFunction(()=>document.querySelector('#hardwareCpu').textContent.includes('12.0%'));
    assert.equal(await page.locator('#integrations').getByText('GitHub account',{exact:true}).count(),0);
    assert.equal(await page.locator('#integrations').getByText('VS Code sidebar',{exact:true}).count(),0);
    assert.ok(!(await page.locator('#integrations').textContent()).includes('gh auth login'));
    assert.equal(requests.filter(r=>r.path==='/api/github').length,0);
    assert.ok((await page.locator('#hardwareGpu').textContent()).includes('unavailable'));
    await page.route('**/api/workbench/metrics',async route=>route.fulfill({json:{data:{cpu_percent:33.5,cpu_cores:4,ram:{total:8589934592,used:2147483648,percent:25},nexus_process_rss:1073741824,disk:{used:1073741824,total:4294967296},gpu_devices:[{name:'<img src=x onerror=alert(1)>',utilization_percent:70,memory_used_mib:2048,memory_total_mib:8192}]},source:'test-only hardware fixture',checked_at:1700000000}}));
    await page.click('#refreshHardware');
    await page.waitForFunction(()=>document.querySelector('#hardwareCpu').textContent.includes('33.5%'));
    assert.equal(await page.locator('#hardwareRam').textContent(),'2.00 GiB / 8.00 GiB');
    assert.equal(await page.locator('#hardwareProcess').textContent(),'1.00 GiB');
    assert.ok((await page.locator('#hardwareGpu').textContent()).includes('70%'));
    assert.equal(await page.locator('#hardwareGpu img').count(),0);
    await page.route('**/api/workbench/metrics',async route=>route.fulfill({status:503,json:{error:'Collector unavailable'}}));
    await page.click('#refreshHardware');
    await page.waitForFunction(()=>document.querySelector('#hardwareStatus').textContent.includes('Collector unavailable'));
    assert.equal(await page.locator('#hardwareCpu').textContent(),'Unavailable');
    assert.equal(await page.locator('#hardwareRam').textContent(),'Unavailable');
    assert.ok((await page.locator('#hardwareGpu').textContent()).includes('unavailable'));
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('command stdout/stderr stream live, stopping is acknowledged, and applied code diffs stay escaped',async()=>{
  const {page,context,errors,setFailure,requests}=await setup();
  try{
    await start(page);
    await page.evaluate(()=>{
      window.sendEvent({type:'think',event_id:1,agent:'local',model:'test-model',turn:1});
      window.sendEvent({type:'tool_call',event_id:2,tool:'run_command',call_id:'call-1',risk:'write',args:{command:'echo hello'},preview:'$ echo hello'});
      window.sendEvent({type:'process',event_id:3,call_id:'call-1',process_id:'proc-1',status:'running',command:'echo hello',cwd:'/test-only',pid:42});
      window.sendEvent({type:'tool_output',event_id:4,call_id:'call-1',process_id:'proc-1',stream:'stdout',text:'first output <img src=x onerror=alert(1)>\n'});
      window.sendEvent({type:'tool_output',event_id:4,call_id:'call-1',process_id:'proc-1',stream:'stdout',text:'DUPLICATE'});
      window.sendEvent({type:'tool_output',event_id:5,call_id:'call-1',process_id:'proc-1',stream:'stderr',text:'diagnostic\n'});
    });
    const card=page.locator('[data-process-id="proc-1"]');
    assert.ok((await page.locator('#stream .think').textContent()).includes('waiting for model response'));
    assert.ok((await card.locator('[data-stream="stdout"]').textContent()).includes('first output'));
    assert.ok(!(await card.textContent()).includes('DUPLICATE'));
    assert.equal(await card.locator('[data-stream="stderr"]').textContent(),'diagnostic\n');
    assert.equal(await card.locator('img').count(),0);
    assert.equal(await page.locator('#stream .tool_result').count(),0);
    await page.locator('[data-f="tools"]').click();assert.equal(await card.isVisible(),true);
    setFailure('cancel',true);await card.locator('.live-process-stop').click();
    await page.waitForFunction(()=>document.querySelector('#actionError').textContent.includes('Stop failed'));
    assert.equal(await card.locator('.live-process-stop').isEnabled(),true);
    setFailure('cancel',false);await card.locator('.live-process-stop').click();
    await page.waitForFunction(()=>document.querySelector('.live-process-stop').textContent==='Stop requested');
    assert.equal(await card.locator('.live-process-status').textContent(),'Running command');
    assert.ok(requests.some(r=>r.path==='/api/cancel/test-task'));
    await page.evaluate(()=>{
      window.sendEvent({type:'process',event_id:6,call_id:'call-1',process_id:'proc-1',status:'cancelled',exit_code:-15});
      window.sendEvent({type:'tool_result',event_id:7,call_id:'call-1',tool:'run_command',is_error:true,preview:'command cancelled',output:'line\n'.repeat(500)+'RETAINED TAIL',chars:2520});
      window.sendEvent({type:'file_change',event_id:8,path:'safe.txt',action:'updated',diff:'--- a/safe.txt\n+++ b/safe.txt\n-old\n+<img src=x onerror=alert(1)>',note:'Applied change'});
    });
    assert.equal(await card.locator('.live-process-status').textContent(),'cancelled · exit -15');
    assert.equal(await card.locator('.live-process-stop').isVisible(),false);
    const result=page.locator('#stream .tool_result');assert.equal(await result.isVisible(),true);
    await result.locator('summary').click();assert.ok((await result.locator('pre').textContent()).endsWith('RETAINED TAIL'));
    assert.ok((await page.locator('#stream .file_change').textContent()).includes('safe.txt'));
    assert.equal(await page.locator('#stream img').count(),0);
    await page.evaluate(()=>{
      window.sendEvent({type:'done',event_id:9,status:'cancelled',result:{ok:false}});
      window.sendEvent({type:'tool_output',event_id:10,process_id:'proc-1',stream:'stdout',text:'STALE POST-COMPLETION OUTPUT'});
    });
    assert.ok((await page.locator('#stream .done').textContent()).includes('run cancelled'));
    assert.ok(!(await card.textContent()).includes('STALE POST-COMPLETION OUTPUT'));
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('hardware cards fit desktop/mobile, animate only measurements, and respect reduced motion',async()=>{
  for(const width of [1568,430]){
    const {page,context,errors}=await setup({width,height:900});let release;
    try{
      await page.waitForFunction(()=>document.querySelector('#hardwareState').textContent==='SAMPLED');
      assert.equal(await page.locator('#hardwareCpuDial').evaluate(el=>el.style.getPropertyValue('--usage')),'12%');
      assert.equal(await page.locator('#hardwareRamFill').evaluate(el=>el.style.getPropertyValue('--usage')),'20%');
      assert.equal(await page.locator('#hardwareGpuState').textContent(),'UNAVAILABLE');
      assert.equal(await page.locator('.hardware-details').getAttribute('open'),null);
      await page.locator('#serverMonitor').scrollIntoViewIfNeeded();
      for(const card of await page.locator('#serverMonitor .hardware-card').all()){
        const box=await card.boundingBox();assert.ok(box.width>80&&box.x>=0&&box.x+box.width<=width);
      }
      await page.emulateMedia({reducedMotion:'reduce'});
      await page.route('**/api/workbench/metrics',async route=>{
        await new Promise(resolve=>{release=resolve;});
        await route.fulfill({status:503,json:{error:'Test-only collector failure'}});
      });
      await page.click('#refreshHardware');
      await page.waitForFunction(()=>document.querySelector('#serverMonitor').getAttribute('aria-busy')==='true');
      assert.equal(await page.locator('#serverMonitor').getAttribute('data-state'),'loading');
      assert.equal(await page.locator('#refreshHardware span').evaluate(el=>getComputedStyle(el).animationName),'none');
      release();release=null;
      await page.waitForFunction(()=>document.querySelector('#hardwareState').textContent==='OFFLINE');
      assert.equal(await page.locator('#hardwareCpuDial').evaluate(el=>el.style.getPropertyValue('--usage')),'0%');
      assert.equal(await page.locator('#hardwareCpu').textContent(),'Unavailable');
      assert.deepEqual(errors,[]);
    }finally{if(release)release();await context.close();}
  }
});

test('live-run metrics explain idle state and update immediately from observed events',async()=>{
  const {page,context,errors}=await setup();
  try{
    assert.ok((await page.locator('#liveRunNote').textContent()).includes('No Nexus task selected'));
    assert.equal(await page.locator('#statLatency').textContent(),'No results yet');
    assert.equal(await page.locator('#statToolCalls').textContent(),'0');
    assert.equal(await page.locator('#statRegions').count(),0);
    await start(page);
    await page.evaluate(()=>{
      window.sendEvent({type:'think',event_id:1,agent:'local',model:'test-model',turn:1});
      window.sendEvent({type:'tool_call',event_id:2,tool:'read_file',risk:'read',preview:'hello.txt'});
    });
    assert.equal(await page.locator('#statCalls').textContent(),'1');
    assert.equal(await page.locator('#statToolCalls').textContent(),'1');
    assert.equal(await page.locator('#statLatency').textContent(),'No results yet');
    assert.ok((await page.locator('#liveRunNote').textContent()).includes('Task test-task'));
    await page.evaluate(()=>{
      window.sendEvent({type:'tool_result',event_id:3,tool:'read_file',duration_ms:80,preview:'hello'});
      window.sendEvent({type:'tool_result',event_id:4,tool:'read_file',duration_ms:120,preview:'hello'});
    });
    assert.equal(await page.locator('#statLatency').textContent(),'100ms');
    await page.evaluate(()=>window.sendEvent({type:'done',event_id:5,status:'done',result:{ok:true}}));
    assert.ok((await page.locator('#liveRunNote').textContent()).includes('retained run events'));
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('GitHub search retains selection; remote private repo PR uses its default base and explicit head',async()=>{
  const {page,context,errors}=await setup({width:430,height:780});const previews=[];
  await page.route('**/api/workbench/repository/list*',route=>route.fulfill({json:{data:[
    {id:'repo-fixture',name:'example/local',agent_workspace:true},
    {id:'team/secret',name:'team/secret',private:true,agent_workspace:false},
    {id:'team/public',name:'team/public',private:false,agent_workspace:false}
  ]}}));
  await page.route('**/api/workbench/repository/status?*',route=>{
    const id=new URL(route.request().url()).searchParams.get('repo');
    return route.fulfill({json:{data:{id,name:id,local_available:false,can_push:true,branch:'trunk',default_branch:'trunk',path:null,head:null,files:[],additions:null,deletions:null}}});
  });
  await page.route('**/api/workbench/repository/branches?*',route=>route.fulfill({json:{data:['trunk','feature/test']}}));
  await page.route('**/api/workbench/repository/preview',route=>{
    const payload=route.request().postDataJSON();previews.push(payload);
    return route.fulfill({json:{data:{id:'remote-review',confirmation:'CREATE PR team/secret feature/test',diff:'+real test-only review'}}});
  });
  try{
    await page.reload();await page.waitForFunction(()=>document.querySelector('#repoSelect').options.length===4);
    await page.fill('#repoSearch','secret');
    assert.equal(await page.locator('#repoSelect').inputValue(),'repo-fixture');
    assert.equal(await page.locator('#repoSelect option[value="team/public"]').count(),0);
    assert.ok((await page.locator('#repoSelect option[value="team/secret"]').textContent()).includes('private'));
    await page.selectOption('#repoSelect','team/secret');
    await page.waitForFunction(()=>document.querySelector('#repoScope').textContent.includes('new GitHub branch after permission')&&!document.querySelector('#repoScope').hidden);
    assert.equal(await page.locator('#repoCounts').isEnabled(),false);
    assert.ok((await page.locator('#repoAdded').textContent()).includes('unavailable'));
    assert.equal(await page.locator('#repoActions [data-repo-action="commit"]').isEnabled(),false);
    assert.equal(await page.locator('#repoActions [data-repo-action="push"]').isEnabled(),false);
    await page.locator('#repoBar [data-repo-action="create-pr"]').click();
    await page.locator('#repoHead').waitFor();
    assert.equal(await page.locator('#repoBase').inputValue(),'trunk');
    await page.selectOption('#repoHead','feature/test');await page.fill('#repoTitle','Remote review');await page.fill('#repoBody','Test evidence');await page.click('#repoPreview');
    await page.waitForFunction(()=>document.querySelector('#repoOutput').textContent.includes('remote-review'));
    assert.deepEqual(previews,[{repo:'team/secret',action:'create-pr',base:'trunk',head:'feature/test',title:'Remote review',body:'Test evidence'}]);
    assert.equal(await page.locator('#repoExecute').isEnabled(),false);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('GitHub inventory authentication errors never fall back to local projects',async()=>{
  const {page,context,errors}=await setup();
  await page.route('**/api/workbench/repository/list*',route=>route.fulfill({status:400,json:{error:'GitHub connection needs attention on the Nexus server'}}));
  try{
    await page.reload();await page.waitForFunction(()=>document.querySelector('#repoBarError').textContent.includes('GitHub connection needs attention'));
    assert.equal(await page.locator('#repoSelect').inputValue(),'');
    assert.equal(await page.locator('#repoBar [data-repo-action="create-pr"]').isEnabled(),false);
    assert.equal(await page.locator('#repoActions [data-repo-action="push"]').isEnabled(),false);
    assert.equal(await page.locator('#repoPRs').isEnabled(),false);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

for(const viewport of [{width:1568,height:770},{width:430,height:780}]){
  test(`Compact task entry preserves more processing space (${viewport.width}px)`,async()=>{
    const {page,context,errors}=await setup(viewport);
    try{
      await page.waitForFunction(()=>document.querySelector('#repoBranch').textContent.includes('feature-test'));
      const sizes=await page.evaluate(()=>{
        const rect=selector=>document.querySelector(selector).getBoundingClientRect();
        const input=rect('#taskInput'),actions=rect('.composer-actions'),stream=rect('#stream'),process=rect('.process-card'),map=rect('.map-card');
        return {input:input.height,actions:actions.height,stream:stream.height,process:process.height,map:map.height,inputRight:input.right,actionsRight:actions.right,width:innerWidth};
      });
      assert.ok(sizes.input<=56,JSON.stringify(sizes));
      assert.ok(sizes.actions<70,JSON.stringify(sizes));
      assert.ok(sizes.stream>=150,JSON.stringify(sizes));
      assert.ok(sizes.inputRight<=sizes.width&&sizes.actionsRight<=sizes.width,JSON.stringify(sizes));
      if(viewport.width>1180)assert.ok(sizes.process>sizes.map,JSON.stringify(sizes));
      await page.fill('#taskInput','A longer task description\n'.repeat(15));
      assert.equal(await page.locator('#runBtn').isVisible(),true);
      assert.equal(await page.locator('#clearBtn').isVisible(),true);
      assert.deepEqual(errors,[]);
    }finally{await context.close();}
  });
}

test('Task workspace is explicit; prose-only coding completion is shown as blocked',async()=>{
  const {page,context,errors}=await setup();
  try{
    assert.ok((await page.locator('#taskWorkspace').textContent()).includes('Task target: GitHub'));
    assert.ok((await page.locator('#taskWorkspace').textContent()).includes('no clone/local tests'));
    assert.ok((await page.locator('#taskWorkspace').getAttribute('title')).includes('GitHub'));
    await start(page);
    await page.evaluate(()=>window.sendEvent({type:'done',event_id:1,status:'error',result:{
      ok:false,tool_calls:0,changed_files:[],text:'I created the files and pushed',
      stopped_reason:'blocked: no successful workspace tool execution; task not completed',
      execution:{required:true,state:'blocked',successful_workspace_tools:0}
    }}));
    const completion=await page.locator('#stream .done').textContent();
    assert.ok(completion.includes('run blocked'));
    assert.ok(completion.includes('no successful workspace tool execution'));
    assert.ok(!completion.includes('run complete'));
    assert.ok(!completion.includes('I created the files and pushed'));
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('Observed tool activity does not label task acceptance verified',async()=>{
  const {page,context,errors}=await setup();
  try{
    await start(page);
    await page.evaluate(()=>window.sendEvent({type:'done',event_id:1,status:'done',result:{
      ok:true,tool_calls:2,changed_files:['hello.txt'],summary:'Edited hello.txt; tests not run',
      execution:{required:true,state:'unverified',successful_workspace_tools:1}
    }}));
    const completion=await page.locator('#stream .done').textContent();
    assert.ok(completion.includes('acceptance unverified'));
    assert.ok(!completion.includes('run complete'));
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('Streaming assistant output is finalized once instead of duplicated',async()=>{
  const {page,context,errors}=await setup();
  try{
    await start(page);
    await page.evaluate(()=>{
      window.sendEvent({type:'text_delta',event_id:1,text:'Inspecting the workspace',role:'general'});
      window.sendEvent({type:'text',event_id:2,text:'Inspecting the workspace safely',role:'general',streamed:true});
    });
    assert.equal(await page.locator('#stream .step.text').count(),1);
    assert.ok((await page.locator('#stream .step.text').textContent()).includes('Inspecting the workspace safely'));
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('Selected repo prompt, task branch, PR and merge need only approval, including after reload',async()=>{
  const {page,context,errors}=await setup();const calls=[];
  const task={task_id:'abcdef123456',repository:'example/project',branch:'nexus/task-abcdef123456',base:'main',head:'reviewed-sha',created:true,files:['README.md']};
  await page.route('**/api/workbench/repository/list*',route=>route.fulfill({json:{data:[{id:'example/project',name:'example/project',agent_workspace:false}]}}));
  await page.route('**/api/workbench/repository/status?*',route=>route.fulfill({json:{data:{id:'example/project',name:'example/project',local_available:false,can_push:true,branch:'main',default_branch:'main',files:[],additions:null,deletions:null}}}));
  await page.route('**/api/task',route=>{
    calls.push({path:'task',body:route.request().postDataJSON()});return route.fulfill({status:202,json:{task_id:task.task_id}});
  });
  await page.route('**/api/workbench/repository/auto-preview',route=>{
    const body=route.request().postDataJSON();calls.push({path:'preview',body});
    return route.fulfill({json:{data:{id:'task-preview-'+body.action,action:body.action,github_task_id:task.task_id,diff:'+ reviewed remote change',confirmation:'server-owned task binding'}}});
  });
  await page.route('**/api/workbench/repository/auto-execute',route=>{
    calls.push({path:'execute',body:route.request().postDataJSON()});
    return route.fulfill({json:{data:{output:'approved task action completed (scripted)',github_task:{...task,pr:42}}}});
  });
  try{
    await page.reload();await page.selectOption('#repoSelect','example/project');await start(page);
    assert.equal(calls[0].body.github_repo,'example/project');
    await page.evaluate(task=>{
      window.sendEvent({type:'github_task',event_id:1,task});
      window.sendEvent({type:'done',event_id:2,status:'done',result:{ok:true,github_task:task,execution:{required:true,state:'unverified'}}});
    },task);
    await page.locator('#repoBar [data-repo-action="create-pr"]').click();
    await page.waitForFunction(()=>!document.querySelector('#repoExecute').disabled);
    assert.equal(await page.locator('#repoBase').count(),0);
    assert.equal(await page.locator('#repoTitle').count(),0);
    assert.equal(await page.locator('#repoConfirmation').isVisible(),false);
    assert.equal(calls.filter(c=>c.path==='execute').length,0);
    await page.getByRole('button',{name:'Approve Create PR',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#repoOutput').textContent.includes('approved task action completed'));
    await page.click('#repoClose');
    await page.reload();await page.selectOption('#repoSelect','example/project');
    await page.click('#repoMore');await page.locator('#repoActions [data-repo-action="merge"]').click();
    await page.waitForFunction(()=>!document.querySelector('#repoExecute').disabled);
    assert.equal(await page.locator('#repoNumber').count(),0);
    assert.equal(await page.locator('#repoConfirmation').isVisible(),false);
    await page.getByRole('button',{name:'Approve Merge',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#repoOutput').textContent.includes('approved task action completed'));
    assert.deepEqual(calls.filter(c=>c.path==='preview').map(c=>c.body),[
      {task_id:task.task_id,repo:'example/project',action:'create-pr'},
      {task_id:task.task_id,repo:'example/project',action:'merge'}
    ]);
    assert.ok(calls.filter(c=>c.path==='execute').every(c=>c.body.approved===true&&!('confirm' in c.body)));
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('GitHub can be deselected, stays off after reload and new tasks have no remote repository',async()=>{
 const {page,context,requests,errors}=await setup();
 try{
  await page.waitForFunction(()=>document.querySelector('#repoBranch').textContent.includes('feature-test'));
  await page.selectOption('#repoSelect','');
  assert.equal(await page.locator('#repoConnection').innerText(),'Connect GitHub');
  assert.equal(await page.evaluate(()=>window.nexusGitHubRepository()),null);
  assert.equal(await page.locator('#repoBar [data-repo-action="create-pr"]').isEnabled(),false);
  assert.match(await page.locator('#taskWorkspace').innerText(),/no GitHub repository attached/);
  requests.length=0;await page.reload();await page.waitForFunction(()=>document.querySelector('#metaUplink').textContent==='online');
  assert.equal(await page.locator('#repoConnection').innerText(),'Connect GitHub');assert.equal(await page.locator('#repoSelect').inputValue(),'');
  assert.equal(requests.some(r=>r.path.startsWith('/api/workbench/repository/')),false);
  await start(page);assert.equal(requests.find(r=>r.path==='/api/task').payload.github_repo,undefined);
  assert.deepEqual(errors,[]);
 }finally{await context.close();}
});

test('explicit GitHub off rejects late inventory results and reconnect does not auto-attach a repo',async()=>{
 const {page,context,errors}=await setup();let release;
 try{
  await page.waitForFunction(()=>document.querySelector('#repoBranch').textContent.includes('feature-test'));
  await page.route('**/api/workbench/repository/list*',route=>new Promise(resolve=>{release=async()=>{await route.fulfill({json:{data:[{id:'repo-fixture',name:'Nexus fixture',agent_workspace:true}]}});resolve();};}));
  await page.locator('#repoMore').click();await page.locator('#repoRefresh').click();await page.waitForFunction(()=>true);
  assert.ok(release,'Inventory request must be in flight before detaching');
  await page.locator('#repoConnection').click();await release();await page.waitForTimeout(100);
  assert.equal(await page.locator('#repoSelect').inputValue(),'');assert.equal(await page.evaluate(()=>window.nexusGitHubRepository()),null);
  await page.unroute('**/api/workbench/repository/list*');await page.locator('#repoConnection').click();
  await page.waitForFunction(()=>document.querySelector('#repoSelect').options.length>1);
  assert.equal(await page.locator('#repoSelect').inputValue(),'');
  await page.reload();await page.waitForFunction(()=>document.querySelector('#repoSelect').options.length>1);
  assert.equal(await page.locator('#repoSelect').inputValue(),'','Reconnecting without selection must remain unattached after reload');
  await page.selectOption('#repoSelect','repo-fixture');await page.waitForFunction(()=>document.querySelector('#repoBranch').textContent.includes('feature-test'));
  assert.equal(await page.evaluate(()=>window.nexusGitHubRepository()),'repo-fixture');assert.deepEqual(errors,[]);
 }finally{await context.close();}
});
