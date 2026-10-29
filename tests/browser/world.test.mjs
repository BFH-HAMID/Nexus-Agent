/** Actual local Three/WebGL/SVG and HUD auth; ONLY public-feed responses are explicit test fixtures. */
import {test,before,after,afterEach} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';
const base=process.env.NEXUS_WORLD_TEST_URL||'http://127.0.0.1:8787';
const token=process.env.NEXUS_WORLD_TEST_TOKEN||fs.readFileSync('/tmp/nexus-world-preview-token','utf8');
let browser;
before(async()=>{browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE_PATH||await bundled.executablePath(),args:[...bundled.args.filter(a=>a!=='--single-process'),'--use-angle=swiftshader','--enable-unsafe-swiftshader'],headless:true});});
after(async()=>browser?.close());
afterEach(async()=>{for(const c of browser.contexts())await c.close();});
const now=Math.floor(Date.now()/1000), hours=Array.from({length:168},(_,i)=>now+i*3600), days=Array.from({length:7},(_,i)=>now+i*86400);
const article={id:'explicit-browser-fixture',title:'SCRIPTED TEST Bangladesh headline <img src=x onerror="window.worldXss=1">',summary:'TEST ONLY: literal untrusted excerpt, not live reporting.',source:'SCRIPTED TEST SOURCE',source_id:'test',category:'World',published:now,url:'https://example.org/test-fixture',countries:['BGD']};
const row=data=>({data,status:'live',offline:false,stale:false,at:now});
const forecast=row({current:{temperature_2m:26,apparent_temperature:28,relative_humidity_2m:70,wind_speed_10m:9,precipitation:1,weather_code:95,is_day:1},timezone:'Asia/Dhaka',hourly:{time:hours,temperature_2m:hours.map((_,i)=>25+i%6),precipitation_probability:hours.map(()=>65)},daily:{time:days,temperature_2m_max:days.map(()=>32),temperature_2m_min:days.map(()=>22),weather_code:days.map(()=>3),sunrise:days,sunset:days.map(t=>t+43200),uv_index_max:days.map(()=>5)},alerts:['SCRIPTED TEST thunderstorm threshold'],alert_notice:'NOT an official warning'});
async function open({fallback=false,width=1440,motion='no-preference',fixtures=true}={}){
 const context=await browser.newContext({viewport:{width,height:1100},reducedMotion:motion});
 const page=await context.newPage(),errors=[],violations=[],external=[];page.setDefaultTimeout(12000);
 page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error'&&/Content Security Policy|violat/i.test(m.text()))violations.push(m.text());});
 page.on('request',r=>{if(!r.url().startsWith(base)&&!r.url().startsWith('data:'))external.push(r.url());});
 if(fallback)await page.addInitScript(()=>{const original=HTMLCanvasElement.prototype.getContext;HTMLCanvasElement.prototype.getContext=function(type,...args){return /webgl/i.test(type)?null:original.call(this,type,...args);};});
 if(fixtures){
 await page.route('**/api/news',r=>r.fulfill({json:{items:[article,{...article,id:'bangla-test',title:'SCRIPTED TEST: দিনাজপুরে বৃষ্টি',category:'Bangladesh'}],sources:[{id:'test',name:'SCRIPTED TEST SOURCE',status:'live',at:now,terms:'https://example.org/terms'}],status:'ready',hotspots:{BGD:2}}}));
 await page.route('**/api/crypto',r=>r.fulfill({json:row({provider:'SCRIPTED TEST QUOTES',items:[{symbol:'BTC',price:100,quote:'USD',change:-2,spark:[100,102,100],spark_period:'scripted test samples'}]})}));
 await page.route('**/api/weather*',r=>r.fulfill({json:forecast}));
 await page.route('**/api/quakes',r=>r.fulfill({json:row([{id:'test-quake',lat:25,lon:88,magnitude:4,time:now,place:'SCRIPTED TEST quake',url:'https://example.org'}])}));
 await page.route('**/api/cities*',r=>r.fulfill({json:row([{name:'SCRIPTED TEST city',latitude:23.7,longitude:90.4}])}));
 }
 await page.goto(base);await page.locator('#token').fill(token);await page.locator('#connect').click();
 await page.waitForFunction(()=>document.getElementById('connection').textContent.includes('LIVE'));
 await page.waitForFunction(()=>document.getElementById('world-countries').children.length>200);
 await page.waitForFunction(()=>/3D WebGL|2D SVG/.test(document.getElementById('world-mode').textContent));
 return {page,context,errors,violations,external};
}
test('actual WebGL, locally bundled assets, safe RSS rendering, prices and seven-day forecast',async()=>{
 const {page,context,errors,violations,external}=await open();
 assert.equal(await page.locator('#world-canvas canvas').count(),1);
 assert.match(await page.locator('#world-mode').innerText(),/3D WebGL/);
 await page.locator('#world-news-list').getByText(article.title,{exact:true}).waitFor();
 assert.equal(await page.locator('#world-news-list img').count(),0);assert.equal(await page.evaluate(()=>window.worldXss),undefined);
 assert.equal(await page.locator('#world-news-list a').first().getAttribute('rel'),'noopener noreferrer');
 await page.waitForFunction(()=>document.querySelectorAll('#world-forecast>div').length===7);
 assert.match(await page.locator('#world-weather-alert').innerText(),/NOT an official warning/);
 assert.match(await page.locator('#world-crypto').innerText(),/SCRIPTED TEST QUOTES/);
 assert.deepEqual(errors,[]);assert.deepEqual(violations,[]);assert.deepEqual(external,[]);
 await context.close();
});
test('country search/focus, Bangla toggle, layers, Home, ticker and fullscreen',async()=>{
 const {page,context,errors}=await open({fallback:true});
 await page.locator('#world-search').fill('Bangladesh');await page.locator('#world-jump').click();
 assert.equal(await page.locator('#world-country-name').innerText(),'Bangladesh');
 await page.getByRole('tab',{name:'বাংলাদেশ / দিনাজপুর'}).click();
 await page.locator('#world-news-list').getByText('SCRIPTED TEST: দিনাজপুরে বৃষ্টি').waitFor();
 await page.locator('#world-language').click();assert.equal(await page.locator('#world-country-name').innerText(),'বাংলাদেশ');
 await page.locator('[data-layer="quakes"]').uncheck();await page.locator('[data-layer="terminator"]').uncheck();
 await page.locator('#world-home').click();assert.match(await page.locator('#world-weather-name').innerText(),/Dinajpur/);
 await page.locator('#world-ticker-pause').click();assert.equal(await page.locator('#world-ticker-pause').innerText(),'Resume ticker');
 await page.locator('#world-fullscreen').click();await page.waitForFunction(()=>document.fullscreenElement?.id==='world-widget'||document.getElementById('world-status').textContent.includes('refused full-screen'));if(await page.evaluate(()=>!!document.fullscreenElement))await page.evaluate(()=>document.exitFullscreen());
 assert.deepEqual(errors,[]);await context.close();
});
test('no WebGL uses actual interactive SVG borders, hover, country click and zoom',async()=>{
 const {page,context,errors}=await open({fallback:true});
 await page.locator('#world-map .country').first().waitFor({state:'attached'});
 assert.match(await page.locator('#world-mode').innerText(),/2D SVG fallback/);
 assert.ok(await page.locator('#world-map .country').count()>150);
 assert.equal(await page.locator('#world-map [data-country]').evaluateAll(nodes=>new Set(nodes.map(n=>n.getAttribute('data-country'))).size),242);
 const path=page.locator('#world-map [aria-label="Bangladesh"]');await path.focus();await path.press('Enter');
 assert.equal(await page.locator('#world-country-name').innerText(),'Bangladesh');
 const before=await page.locator('#world-map').getAttribute('viewBox');await page.locator('#world-zoom-in').click();assert.notEqual(await page.locator('#world-map').getAttribute('viewBox'),before);
 assert.deepEqual(errors,[]);await context.close();
});
test('city search/favorites are opt-in local data and never store HUD token',async()=>{
 const {page,context,errors}=await open({fallback:true});
 assert.equal(await page.evaluate(()=>localStorage.length),0);
 await page.locator('#world-city').fill('Dhaka');await page.locator('#world-city-form button').click();
 await page.locator('#world-city-results button').first().click();await page.locator('#world-favorite').click();
 assert.match(await page.locator('#world-favorites').innerText(),/SCRIPTED TEST city/);
 const stored=await page.evaluate(()=>JSON.stringify(localStorage));assert.equal(stored.includes(token),false);assert.match(stored,/nexus-world-favorites/);
 await page.getByRole('button',{name:'Remove SCRIPTED TEST city'}).click();assert.equal(await page.locator('#world-favorites button').count(),0);
 assert.deepEqual(errors,[]);await context.close();
});
test('narrow/reduced-motion layout fits viewport and disables decorative CSS motion',async()=>{
 const {page,context,errors}=await open({fallback:true,width:430,motion:'reduce'});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 assert.equal(await page.locator('#world-current .world-weather-icon').evaluate(n=>getComputedStyle(n).animationName),'none');
 assert.equal(await page.locator('#world-ticker-text').evaluate(n=>getComputedStyle(n).animationName),'none');
 await page.locator('#disconnect').click();assert.match(await page.locator('#world-status').innerText(),/Disconnected/);
 assert.deepEqual(errors,[]);await context.close();
});
