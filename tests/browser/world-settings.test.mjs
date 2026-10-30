/** Real GUI/settings persistence; ONLY the public news/weather display is fixture data. */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';
const base=process.env.NEXUS_TEST_URL||'http://127.0.0.1:8787',token=process.env.NEXUS_GUI_TEST_TOKEN;
test('native Bangladesh RSS settings and WeatherAPI key setup are secret-safe and usable from the control shortcuts',async()=>{
 assert.ok(token);const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE_PATH||await bundled.executablePath(),args:bundled.args.filter(a=>!['--single-process','--disable-web-security'].includes(a)),headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 const key='explicit-browser-weather-fixture-'+'x'.repeat(32);let before;
 const api=body=>page.evaluate(async body=>(await window.nexusGui.api('/api/workbench/world-settings',body)).data,body);
 try{
  await page.goto(base+'/dashboard?map=2d');await page.locator('#guiToken').fill(token);await page.locator('#guiLogin button').click();await page.waitForFunction(()=>document.getElementById('metaUplink').textContent==='online');before=await api();
  await page.locator('#guiSettings').click();
  for(const [name,pane] of [['AI downloads & capabilities','models'],['Model routing & safety','policy'],['History & planning','runs'],['Waiting task queue','queue'],['File diffs','files'],['Saved error history','errors']]){
   await page.getByRole('button',{name,exact:true}).click();assert.ok(await page.locator('#wbPane-'+pane).isVisible());await page.locator('#workbench [data-pane="overview"]').click();
  }
  await page.getByRole('button',{name:'Studio workflows',exact:true}).click();assert.ok(await page.locator('#nexusStudio').isVisible());await page.locator('#stClose').click();await page.locator('#guiSettings').click();
  await page.getByRole('button',{name:'GitHub attachment',exact:true}).click();assert.ok(await page.locator('#repoBar').isVisible());await page.locator('#guiSettings').click();
  await page.getByRole('button',{name:'Worker & approvals',exact:true}).click();assert.ok(await page.locator('#view-operations').isVisible());await page.locator('#guiSettings').click();
  await page.getByRole('button',{name:'RSS & WeatherAPI settings',exact:true}).click();await page.waitForFunction(()=>document.getElementById('wsSources').children.length===4);
  assert.ok(await page.locator('#wsSources input[data-category="Bangladesh"]').count()>=8);
  await page.locator('#wsProvider').selectOption('weatherapi');await page.locator('#wsLanguage').selectOption('bn');await page.locator('#wsBangladesh').click();await page.locator('#wsKey').fill(key);
  page.once('dialog',d=>d.accept('wrong'));await page.locator('#wsSave').click();assert.match(await page.locator('#wsStatus').innerText(),/Cancelled/);
  await page.locator('#wsKey').fill(key);page.once('dialog',d=>d.accept('SAVE NEWS AND WEATHER'));await page.locator('#wsSave').click();await page.waitForFunction(()=>document.getElementById('wsStatus').textContent.startsWith('Saved.'));
  const after=await api();assert.ok(after.weather_key_present);assert.ok(after.settings.sources.includes('dhaka-tribune'));assert.equal(after.settings.sources.length,8);assert.equal(JSON.stringify(after).includes(key),false);assert.equal(await page.locator('#wsKey').inputValue(),'');assert.equal(await page.evaluate(k=>(JSON.stringify(localStorage)+JSON.stringify(sessionStorage)).includes(k),key),false);
  const now=Date.now()/1000;
  await page.route('**/api/news',r=>r.fulfill({json:{items:[{id:'fixture-bd',title:'EXPLICIT RSS TEST ঢাকার সংবাদ <img onerror=bad()>',summary:'SCRIPTED TEST ONLY',url:'https://example.org/test-news',source:'Prothom Alo · scripted fixture',source_id:'prothom-alo',category:'Bangladesh',published:now,countries:['BGD']}],sources:[],status:'ready',hotspots:{BGD:1}}}));
  await page.route('**/api/weather?*',r=>r.fulfill({json:{status:'live',at:now,data:{provider:'WeatherAPI.com',provider_url:'https://www.weatherapi.com/',timezone:'Asia/Dhaka',current:{temperature_2m:30,is_day:1,weather_code:3},daily:{time:[now],temperature_2m_max:[32],temperature_2m_min:[24],weather_code:[3]},hourly:{time:[now+3600],temperature_2m:[31]},alerts:[]}}}));
  await page.locator('#wsKey').fill(key);await page.locator('#wbClose').click();await page.waitForFunction(()=>document.getElementById('wsKey').value==='');assert.equal(await page.locator('#wsKey').inputValue(),'');await page.getByRole('tab',{name:'World, news & weather',exact:true}).click();await page.waitForFunction(()=>document.getElementById('world-news-list').textContent.includes('EXPLICIT RSS TEST'));
  assert.equal(await page.locator('#world-news-list img').count(),0);assert.match(await page.locator('#world-current').innerText(),/30/);assert.match(await page.locator('#world-weather-credit').innerText(),/WeatherAPI.com/);
  await page.locator('#guiWorldSettings').click();await page.setViewportSize({width:430,height:900});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(errors,[]);
 }finally{
  if(before){try{const current=await api();const cfg=before.settings;await api({confirmation:'SAVE NEWS AND WEATHER',revision:current.revision,settings:{sources:cfg.sources,weather_provider:cfg.weather_provider,language:cfg.language,enabled:cfg.enabled},clear_session_key:true});}catch(e){console.error('Disposable World fixture cleanup:',e.message);}}
  await context.close();await browser.close();
 }
});
