/** Actual original GUI + real configuration APIs; disposable workspace, no inference. */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';
const base=process.env.NEXUS_TEST_URL||'http://127.0.0.1:8787';
const token=process.env.NEXUS_GUI_TEST_TOKEN;
test('dashboard edits AI agent, role and ordered Red/Blue team in existing YAML without executing them',async()=>{
 assert.ok(token,'Use a disposable GUI workspace and fixture token');
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE_PATH||await bundled.executablePath(),args:bundled.args.filter(a=>!['--single-process','--disable-web-security'].includes(a)),headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();const errors=[],tasks=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname==='/api/task')tasks.push(r.url());});
 const suffix=String(Date.now()),agent='gui-test-agent-'+suffix,role='gui-test-role-'+suffix,team='gui-test-team-'+suffix;
 async function api(path,body){return page.evaluate(async({path,body})=>window.nexusGui.api(path,body),{path,body});}
 async function save(kind,name){page.once('dialog',d=>d.accept('SAVE '+kind.toUpperCase()+' '+name));await page.locator('#mcSave').click();await page.waitForFunction(({kind,name})=>document.getElementById('mcStatus').textContent.includes('Saved '+kind+' '+name),{kind,name});}
 try{
  await page.goto(base+'/dashboard#run');await page.locator('#guiToken').fill(token);await page.locator('#guiLogin button').click();await page.waitForFunction(()=>document.getElementById('metaUplink').textContent==='online');
  await page.locator('#taskInput').fill('Keep this draft; do not run');await page.locator('#guiManagement').click();await page.waitForFunction(()=>document.getElementById('mcStatus').textContent.includes('AI agents'));
  await page.locator('#mcKind').selectOption('agent');await page.locator('#mcNew').click();await page.locator('#mcName').fill(agent);await page.locator('#mc-model').fill('explicit-browser-fixture-model');await page.locator('#mc-base_url').fill('http://127.0.0.1:11434/v1');
  page.once('dialog',d=>d.accept('wrong'));await page.locator('#mcSave').click();assert.match(await page.locator('#mcStatus').innerText(),/Cancelled/);
  assert.equal((await api('/api/agents'))[agent],undefined);await save('agent',agent);assert.equal((await api('/api/agents'))[agent].model,'explicit-browser-fixture-model');
  await page.locator('#mcKind').selectOption('role');await page.locator('#mcNew').click();await page.locator('#mcName').fill(role);await page.locator('#mc-agent').selectOption(agent);await page.locator('#mc-persona').fill('শুধু অনুমোদিত কোড পর্যালোচনা করুন। Never execute external actions without approval.');await page.locator('#mc-category').fill('security');await save('role',role);
  assert.ok((await api('/api/roles')).some(r=>r.name===role&&r.agent===agent));
  await page.locator('#mcKind').selectOption('team');await page.locator('#mcNew').click();await page.locator('#mcName').fill(team);await page.getByRole('button',{name:'Add Red-team specialist',exact:true}).click();await page.getByRole('button',{name:'Add Blue-team specialist',exact:true}).click();
  await page.getByRole('button',{name:'Add member',exact:true}).click();await page.getByLabel('Team member 3', {exact:true}).selectOption(role);await save('team',team);
  const saved=(await api('/api/pipelines'))[team];assert.deepEqual(saved.steps.map(s=>s.role),['pentesting','blue-team',role]);assert.ok(saved.steps.every(s=>s.read_only));
  await page.locator('#mcUse').click();assert.equal(await page.locator('#pipelineSelect').inputValue(),team);assert.equal(await page.locator('#taskInput').inputValue(),'Keep this draft; do not run');assert.deepEqual(tasks,[]);
  await page.locator('#guiManagement').click();await page.waitForFunction(()=>document.getElementById('mcStatus').textContent.includes('AI agents'));await page.setViewportSize({width:430,height:900});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.locator('#mcGitHub').click();assert.equal(await page.locator('#repoBar').isVisible(),true);assert.equal(new URL(page.url()).pathname,'/dashboard');assert.deepEqual(errors,[]);
 }finally{
  for(const [kind,name] of [['team',team],['role',role],['agent',agent]]){try{const current=(await api('/api/workbench/configuration')).data;if(current.overrides[kind].includes(name))await api('/api/workbench/configuration',{kind,name,action:'remove',revision:current.revision,confirmation:'REMOVE '+kind.toUpperCase()+' '+name});}catch(e){console.error('Fixture cleanup failed:',e.message);}}
  await context.close();await browser.close();
 }
});
