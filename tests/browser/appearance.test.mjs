/** Real authenticated desktop discovery, navigation and appearance acceptance. */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';
const base=process.env.NEXUS_TEST_URL||'http://127.0.0.1:8787';
const shot=name=>fileURLToPath(new URL('../../docs/images/gui-smart-'+name+'.png',import.meta.url));
test('modern desktop controls combine search, categories and persistent favourites without executing tasks',async()=>{
 const browser=await chromium.launch({executablePath:await bundled.executablePath(),args:bundled.args.filter(a=>!['--single-process','--disable-web-security'].includes(a)),headless:true});
 const page=await browser.newPage({viewport:{width:1600,height:1100},reducedMotion:'reduce'}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto(base+'/dashboard?map=2d');await page.locator('#guiToken').fill(process.env.NEXUS_GUI_TEST_TOKEN);await page.locator('#guiLogin button').click();await page.waitForFunction(()=>document.getElementById('metaUplink').textContent==='online');
  /* Mission Control is the landing page; the control cards live one section over. */
  await page.locator('.sidebar-nav [data-nav-id="controls"]').click();await page.waitForFunction(()=>document.getElementById('view-control')&&!document.getElementById('view-control').hidden);
  assert.equal(await page.locator('.dc-card').count(),18);
  assert.deepEqual(await page.locator('.ui-live-strip b').allTextContents(),await page.evaluate(()=>['metaUplink','metaRoles','metaAgents'].map(id=>document.getElementById(id).textContent)));
  await page.getByRole('button',{name:'Favourites',exact:true}).click();assert.equal(await page.locator('.dc-card:visible').count(),0);assert.match(await page.locator('#dcEmpty').innerText(),/No favourites/);
  await page.getByRole('button',{name:'Clear filters',exact:true}).click();await page.getByRole('button',{name:'Favourite Context & privacy',exact:true}).click();
  await page.getByRole('button',{name:'Favourites',exact:true}).click();assert.equal(await page.locator('.dc-card:visible').count(),1);
  await page.locator('#dcSearch').fill('unmatched-feature');assert.equal(await page.locator('.dc-card:visible').count(),0);
  await page.getByRole('button',{name:'Clear filters',exact:true}).click();assert.equal(await page.locator('.dc-card:visible').count(),18);
  await page.getByRole('button',{name:'Execution',exact:true}).click();assert.equal(await page.locator('.dc-card:visible').count(),5);
  await page.getByRole('button',{name:'Clear filters',exact:true}).click();
  await page.getByRole('button',{name:'Compose a task',exact:true}).click();assert.equal(await page.locator('#taskInput').evaluate(n=>document.activeElement===n),true);
  await page.locator('#taskInput').fill('Keep this draft; do not execute');await page.locator('[data-gui-tab="control"]').click();await page.getByRole('button',{name:'Compose a task',exact:true}).click();assert.equal(await page.locator('#taskInput').inputValue(),'Keep this draft; do not execute');await page.locator('#taskInput').fill('');await page.locator('[data-gui-tab="control"]').click();
  await page.getByRole('button',{name:'Review approvals',exact:true}).click();assert.equal(await page.locator('#reviews').isVisible(),true);await page.locator('[data-gui-tab="control"]').click();
  await page.getByRole('button',{name:'Compact layout',exact:true}).click();await page.getByRole('button',{name:'Reduce effects',exact:true}).click();
  await page.reload();await page.waitForFunction(()=>document.getElementById('metaUplink').textContent==='online');
  assert.equal(await page.locator('body').evaluate(n=>n.classList.contains('ui-still')&&n.classList.contains('ui-compact')),true);
  await page.locator('.sidebar-nav [data-nav-id="controls"]').click();await page.waitForFunction(()=>document.getElementById('view-control')&&!document.getElementById('view-control').hidden);
  assert.equal(await page.getByRole('button',{name:'Favourite Context & privacy',exact:true}).getAttribute('aria-pressed'),'true');
  await page.getByRole('button',{name:'Compact layout',exact:true}).click();await page.getByRole('button',{name:'Reduce effects',exact:true}).click();
  await page.screenshot({path:shot('control'),fullPage:true,animations:'disabled'});
  await page.locator('#guiSettings').click();assert.equal(await page.locator('#workbench').isVisible(),true);await page.screenshot({path:shot('settings'),animations:'disabled'});await page.locator('#wbClose').click();
  for(const [view,title] of [['run','Run & chat'],['operations','Your work, in focus'],['world','A wider perspective']]){
   await page.locator(`[data-gui-tab="${view}"]`).click();assert.equal(await page.locator(`[data-gui-view="${view}"] .ui-section-heading h2`).innerText(),title);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,view+' must not overflow');await page.screenshot({path:shot(view),fullPage:true,animations:'disabled'});
  }
  assert.equal(await page.locator('.ui-preferences button').first().evaluate(n=>getComputedStyle(n).transitionDuration),'0s');assert.equal(await page.locator('iframe').count(),0);assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});
test('malformed or unavailable browser storage does not break discovery',async()=>{
 const browser=await chromium.launch({executablePath:await bundled.executablePath(),args:bundled.args.filter(a=>!['--single-process','--disable-web-security'].includes(a)),headless:true});
 try{
  for(const mode of ['malformed','blocked']){
   const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
   await context.addInitScript(mode=>{if(mode==='blocked'){Object.defineProperty(window,'localStorage',{get(){throw new DOMException('Blocked','SecurityError');}});}else{localStorage.setItem('nexus.ui.favourites','{"invalid":true}');}},mode);
   const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(base+'/dashboard');await page.locator('#guiToken').fill(process.env.NEXUS_GUI_TEST_TOKEN);await page.locator('#guiLogin button').click();await page.waitForFunction(()=>document.getElementById('metaUplink').textContent==='online');
   await page.locator('.sidebar-nav [data-nav-id="controls"]').click();await page.waitForFunction(()=>document.getElementById('view-control')&&!document.getElementById('view-control').hidden);
   await page.getByRole('button',{name:'Favourite Context & privacy',exact:true}).click();await page.getByRole('button',{name:'Favourites',exact:true}).click();assert.equal(await page.locator('.dc-card:visible').count(),1);assert.deepEqual(errors,[]);await context.close();
  }
 }finally{await browser.close();}
});
