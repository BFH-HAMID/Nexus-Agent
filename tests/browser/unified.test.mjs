/** Main /dashboard, actual shared identity and persistent operations; never inference. */
import {test,before,after,afterEach} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';
const base=process.env.NEXUS_TEST_URL||'http://127.0.0.1:8787';
const token=process.env.NEXUS_GUI_TEST_TOKEN;
let browser;
before(async()=>{assert.ok(token);browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE_PATH||await bundled.executablePath(),args:bundled.args.filter(a=>!['--single-process','--disable-web-security'].includes(a)),headless:true});});
afterEach(async()=>{for(const c of browser.contexts())await c.close();});
after(async()=>browser?.close());
async function open(width=1440,storageState,flat=true){
 const context=await browser.newContext({viewport:{width,height:1000},storageState,reducedMotion:flat?"no-preference":"reduce"});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+'/dashboard'+(flat?'?map=2d':'')+'#run');
 if(!storageState){await page.locator('#guiToken').fill(token);await page.locator('#guiLogin button').click();}
 await page.waitForFunction(()=>document.getElementById('metaUplink').textContent==='online');
 return {page,context,errors};
}
async function operations(page){await page.getByRole('tab',{name:'Operations & approvals',exact:true}).click();await page.waitForFunction(()=>document.getElementById('connection').textContent.includes('same Nexus GUI'));}
test('original Run/composer/repositories/models coexist with operations under one GUI and identity',async()=>{
 const {page,errors}=await open();
 await page.locator('#taskInput').fill('Preserved main GUI draft — do not execute');
 assert.equal(await page.locator('#runBtn').isVisible(),true);assert.equal(await page.locator('#repoBar').count(),1);
 await operations(page);assert.equal(await page.locator('#controls').isVisible(),true);
 assert.equal(await page.locator('#token').count(),0);assert.equal(await page.locator('iframe').count(),0);
 assert.match(await page.locator('#privacy-state').innerText(),/OFF/);
 await page.getByRole('tab',{name:'Run & chat',exact:true}).click();
 assert.equal(await page.locator('#taskInput').inputValue(),'Preserved main GUI draft — do not execute');
 await page.locator('#guiSettings').click();assert.equal(await page.locator('#workbench').isVisible(),true);await page.locator('#wbClose').click();
 assert.equal(new URL(page.url()).pathname,'/dashboard');assert.deepEqual(errors,[]);
});
test('main GUI background queue approval and cancellation use the real shared Studio store',async()=>{
 const {page,errors}=await open();await operations(page);
 const task='Unified GUI real queue '+Date.now();await page.locator('#task').fill(task);await page.locator('#delegate button').click();
 const row=page.locator('#jobs .item').filter({hasText:task});await row.waitFor();
 page.once('dialog',d=>d.accept('wrong'));await row.getByRole('button',{name:'Approve task',exact:true}).click();
 await page.waitForFunction(()=>document.getElementById('error').textContent.length>0);
 assert.equal(await row.getByRole('button',{name:'Approve task',exact:true}).isEnabled(),true);
 page.once('dialog',d=>d.accept(d.message().split('Type: ')[1]));await row.getByRole('button',{name:'Approve task',exact:true}).click();
 await page.waitForFunction(t=>[...document.querySelectorAll('#jobs .item')].find(n=>n.textContent.includes(t))?.textContent.includes('approved'),task);
 await row.getByRole('button',{name:'Cancel',exact:true}).click();
 await page.waitForFunction(t=>[...document.querySelectorAll('#jobs .item')].find(n=>n.textContent.includes(t))?.textContent.includes('cancelled'),task);
 assert.deepEqual(errors,[]);
});
test('main GUI lifecycle buttons receive actual worker acknowledgements without running a model',async()=>{
 const {page,errors}=await open();await operations(page);
 try{
 for(const [action,state] of [['start','running'],['pause','paused'],['resume','running'],['stop','stopped'],['start','running'],['kill','killed']]){
  if(['start','resume'].includes(action))page.once('dialog',d=>d.accept(action.toUpperCase()+' JARVIS'));
  await page.locator(`[data-control="${action}"]`).click();await page.waitForFunction(s=>document.getElementById('daemon').textContent.startsWith(s+' /'),state,{timeout:20000});
 }
 assert.deepEqual(errors,[]);
 }finally{await page.evaluate(()=>window.nexusGui.api('/api/operations/action/control',{action:'stop'})).catch(()=>{});}
});
test('world/news/weather are native sections of the original GUI and preserve ordinary scrolling',async()=>{
 const {page,errors}=await open(430);
 await operations(page);
 /* The world widget labels countries in the language stored in its own server
    settings; pin it to English so this assertion measures the widget, not a
    value another suite may have left behind. */
 const worldSettings=body=>page.evaluate(async b=>(await window.nexusGui.api('/api/workbench/world-settings',b)).data,body);
 const current=await worldSettings();
 const worldLanguage=current.settings.language;
 if(worldLanguage!=='en')
  await worldSettings({confirmation:'SAVE NEWS AND WEATHER',revision:current.revision,
   settings:{sources:current.settings.sources,weather_provider:current.settings.weather_provider,language:'en',enabled:current.settings.enabled},clear_session_key:false});
 const wrong=[];page.on('request',r=>{if(/\/api\/operations\/(countries|news|weather|crypto|cities|quakes)/.test(r.url()))wrong.push(r.url());});
 await page.getByRole('tab',{name:'World, news & weather',exact:true}).click();
 await page.waitForFunction(()=>document.getElementById('world-countries').children.length>200);
 assert.equal(await page.locator('#world-widget').isVisible(),true);assert.equal(new URL(page.url()).pathname,'/dashboard');assert.deepEqual(wrong,[]);
 assert.equal(await page.locator('#world-map .country').count()>150,true);
 await page.locator('#world-search').fill('Bangladesh');await page.locator('#world-jump').click();assert.equal(await page.locator('#world-country-name').innerText(),'Bangladesh');
 await page.locator('.world-stage').scrollIntoViewIfNeeded();const b=await page.locator('.world-stage').boundingBox();await page.mouse.move(b.x+b.width/2,b.y+80);const y=await page.evaluate(()=>scrollY);await page.mouse.wheel(0,400);await page.waitForFunction(old=>scrollY>old+100,y);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.getByRole('tab',{name:'Run & chat',exact:true}).click();assert.equal(await page.locator('#taskInput').isVisible(),true);
 assert.deepEqual(errors,[]);
 if(worldLanguage!=='en'){const now=await worldSettings();await worldSettings({confirmation:'SAVE NEWS AND WEATHER',revision:now.revision,
  settings:{sources:now.settings.sources,weather_provider:now.settings.weather_provider,language:worldLanguage,enabled:now.settings.enabled},clear_session_key:false});}
});
test('one remembered GUI login survives reload and a reopened browser context; logout forgets it',async()=>{
 const {page,context,errors}=await open();const cookie=(await context.cookies()).find(c=>c.name==='nexus_gui_session');
 assert.ok(cookie?.httpOnly);assert.equal(cookie.sameSite,'Strict');assert.ok(!cookie.value.includes(token));
 assert.equal((await page.evaluate(()=>JSON.stringify(localStorage)+JSON.stringify(sessionStorage))).includes(token),false);
 await operations(page);await page.reload();await page.waitForFunction(()=>document.getElementById('connection').textContent.includes('same Nexus GUI'));
 assert.equal(await page.locator('#guiLogin').isVisible(),false);
 const saved=await context.storageState();await context.close();const reopened=await open(1440,saved);
 await reopened.page.locator('#guiLogout').click();await reopened.page.waitForSelector('#guiLogin[open]');
 assert.equal((await reopened.context.cookies()).some(c=>c.name==='nexus_gui_session'),false);assert.deepEqual(errors,[]);assert.deepEqual(reopened.errors,[]);
});

test('native desktop World renders actual WebGL and hidden World does not restart public fetches',async()=>{
 const {page,errors}=await open(1440,undefined,false);await operations(page);
 await page.getByRole('tab',{name:'World, news & weather',exact:true}).click();
 await page.waitForFunction(()=>document.getElementById('world-countries').children.length>200);
 assert.match(await page.locator('#world-mode').innerText(),/3D WebGL/);
 assert.ok(await page.locator('#world-canvas canvas').count()>0);
 await page.locator('.world-stage').scrollIntoViewIfNeeded();const box=await page.locator('.world-stage').boundingBox();await page.mouse.move(box.x+box.width/2,box.y+80);
 const y=await page.evaluate(()=>scrollY);await page.mouse.wheel(0,400);await page.waitForFunction(old=>scrollY>old+100,y);
 await page.getByRole('tab',{name:'Run & chat',exact:true}).click();
 const requests=[];page.on('request',r=>{if(/\/api\/(news|weather|crypto|quakes)/.test(r.url()))requests.push(r.url());});
 await page.evaluate(()=>{for(const revision of [991,992])window.dispatchEvent(new CustomEvent('hud:world-update',{detail:{revision}}));});
 await page.waitForTimeout(200);assert.deepEqual(requests,[]);
 await page.evaluate(()=>window.dispatchEvent(new CustomEvent('nexus:event',{detail:{type:'approval_request'}})));
 assert.equal(await page.evaluate(()=>window.nexusGui.view),'run');assert.deepEqual(errors,[]);
});

test('unchecked trusted-browser option stays session-only across API cookie renewal',async()=>{
 const context=await browser.newContext();const page=await context.newPage();
 await page.goto(base+'/dashboard');await page.locator('#guiRemember').uncheck();await page.locator('#guiToken').fill(token);await page.locator('#guiLogin button').click();
 await page.waitForFunction(()=>document.getElementById('metaUplink').textContent==='online');
 await page.evaluate(()=>window.nexusGui.api('/api/stats'));
 const cookie=(await context.cookies()).find(c=>c.name==='nexus_gui_session');assert.ok(cookie?.httpOnly);assert.equal(cookie.expires,-1);
 await page.reload();await page.waitForFunction(()=>document.getElementById('metaUplink').textContent==='online');assert.equal(await page.locator('#guiLogin').isVisible(),false);
 assert.equal((await context.cookies()).find(c=>c.name==='nexus_gui_session').expires,-1);
});
