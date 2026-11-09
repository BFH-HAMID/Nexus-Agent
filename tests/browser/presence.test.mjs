/** Shared HUD: real Chromium DOM with fake APIs/audio only. No real server,
 * model, microphone, credential, push service or OS service is contacted. */
import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';

let browser;
const asset = name => fileURLToPath(new URL(`../../nexus/panel/${name}`, import.meta.url));
before(async () => {
  browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter(a => !['--single-process','--disable-web-security'].includes(a)), headless:true});
});
after(async () => { await browser?.close(); });

async function panel({width=1200, stream=false, realMic=false}={}) {
  const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await context.route('**/*',route=>route.abort());
  await page.setContent('<!doctype html><html lang="en"><head></head><body><main class="deck"></main><section id="view-world"><div id="world-widget"></div></section></body></html>');
  for(const css of ['dashboard.css','shell.css','assistant.css','presence.css'])await page.addStyleTag({path:asset(css)});
  await page.evaluate(({stream})=>{
    window.calls=[];window.paused=0;window.micStarts=0;window.micStops=0;
    window.fixture={enabled:true,devices_enabled:true,conversation_enabled:true,voice_enabled:true,handoff_enabled:true,push_enabled:false,world_enabled:true,
      principal:{kind:'device',id:'phone',scopes:['read','chat','voice','approve','handoff']}};
    window.snapshot={revision:1,conversation_id:'shared-fixture-conversation',speaker:'phone',speaker_generation:1,active_mission:'chief-deadbeef',
      principal:window.fixture.principal,capabilities:window.fixture,
      voice:{state:'idle',source:'local'},turns:[{id:'turn',source:'phone',user:'বাংলা <img src=x onerror=alert(1)>',assistant:'Observed reply, not a test claim.',status:'done',usage:{input_tokens:3,output_tokens:4}}],
      vision:{enabled:false,camera:{state:'off'},screen:{state:'off'}},
      missions:[{id:'chief-deadbeef',goal:'Actual fixture mission',status:'running',cost_usd:.21,cost_cap_usd:2,
        nodes:[{id:'plan',title:'Plan',status:'done',depends_on:[]},{id:'review',title:'Review',status:'pending',depends_on:['plan']}]}],
      approvals:[{id:'card-one',reference:'task-fixture',approval_id:'fresh-fixture',source:'live',tool:'run_command',title:'Exact requested action',
        action_text:JSON.stringify({tool:'run_command',arguments:{command:'echo '+ 'long_exact_word_'.repeat(50)}}),
        complete:true,token:'APPROVE ACTION fresh-fixture',decisions:['approve','deny']}],
      devices:[{id:'phone',name:'My phone',scopes:['read','chat','voice','approve','handoff'],expires:Date.now()/1000+10000,revoked:false}],
      speech:[],briefs:[{id:'brief',text:'A recorded brief.'}],service:{state:'not_observed'}};
    let review;
    window.nexusGui={view:stream?'presence':'run',ready:Promise.resolve(),
      show(view){this.view=view;window.dispatchEvent(new CustomEvent('nexus:view',{detail:view}));},
      async mountWorld(host,api){host.append(document.getElementById('world-widget'));await api('countries');},
      async api(path,body){
        window.calls.push({path,body});
        if(path==='/api/presence/config')return window.fixture;
        if(path==='/api/presence/review'){review=structuredClone(body);return{id:'one-use-review',confirmation:'CONFIRM '+body.operation.toUpperCase()+' fixture',arguments:body.arguments};}
        if(path==='/api/presence/execute')return review.operation==='enroll'?{id:'device',token:'not-a-real-device-token'}:{task_id:'task',queued:true};
        if(path==='/api/presence/approval')return{accepted:true};
        if(path==='/api/presence/voice/transcribe')return{transcript:'শুধু খসড়া <script>evil()</script>',language:'bn'};
        if(path==='/api/presence/voice/state' || path==='/api/presence/revoke')return{observed:true};
        if(path==='/api/presence/speech')return{audio_base64:btoa('fixture-only'),format:'wav',generation:1};
        if(path==='/api/presence/world/countries')return{items:[],config:{enabled:false}};
        throw Error('Missing offline fixture for '+path);
      },
      async fetch(path,options={}){
        window.calls.push({path,headers:options.headers});
        if(path!=='/api/presence/events')throw Error('Unexpected realtime endpoint');
        const encoder=new TextEncoder();
        return{ok:true,body:new ReadableStream({start(controller){
          window.pushFrame=(event,data)=>{
            const value=encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
            // Split both frame lines and multi-byte Bangla, just like a TCP stream.
            for(let i=0;i<value.length;i+=7)controller.enqueue(value.slice(i,i+7));
          };
          window.endStream=()=>controller.close();
          options.signal.addEventListener('abort',()=>{try{controller.close();}catch{}});
          window.pushFrame('presence.state',{reset:true,state:window.snapshot});
        }})};
      }};
    window.nexusAssistant={state:()=>({microphone:window.recording}),waveform:()=>null,draftAudio:()=>window.audioDraft||'',
      async startRecording(){window.micStarts++;window.recording=true;},
      stopRecording(){window.micStops++;window.recording=false;window.audioDraft=btoa('fake microphone draft');},stop(){},discardAudio(){window.audioDraft='';}};
    window.Audio=class{async play(){window.played=(window.played||0)+1;}pause(){window.paused++;}};
  },{stream});
  await page.addScriptTag({path:asset('i18n.js')});
  if(realMic)await page.addScriptTag({path:asset('assistant.js')});
  await page.addScriptTag({path:asset('presence.js')});
  if(!stream)await page.evaluate(()=>{document.getElementById('view-presence').hidden=false;window.nexusPresence.setConfig(window.fixture);window.nexusPresence.accept({state:window.snapshot});});
  else await page.waitForFunction(()=>window.nexusPresence.state().connected);
  return {page,context,errors};
}

for(const width of [1200,360])test(`HUD exact reviews, DAG, cost and Bangla remain usable at ${width}px`,async()=>{
  const {page,context,errors}=await panel({width});
  try{
    assert.equal(await page.locator('.ph-node').count(),2);
    assert.match(await page.locator('#phMissions').innerText(),/review ← plan/);
    assert.match(await page.locator('#phCost').innerText(),/\$0.2100/);
    assert.equal(await page.locator('#phCostMeter').getAttribute('max'),'2');
    assert.equal(await page.locator('#phTranscript img').count(),0);
    assert.match(await page.locator('#phTranscript').innerText(),/<img/);
    await page.locator('[data-approval-action="approve"]').click();
    const exact=await page.locator('#phReviewText').innerText();
    assert.equal(exact,await page.evaluate(()=>window.snapshot.approvals[0].action_text));
    await page.locator('#phReviewInput').fill('yes');await page.locator('#phReviewConfirm').click();
    assert.equal(await page.locator('#phReview').evaluate(n=>n.open),true);
    assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.path==='/api/presence/approval').length),0);
    await page.locator('#phReviewInput').fill('APPROVE ACTION fresh-fixture');await page.locator('#phReviewConfirm').click();
    await page.waitForFunction(()=>window.calls.some(c=>c.path==='/api/presence/approval'));
    const payload=await page.evaluate(()=>window.calls.find(c=>c.path==='/api/presence/approval').body);
    assert.equal(payload.approval_id,'fresh-fixture');assert.equal(payload.decision,'approve');assert.equal(payload.scope,undefined);
    await page.locator('#phLanguage').click();
    assert.equal(await page.locator('html').getAttribute('lang'),'bn');
    assert.match(await page.locator('#phPtt').innerText(),/চেপে/);
    assert.equal(await page.evaluate(()=>window.nexusI18n.missing().filter(k=>k.startsWith('presence.')).length),0);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth),0);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('turn review is actor intent only; editing after review does not silently change the executed payload',async()=>{
  const {page,context,errors}=await panel();
  try{
    await page.locator('#phDraft').fill('exact original turn');await page.locator('#phSend').click();
    await page.waitForSelector('#phReview[open]');
    assert.match(await page.locator('#phReviewText').innerText(),/exact original turn/);
    await page.evaluate(()=>{document.getElementById('phDraft').value='later draft';});
    await page.locator('#phReviewInput').fill('CONFIRM CHAT fixture');await page.locator('#phReviewConfirm').click();
    await page.waitForFunction(()=>window.calls.some(c=>c.path.endsWith('/execute')));
    const calls=await page.evaluate(()=>window.calls);
    assert.deepEqual(calls.find(c=>c.path.endsWith('/review')).body.arguments,{text:'exact original turn',read_only:true});
    assert.deepEqual(calls.find(c=>c.path.endsWith('/execute')).body,{id:'one-use-review',confirmation:'CONFIRM CHAT fixture'});
    assert.equal(await page.locator('#phDraft').inputValue(),'later draft');
    assert.match(await page.locator('#phDraftStatus').innerText(),/not completed/);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('one authenticated SSE channel handles fragmented UTF-8; revocation clears private content and disables actions',async()=>{
  const {page,context,errors}=await panel({stream:true});
  try{
    await page.evaluate(()=>{window.dispatchEvent(new Event('nexus:authenticated'));window.dispatchEvent(new Event('nexus:authenticated'));});
    assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.path.endsWith('/events')).length),1);
    assert.match(await page.locator('#phTranscript').innerText(),/বাংলা/);
    const request=await page.evaluate(()=>window.calls.find(c=>c.path.endsWith('/events')));
    assert.ok(!request.path.includes('?'));assert.equal(request.headers['Last-Event-ID'],'0');
    await page.evaluate(()=>window.pushFrame('presence.revoked',{revoked:true}));
    await page.waitForFunction(()=>!window.nexusPresence.state().connected);
    assert.equal(await page.locator('#phTranscript').innerText(),'');
    assert.equal(await page.locator('#phApprovals').innerText(),'');
    assert.equal(await page.locator('#phSend').isDisabled(),true);
    assert.equal(await page.locator('#phOrb').getAttribute('data-state'),'unknown');
    assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.path.endsWith('/execute')).length),0);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('read-only device and incomplete actions cannot grant; cached World uses the scoped read adapter',async()=>{
  const {page,context,errors}=await panel();
  try{
    await page.evaluate(()=>{window.fixture.principal.scopes=['read'];window.snapshot.approvals[0].complete=false;window.snapshot.approvals[0].decisions=['deny'];window.nexusPresence.accept({state:window.snapshot});});
    assert.equal(await page.locator('[data-approval-action="approve"]').count(),0);
    assert.equal(await page.locator('[data-approval-action="deny"]').isDisabled(),true);
    assert.equal(await page.locator('#phSend').isDisabled(),true);
    assert.equal(await page.locator('#phHandoff').isDisabled(),true);
    await page.locator('#phWorld').click();
    await page.waitForFunction(()=>window.calls.some(c=>c.path==='/api/presence/world/countries'));
    assert.equal(await page.locator('#phWorldHost #world-widget').count(),1);
    assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.path==='/api/news').length),0);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('PTT reuses local recording and transcription is a separate reviewed draft, never chat or approval',async()=>{
  const {page,context,errors}=await panel();
  try{
    const mic=page.locator('#phPtt');await mic.hover();await page.mouse.down();await page.waitForFunction(()=>window.micStarts===1);await page.mouse.up();
    await page.waitForFunction(()=>window.micStarts===1);
    await page.locator('#phTranscribe').click();await page.waitForSelector('#phReview[open]');
    await page.locator('#phReviewInput').fill('TRANSCRIBE AUDIO');await page.locator('#phReviewConfirm').click();
    await page.waitForFunction(()=>document.getElementById('phDraft').value.includes('শুধু'));
    assert.equal(await page.locator('#view-presence script').count(),0);
    assert.equal(await page.evaluate(()=>window.calls.filter(c=>/\/execute$|\/approval$/.test(c.path)).length),0);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('releasing PTT before a late browser permission grant stops the actual granted tracks',async()=>{
  const {page,context,errors}=await panel({realMic:true});
  try{
    await page.evaluate(()=>{
      window.trackStops=0;
      Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{getUserMedia:()=>new Promise(resolve=>{window.grantMic=()=>resolve({getTracks:()=>[{stop:()=>window.trackStops++}]});})}});
      window.pendingMic=window.nexusAssistant.startRecording();window.nexusAssistant.stopRecording();window.grantMic();
    });
    await page.waitForFunction(()=>window.trackStops===1);
    assert.equal(await page.evaluate(()=>window.nexusAssistant.state().microphone),false);
    assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.path.includes('voice/transcribe')).length),0);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('speech requires local playback opt-in, claims each ID once, and stops on handoff',async()=>{
  const {page,context,errors}=await panel();
  try{
    await page.evaluate(()=>{window.snapshot.speech=[{id:'say-once',device:'phone'}];window.nexusPresence.accept({state:window.snapshot});});
    assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.path.endsWith('/speech')).length),0);
    await page.locator('#phListen').click();await page.waitForFunction(()=>window.played===1);
    await page.evaluate(()=>window.nexusPresence.accept({state:window.snapshot}));
    assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.path.endsWith('/speech')).length),1);
    await page.evaluate(()=>{window.snapshot.speaker='none';window.snapshot.speaker_generation=2;window.snapshot.speech=[];window.nexusPresence.accept({state:window.snapshot});});
    assert.equal(await page.evaluate(()=>window.paused),1);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('service worker caches only static shell; push ignores hostile payload and opens a fixed same-origin HUD',async()=>{
  const code=await readFile(asset('assistant-sw.js'),'utf8'),handlers={},notifications=[],navigations=[],cacheKeys=[];
  const origin='https://nexus.example.invalid';let waited;
  const self={location:{origin},addEventListener:(name,callback)=>{handlers[name]=callback;},skipWaiting:async()=>{},
    registration:{showNotification:async(title,options)=>notifications.push({title,options})},
    clients:{claim:async()=>{},matchAll:async()=>[{url:origin+'/dashboard#run',navigate:async url=>navigations.push(url),focus:async()=>{}}],openWindow:async url=>navigations.push(url)}};
  const caches={open:async()=>({addAll:async keys=>cacheKeys.push(...keys),put:async()=>{}}),keys:async()=>[],match:async()=>({static:true}),delete:async()=>true};
  vm.runInNewContext(code,{self,caches,URL,fetch:async()=>{throw Error('offline');},Promise});
  handlers.install({waitUntil:promise=>{waited=promise;}});await waited;
  assert.ok(cacheKeys.includes('/dashboard/presence.js'));
  for(const request of [{method:'GET',url:origin+'/api/presence/state'},{method:'POST',url:origin+'/api/presence/execute'},
    {method:'GET',url:origin+'/dashboard?token=private-fixture'},{method:'GET',url:'https://evil.invalid/dashboard'}]){
    handlers.fetch({request,respondWith:()=>assert.fail('Sensitive/nonstatic requests must not be cached')});
  }
  handlers.push({data:{json:()=>{throw Error('Push payload must be ignored');}},waitUntil:promise=>{waited=promise;}});await waited;
  assert.equal(notifications.length,1);assert.ok(!notifications[0].options.actions);
  handlers.notificationclick({notification:{close(){}},waitUntil:promise=>{waited=promise;}});await waited;
  assert.deepEqual(navigations,[origin+'/dashboard#presence']);
  assert.ok(!handlers.sync && !handlers.pushsubscriptionchange);
});

async function dashboard({owner=false,delayedWorld=false}={}) {
  const context=await browser.newContext({viewport:{width:360,height:900},reducedMotion:'reduce',serviceWorkers:'block'});
  const page=await context.newPage(),errors=[],streams=[];
  page.on('pageerror',e=>errors.push(e.message));
  const principal=owner?{kind:'operator',id:'operator',scopes:[]}:{kind:'device',id:'phone',name:'Fixture phone',scopes:['read']};
  const config={enabled:true,devices_enabled:true,voice_enabled:false,conversation_enabled:false,handoff_enabled:false,push_enabled:false,world_enabled:true,principal};
  let releaseWorld, imported=0, worldRequested;
  const worldReady=new Promise(resolve=>{worldRequested=resolve;});
  const worldCalls=[];
  await context.route('**/*',async route=>{
    const url=new URL(route.request().url()),path=url.pathname;
    if(url.hostname!=='nexus.example.invalid')return route.abort();
    if(path==='/api/session')return route.fulfill({json:{authenticated:true,required:true,principal}});
    if(path==='/api/presence/config')return route.fulfill({json:config});
    if(path==='/api/presence/events'){
      streams.push(url.href);
      return route.fulfill({contentType:'text/event-stream',body:'event: presence.state\ndata: '+JSON.stringify({state:{revision:1,conversation_id:'shell-fixture',speaker:'none',speaker_generation:0,active_mission:'',turns:[],voice:{},approvals:[],devices:[],missions:[],briefs:[],speech:[],principal,capabilities:config}})+'\n\n'});
    }
    if(path==='/world/world.js' && delayedWorld){
      imported++;
      const module="window.worldConnections=[];window.addEventListener('hud:connected',event=>{window.worldConnections.push(true);event.detail.api('countries');});";
      releaseWorld=()=>route.fulfill({body:module,contentType:'text/javascript'});worldRequested();return;
    }
    if(['/api/countries','/api/presence/world/countries'].includes(path)){
      worldCalls.push(path);return route.fulfill({json:{items:[],config:{enabled:false}}});
    }
    if(path.startsWith('/api/'))return route.fulfill({status:403,json:{error:'Device is not an administrator'}});
    let file;
    if(path==='/dashboard')file=asset('dashboard.html');
    else if(path==='/dashboard/operations.js')file=fileURLToPath(new URL('../../nexus/hud/assets/hud.js',import.meta.url));
    else if(path.startsWith('/dashboard/'))file=asset(path.slice('/dashboard/'.length));
    else if(path.startsWith('/world/'))file=fileURLToPath(new URL('../../nexus/hud/world/assets/'+path.slice(7),import.meta.url));
    else if(['/assistant-sw.js','/manifest.webmanifest','/assistant-icon.svg'].includes(path))file=asset(path.slice(1));
    if(!file)return route.fulfill({status:404,body:''});
    try{
      const types={js:'text/javascript',css:'text/css',html:'text/html',png:'image/png',svg:'image/svg+xml',ttf:'font/ttf',webmanifest:'application/manifest+json'};
      return route.fulfill({body:await readFile(file),contentType:types[file.split('.').at(-1)]||'application/octet-stream'});
    }catch{return route.fulfill({status:404,body:''});}
  });
  await page.goto('https://nexus.example.invalid/dashboard#presence');
  return {context,page,errors,streams,worldCalls,worldReady,releaseWorld:()=>releaseWorld(),imports:()=>imported};
}

test('the actual dashboard shell opens paired devices in Shared HUD without an admin page or URL token',async()=>{
  const {context,page,errors,streams}=await dashboard();
  try{
    await page.waitForSelector('#view-presence:not([hidden])');
    await page.waitForFunction(()=>window.nexusGui.principal?.kind==='device');
    assert.equal(await page.evaluate(()=>window.nexusGui.view),'presence');
    assert.equal(await page.locator('#phSend').isDisabled(),true);
    await page.locator('#navToggle').click();
    assert.equal(await page.locator('[data-nav-id="presence"]').isVisible(),true);
    assert.equal(await page.locator('[data-nav-id="security"]').isVisible(),false);
    await page.locator('[data-nav-id="presence"]').click();
    assert.ok(!page.url().includes('?'));assert.ok(streams.every(url=>!url.includes('?')));
    await page.locator('#phLanguage').click();
    assert.match(await page.locator('#view-presence h1').innerText(),/সহকারী/);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth),0);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('shared World reparents one widget; a late import/settings event cannot switch the HUD to the live adapter',async()=>{
  const fixture=await dashboard({owner:true,delayedWorld:true});
  const {page,context,worldReady,worldCalls,errors}=fixture;
  try{
    await page.waitForFunction(()=>window.nexusGui.principal?.kind==='operator');
    await page.evaluate(()=>{window.originalWidget=document.getElementById('world-widget');window.nexusGui.show('world');});
    await worldReady;
    await page.evaluate(()=>{window.nexusGui.show('presence');window.hudMount=window.nexusGui.mountWorld(document.getElementById('phWorldHost'),path=>window.nexusGui.api('/api/presence/world/'+path));});
    await fixture.releaseWorld();
    assert.equal(await page.evaluate(()=>window.hudMount),true);
    await page.waitForFunction(()=>window.worldConnections?.length===1);
    await page.evaluate(()=>window.dispatchEvent(new Event('nexus:world-settings')));
    assert.deepEqual(worldCalls,['/api/presence/world/countries']);
    assert.equal(await page.evaluate(()=>document.querySelector('#phWorldHost #world-widget')===window.originalWidget),true);
    await page.evaluate(()=>window.nexusGui.show('world'));
    await page.waitForFunction(()=>window.worldConnections?.length===2);
    assert.equal(worldCalls.at(-1),'/api/countries');
    assert.equal(await page.evaluate(()=>document.querySelector('#view-world #world-widget')===window.originalWidget),true);
    assert.equal(fixture.imports(),1);assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('SSE reconnect preserves its cursor but never resubmits a turn; OFF clears the private review and draft',async()=>{
  const {page,context,errors}=await panel({stream:true});
  try{
    await page.evaluate(()=>window.endStream());
    await page.waitForFunction(()=>!window.nexusPresence.state().connected);
    await page.locator('#phReconnect').click();
    await page.waitForFunction(()=>window.calls.filter(c=>c.path.endsWith('/events')).length===2);
    await page.waitForFunction(()=>window.nexusPresence.state().connected);
    const streams=await page.evaluate(()=>window.calls.filter(c=>c.path.endsWith('/events')));
    assert.equal(streams[1].headers['Last-Event-ID'],'1');
    await page.locator('#phDraft').fill('private unsent draft');
    await page.locator('[data-approval-action="approve"]').click();
    await page.evaluate(()=>{window.fixture.enabled=false;window.pushFrame('presence.state',{state:window.snapshot});});
    await page.waitForFunction(()=>!window.nexusPresence.state().connected);
    assert.equal(await page.locator('#phDraft').inputValue(),'');
    assert.equal(await page.locator('#phReviewText').innerText(),'');
    assert.equal(await page.locator('#phReview').evaluate(n=>n.open),false);
    assert.equal(await page.locator('#phSend').isDisabled(),true);
    assert.equal(await page.evaluate(()=>window.calls.filter(c=>/\/execute$|\/approval$/.test(c.path)).length),0);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('scopes/voice OFF stop local input and disable already-rendered approval buttons',async()=>{
  const {page,context,errors}=await panel();
  try{
    await page.locator('#phPtt').hover();await page.mouse.down();
    await page.waitForFunction(()=>window.recording);
    await page.evaluate(()=>{window.fixture.voice_enabled=false;window.fixture.principal.scopes=['read'];window.nexusPresence.accept({state:window.snapshot});});
    await page.mouse.up();
    assert.equal(await page.evaluate(()=>window.recording),false);
    assert.equal(await page.locator('[data-approval-action="approve"]').isDisabled(),true);
    assert.equal(await page.locator('#phPtt').isDisabled(),true);
    assert.equal(await page.evaluate(()=>window.audioDraft), '');
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('AudioContext failure releases the granted microphone and cannot create a transcription/task',async()=>{
  const {page,context,errors}=await panel({realMic:true});
  try{
    await page.evaluate(()=>{
      window.trackStops=0;
      Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{getUserMedia:async()=>({getTracks:()=>[{stop:()=>window.trackStops++}]})}});
      window.AudioContext=class{constructor(){throw Error('AudioContext fixture unavailable');}};
    });
    await page.locator('#phPtt').hover();await page.mouse.down();
    await page.waitForFunction(()=>window.trackStops===1);await page.mouse.up();
    assert.equal(await page.evaluate(()=>window.nexusAssistant.state().microphone),false);
    assert.match(await page.locator('#phError').innerText(),/AudioContext/);
    assert.equal(await page.evaluate(()=>window.calls.filter(c=>/transcribe|execute/.test(c.path)).length),0);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});
